# -*- coding: utf-8 -*-
"""
bos_plan.py
===========
Step-by-step description of a BOS test plan: for every test, in the order the runner executes it, the
commands sent to the BOS (CAN identifier and data bytes), the FSW SCPI sequence and capture size, supply and
telemetry readings, operator prompts, waits and the analysis done on the PC.

Built from the station's current settings with the same helpers the runner uses (bos_protocol builders,
BosStation._mcfg / _mod_n), so it shows what will actually be sent. English, or Russian when the interface
is Russian (like the findings and reports).
"""

from __future__ import annotations

import math

import bos_analysis as ba
import bos_protocol as bp

# kind: (English tag, Russian tag, colour)
KINDS = {
    "can": ("→BOS", "→БОС", "#58a6ff", "command to the BOS over CAN", "команда в БОС по CAN"),
    "rx": ("BOS→", "БОС→", "#79c0ff", "answer / telemetry from the BOS", "ответ / телеметрия от БОС"),
    "fsw": ("FSW", "FSW", "#c792ea", "R&S FSW (SCPI)", "R&S FSW (SCPI)"),
    "tfg": ("TFG", "TFG", "#56d4dd", "TFG6806 generator (SCPI)", "генератор TFG6806 (SCPI)"),
    "psu": ("PSU", "Пит.", "#f0b03e", "bench supply (HMP4040 / N6701C)", "источник питания (HMP4040 / N6701C)"),
    "op": ("Oper.", "Опер.", "#ff6b6b", "operator dialog", "окно для оператора"),
    "pc": ("PC", "ПК", "#4fd18b", "analysis on the PC", "обработка на ПК"),
    "wait": ("Wait", "Пауза", "#8a96a3", "pause", "пауза"),
    "loop": ("Loop", "Цикл", "#e3b341", "repeated steps", "повторяемые шаги"),
    "info": ("", "", "#8a96a3", "", ""),
}


class Step:
    def __init__(self, kind, text, children=None, tip=""):
        self.kind, self.text, self.children, self.tip = kind, text, list(children or []), tip


def _ru():
    try:
        import i18n
        return i18n.CURRENT == "ru"
    except Exception:
        return False


def L(en, ru):
    return ru if _ru() else en


def tag(kind):
    k = KINDS.get(kind, KINDS["info"])
    return k[1] if _ru() else k[0]


def tag_meaning(kind):
    k = KINDS.get(kind, KINDS["info"])
    return k[4] if _ru() else k[3]


def _hz(f):
    if f >= 1e9:
        return f"{f / 1e9:.6f} GHz"
    if f >= 1e6:
        return f"{f / 1e6:.6g} MHz"
    if f >= 1e3:
        return f"{f / 1e3:.6g} kHz"
    return f"{f:.6g} Hz"


def _sps(fs):
    return f"{fs / 1e6:.6g} MS/s"


# ------------------------------------------------------------------------------------------- CAN
def can_frame(st, code, data=b"", ack=True, receiver=bp.BOS):
    """'0x01 MODE_D  ID 0x06290001  data —' with the station's sender address."""
    cid, payload = bp.command(code, st.ctl.get("sender", 0x18), data, ack, receiver)
    name = bp.COMMANDS.get(code, ("?",))[0]
    hexd = " ".join(f"{b:02X}" for b in payload) if payload else "—"
    return f"0x{code:02X} {name}   ID 0x{cid:08X}   [{len(payload)}] {hexd}"


def _ack(st):
    return Step("rx", L("acknowledgement КВ+ within 300 ms (КВ- or none = test error)",
                        "квитанция КВ+ за 300 мс (КВ- или нет ответа = ошибка проверки)"))


def proto(st, code, data=b"", what=""):
    name = bp.COMMANDS.get(code, ("?", 0, ""))
    s = Step("can", f"{what or name[0]}  —  {L(name[2], name[2])}" if not what else what,
             [Step("info", can_frame(st, code, data)), _ack(st)],
             tip=L(f"CAN line '{st.ctl.get('line', 'BOS CAN1')}', sender 0x{st.ctl.get('sender', 0x18):02X}",
                   f"линия CAN '{st.ctl.get('line', 'BOS CAN1')}', отправитель 0x{st.ctl.get('sender', 0x18):02X}"))
    return [s]


def ref_gate(st, what):
    """Steps of BosStation._ref_gate (before every mode command)."""
    if not st.cfg.get("ext_ref", True):
        return []
    tol, emax = st.lim.get("pulsnumb_tol", 5.0), st.lim.get("err_ref_lo", 3.0)
    w = int(st.ctl.get("ref_wait_s", 30))
    sub = [Step("rx", L("fresh ИОК_ОСН (≤ 2.5 s old); if none: REQ_TM ИОК_ОСН",
                        "свежий ИОК_ОСН (не старше 2,5 с); если нет: REQ_TM ИОК_ОСН"),
                [Step("info", can_frame(st, 0x11, bp.p_req_tm(0xFC), ack=False) + L("  (no КВ)", "  (без КВ)"))]
                if st.ctl.get("method") == "protocol" else [])]
    return [Step("rx", L(f"reference check before {what}: PulsNumb_10MHz = 10 000 000 ± {tol:g} and |Err_Ref_LO| ≤ "
                         f"{emax:g}; waits up to {w} s, otherwise test error and no mode command",
                         f"проверка опоры перед {what}: PulsNumb_10MHz = 10 000 000 ± {tol:g} и |Err_Ref_LO| ≤ "
                         f"{emax:g}; ожидание до {w} с, иначе ошибка проверки и команда режима не отправляется"), sub)]


def _session_code(st, mode):
    return {"cw": 0xA0, "bpsk": st.ctl.get("bpsk_cmd", 0xA1), "oqpsk": st.ctl.get("oqpsk_cmd", 0xA4),
            "shr_pre": 0xA2, "shr_kbd": 0xA4}[mode]


def session(st, mode, att):
    """Steps of BosStation._session for mode / attenuation (protocol control)."""
    if mode == "rf_off":
        return proto(st, 0x01, what=L("MODE_D — standby, RF off", "MODE_D — дежурный режим, ВЧ выкл."))
    code = _session_code(st, mode)
    n = int(st.ctl.get("freq_n", 800))
    gate = ref_gate(st, bp.COMMANDS[code][0])
    rate_k = int(st.ctl.get("rate_ur" if code == 0xA1 else "rate_shr", 2))
    cnt = 2 if mode == "shr_kbd" else 2047
    out = list(gate)
    if st.ctl.get("keepalive", True):
        out.append(Step("can", L("target designation Angle_ErdStation_BKU (0°, 0°) — before the session and "
                                 "every 0.5 s while RF is on (valid 3 s)",
                                 "целеуказание Angle_ErdStation_BKU (0°, 0°) — перед сеансом и каждые 0,5 с, "
                                 "пока ВЧ включена (действует 3 с)"),
                        [Step("info", can_frame(st, 0x0C, bp.p_angle(0.0, 0.0), ack=False,
                                                receiver=bp.GROUP_ANGLE) + L("  (group УПНГ+АФАР+БОС, no КВ)",
                                                                             "  (группа УПНГ+АФАР+БОС, без КВ)"))]))
    try:
        data = bp.p_session(code, n, min(bp.ATTEN_MAX_DB, att), coding=1 if code == 0xA4 else 0, continuous=True,
                           count=cnt, rate=rate_k, reg=5 if code == 0xA1 else 1)
        fr = can_frame(st, code, data)
    except ValueError as e:
        fr = L(f"NOT POSSIBLE: {e}", f"НЕВОЗМОЖНО: {e}")
    what = {0xA0: L("carrier", "несущая"), 0xA1: L("УР КБД (BPSK)", "УР КБД (BPSK)"),
            0xA2: L("ШР preambles", "ШР преамбулы"), 0xA3: L("ШР ПООСШ", "ШР ПООСШ"),
            0xA4: L("ШР КБД (OQPSK)", "ШР КБД (OQPSK)")}[code]
    par = [f"N = {n} → {_hz(bp.freq_hz(n))}", f"Atten_EIRP = {min(bp.ATTEN_MAX_DB, att)} dB"]
    if code in (0xA1, 0xA2, 0xA3, 0xA4):
        par.append(f"{L('rate', 'скорость')} {bp.RATES[rate_k]} kbit/s")
    if code == 0xA4:
        par.append(L(f"{cnt} КБД, turbo coding", f"{cnt} КБД, турбокод"))
    par.append(L("continuous", "непрерывно"))
    out.append(Step("can", f"{bp.COMMANDS[code][0]} — {L('session', 'сеанс')}: {what}  ({', '.join(par)})",
                    [Step("info", fr), _ack(st)]))
    return out


def set_mode(st, mode, att, demo):
    """Steps of BosStation._set_mode."""
    label = {"rf_off": L("RF off", "ВЧ выкл."), "cw": L("carrier (CW)", "несущая (CW)"), "bpsk": "BPSK",
             "oqpsk": "OQPSK"}[mode]
    method = st.ctl.get("method", "protocol")
    settle = st.ctl.get("settle_ms", 1500) / 1000
    if method == "protocol":
        return session(st, mode, att) + [Step("wait", L(f"{settle:g} s settling", f"{settle:g} с установление"))]
    if demo:
        return [Step("pc", L(f"simulator switched to {label}", f"имитатор переключён: {label}"))]
    gate = ref_gate(st, label) if mode != "rf_off" else []
    uid = st.ctl.get("cmd", {}).get(mode)
    c = getattr(st.host, "can_win", None)
    cmd = c.store.by_uid(uid) if (c and uid) else None
    if cmd is not None:
        s = Step("can", L(f"CAN library command '{cmd.label()}' → {label}", f"команда из библиотеки CAN "
                          f"'{cmd.label()}' → {label}"), [Step("info", f"ID {cmd.id_str()}: {cmd.data_str()}")])
    else:
        s = Step("op", L(f"dialog: switch the BOS to {label}, press OK", f"окно: переключите БОС в режим "
                         f"{label}, нажмите OK"))
    return gate + [s, Step("wait", L(f"{settle:g} s settling", f"{settle:g} с установление"))]


def set_att(st, code, mode, demo):
    """Steps of BosStation._set_att."""
    code = int(max(0, min(bp.ATTEN_MAX_DB, code)))
    method = st.ctl.get("method", "protocol")
    settle = st.ctl.get("settle_ms", 1500) / 1000
    if method == "protocol":
        if mode in (None, "rf_off"):
            return [Step("pc", L(f"Atten_EIRP {code} dB stored for the next session",
                                 f"Atten_EIRP {code} дБ — для следующего сеанса"))]
        return session(st, mode, code) + [Step("wait", L(f"{settle:g} s settling", f"{settle:g} с установление"))]
    if demo:
        return [Step("pc", L(f"simulator attenuation {code} dB", f"ослабление имитатора {code} дБ"))]
    am = st.ctl.get("att_mode", "prompt")
    if am == "none":
        return [Step("info", L("attenuator control not configured → test skipped",
                               "управление аттенюатором не задано → проверка пропускается"))]
    if am == "can":
        tpl = st.ctl.get("att_data", "XX").split()
        try:
            data = " ".join(f"{code:02X}" if b.upper() == "XX" else b.upper() for b in tpl)
        except Exception:
            data = st.ctl.get("att_data", "")
        return [Step("can", L(f"attenuator command, code {code}", f"команда аттенюатора, код {code}"),
                     [Step("info", f"ID {st.ctl.get('att_id', '?')}  [{len(tpl)}] {data}  "
                                   f"({L('line', 'линия')} {st.ctl.get('att_line', 'BOS')})")]),
                Step("wait", L(f"{settle:g} s settling", f"{settle:g} с установление"))]
    return [Step("op", L(f"dialog: set Atten_EIRP to {code} dB, press OK",
                         f"окно: установите Atten_EIRP = {code} дБ, нажмите OK")),
            Step("wait", L(f"{settle:g} s settling", f"{settle:g} с установление"))]


# ------------------------------------------------------------------------------------------- FSW
def fsw_capture(st, center, fs, n, what, demo):
    """Steps of bos_station.fsw_capture (one I/Q capture)."""
    c = st.cfg
    head = L(f"I/Q capture «{what}»: {_hz(center)}, {_sps(fs)}, {n:,} samples ({n / fs * 1e3:.4g} ms, "
             f"{n * 8 / 1e6:.3g} MB)",
             f"запись I/Q «{what}»: {_hz(center)}, {_sps(fs)}, {n:,} отсчётов ({n / fs * 1e3:.4g} мс, "
             f"{n * 8 / 1e6:.3g} МБ)")
    if demo:
        return [Step("fsw", head + L("  — simulated", "  — имитация"))]
    lv = ["ADJ:LEV", "*OPC?"] if c.get("autolevel", True) else [f"DISP:WIND:TRAC:Y:SCAL:RLEV {c.get('ref_level', 0):.4g}"]
    scpi = (["INST:SEL 'IQ Analyzer'  " + L("(I/Q Analyzer channel; created on first use)",
                                            "(канал I/Q Analyzer; создаётся при первом использовании)"),
             f"FREQ:CENT {center:.12g}", f"TRAC:IQ:SRAT {fs:.10g}", f"TRAC:IQ:RLEN {int(n)}"] + lv +
            ["*OPC?", "SYST:ERR?  " + L("(errors → log)", "(ошибки → журнал)"), "TRAC:IQ:SRAT?",
             "*CLS;:INIT:IMM;*OPC  " + L("(single capture, wait for the end)", "(одна запись, ожидание конца)"),
             "TRAC:IQ:DATA:MEM?  " + L(f"→ {n:,} I/Q pairs, float32", f"→ {n:,} пар I/Q, float32")])
    out = [Step("fsw", head, [Step("info", s) for s in scpi])]
    if c.get("save_iq"):
        out.append(Step("pc", L("capture saved: bos_reports/captures/*.iq.tar", "запись сохраняется: "
                                "bos_reports/captures/*.iq.tar")))
    return out


def keep_session_note(st):
    if st.ctl.get("method") == "protocol":
        return [Step("info", L("before a capture: if the session started > 20 s ago it is re-sent "
                               "(stays inside Time_SNR)", "перед записью: если сеанс начат > 20 с назад, "
                               "команда сеанса повторяется (в пределах Time_SNR)"))]
    return []


def analysis(text_en, text_ru):
    return Step("pc", L(text_en, text_ru))


# ------------------------------------------------------------------------------------------- tests
def t_ref(st, demo, state):
    c = st.ctl
    out = []
    if c.get("tfg_setup", True):
        src, feed = c.get("tfg_src", "EXT"), int(c.get("tfg_feed", 1))
        scpi = []
        if src in ("INT", "EXT"):
            scpi.append(f"UTILity:CLOck:SOURce {'EXTernal' if src == 'EXT' else 'INTernal'}")
        if feed == 0:
            scpi.append("UTILity:INSide:CLOck ON  " + L("(internal 10 MHz on the rear port)",
                                                        "(внутренние 10 МГц на заднем разъёме)"))
        elif src == "EXT":
            scpi.append("UTILity:INSide:CLOck OFF  " + L("(rear port = input)", "(задний разъём = вход)"))
        scpi += ["UTILity:CLOck:SOURce?", "UTILity:INSide:CLOck?"]
        if feed in (1, 2):
            if c.get("tfg_ref_apply", True):
                amp, offs = float(c.get("tfg_ref_amp", 2.0)), float(c.get("tfg_ref_offs", 2.0))
                scpi += [f"SOURce{feed}:FUNCtion SINusoid", f"SOURce{feed}:FUNCtion:MODE CONTinuous",
                         f"SOURce{feed}:FREQuency 10000000Hz", f"SOURce{feed}:VOLTage {amp:g}Vpp",
                         f"SOURce{feed}:VOLTage:OFFSet {offs:g}V"]
            scpi += [f"OUTPut{feed} ON", L("read back: waveform, frequency (10 MHz), amplitude, offset, output",
                                           "контроль: форма, частота (10 МГц), амплитуда, смещение, выход")]
        scpi.append("SYSTem:ERRor?")
        what = {"EXT": L("external reference", "внешняя опора"), "INT": L("internal reference", "внутренняя опора"),
                "": L("reference unchanged", "опора без изменений")}[src]
        feed_txt = {1: L("10 MHz to the BOS from CH1", "10 МГц в БОС с CH1"),
                    2: L("10 MHz to the BOS from CH2", "10 МГц в БОС с CH2"),
                    0: L("10 MHz to the BOS from the rear port", "10 МГц в БОС с заднего разъёма"),
                    -1: L("outputs not touched", "выходы не меняются")}[feed]
        out.append(Step("tfg", L(f"TFG6806 first (before BOS power): {what}, {feed_txt}",
                                 f"сначала TFG6806 (до питания БОС): {what}, {feed_txt}"),
                        [Step("info", s) for s in scpi] +
                        ([Step("info", L("the TFG uses the external clock only if it detects a qualified one "
                                         "('Ext Ref On'); otherwise it runs on its internal clock - not readable "
                                         "over SCPI", "TFG использует внешнюю опору, только если обнаружит "
                                         "годный сигнал («Ext Ref On»); иначе работает от внутренней - по SCPI "
                                         "это не читается"))] if src == "EXT" else [])))
    pu = c.get("power_up", "prompt")
    if pu == "prompt" and not demo:
        out.append(Step("op", L("dialog: switch the BOS power on, press OK", "окно: включите питание БОС, "
                                                                              "нажмите OK")))
    elif pu == "supply":
        name = _supply_name(st) if not demo else L("simulated supply", "имитация источника")
        out.append(Step("psu", L(f"BOS power on: {name or 'no output selected → test error'} (voltage / current "
                                 f"from its tab{', + master' if (name or '').startswith('HMP') else ''})",
                                 f"питание БОС: {name or 'выход не выбран → ошибка'} (напряжение / ток с его "
                                 f"вкладки{', + общий выход' if (name or '').startswith('HMP') else ''})")))
    else:
        out.append(Step("info", L("BOS assumed powered (check only)", "БОС считается включённым (только проверка)")))
    if st.cfg.get("ext_ref", True):
        out.append(Step("rx", L(f"ИОК_ОСН after power-up (≤ {max(10, int(c.get('ref_wait_s', 30)))} s; REQ_TM if "
                                f"needed): PulsNumb_10MHz > 0 (10 MHz pulses present), Stat_10MHz",
                                f"ИОК_ОСН после включения (≤ {max(10, int(c.get('ref_wait_s', 30)))} с; при "
                                f"необходимости REQ_TM): PulsNumb_10MHz > 0 (импульсы 10 МГц есть), Stat_10MHz")))
    return out


def t_can12(st, demo, state):
    l1, l2 = st.ctl.get("line", "BOS CAN1"), st.ctl.get("line2", "BOS CAN2")
    out = []
    for n, line in ((1, l1), (2, l2)):
        sub = [Step("info", L(f"on line '{line}'", f"по линии '{line}'")),
               Step("info", can_frame(st, 0x00)),
               Step("rx", L(f"КВ+ must come back on '{line}' with 'via CAN{n}'",
                            f"КВ+ должна прийти по '{line}' с признаком «через CAN{n}»")),
               Step("info", can_frame(st, 0x11, bp.p_req_tm(0xFC))),
               Step("rx", L(f"КВ+, then ИОК_ОСН on '{line}' within 3 s", f"КВ+, затем ИОК_ОСН по '{line}' за 3 с"))]
        out.append(Step("can", L(f"CAN{n}: NOP + REQ_TM ИОК_ОСН via {line}", f"CAN{n}: NOP + REQ_TM ИОК_ОСН через "
                                                                             f"{line}"), sub))
    if st.ctl.get("can1_only", False):
        out += proto(st, 0x06, bytes([0x01]), L("SET_CAN = 01 — BOS transmits via CAN1 only",
                                                "SET_CAN = 01 — БОС передаёт только по CAN1"))
    out.append(Step("info", L(f"afterwards every command via CAN1 ({l1})", f"далее все команды через CAN1 ({l1})")))
    return out


def t_link(st, demo, state):
    out = proto(st, 0x00, what=L("NOP — link check", "NOP — проверка связи"))
    out += proto(st, 0x01, what=L("MODE_D — standby, RF off", "MODE_D — дежурный режим, ВЧ выкл."))
    state["mode"] = "rf_off"
    ts = int(st.ctl.get("time_snr", 255))
    if ts:
        out += proto(st, 0x04, bp.p_byte(ts), L(f"SET_Time_SNR {ts} s — session limit", f"SET_Time_SNR {ts} с — "
                                                                                          f"длительность сеанса"))
    if st.ctl.get("tm_enable", True):
        for tm, per in ((0xFB, 1.0), (0xFC, 1.0), (0xFD, 10.0)):
            out += proto(st, 0x10, bp.p_period_tm(tm, per), f"SET_Period_TM {bp.TM_TYPES[tm]} {per:g} "
                                                            f"{L('s', 'с')}")
        out += proto(st, 0x11, bp.p_req_tm(0xFA), L("REQ_TM ИОК_КОНФИГ — read the stored configuration",
                                                    "REQ_TM ИОК_КОНФИГ — чтение сохранённой конфигурации"))
        out.append(Step("rx", L("ИОК_КОНФИГ: 18 CAN frames, waited for ≤ 3 s",
                                "ИОК_КОНФИГ: 18 кадров CAN, ожидание ≤ 3 с")))
        out.append(analysis("CRC-16 check; Config_Type, Sour_10MHz, Data_Random, Time_SNR, Numb_Preamb, telemetry "
                            "periods, temperature limits, preamble value → Measurements",
                            "проверка CRC-16; Config_Type, Sour_10MHz, Data_Random, Time_SNR, Numb_Preamb, периоды "
                            "телеметрии, пороги температуры, преамбула → Измерения"))
    return out


def t_pre(st, demo, state):
    lim = st.lim
    return [Step("rx", L("latest BOS telemetry received on the CAN line (ИОК_ДОП / ИОК_ОСН)",
                         "последняя телеметрия БОС с линии CAN (ИОК_ДОП / ИОК_ОСН)")),
            analysis(f"checks: telemetry age ≤ {lim['tlm_stale_s']:g} s; PLLs LMK / DAC clock / LMX2594 / FPGA JESD / "
                     f"FPGA locked; JESD204B link OK; FPGA rails 1 / 1.8 / 2.5 V ±{lim['rail_tol_pct']:g} %; "
                     f"temperatures ≤ {lim['temp_max']:g} °C; 10 MHz source "
                     f"{'external' if st.cfg.get('ext_ref') else 'internal'} and valid; "
                     f"|Err_Ref_LO| ≤ {lim['err_ref_lo']:g}",
                     f"проверки: возраст телеметрии ≤ {lim['tlm_stale_s']:g} с; ФАПЧ LMK / такт ЦАП / LMX2594 / "
                     f"JESD ПЛИС / ПЛИС захвачены; канал JESD204B исправен; шины ПЛИС 1 / 1,8 / 2,5 В "
                     f"±{lim['rail_tol_pct']:g} %; температуры ≤ {lim['temp_max']:g} °C; опора 10 МГц "
                     f"{'внешняя' if st.cfg.get('ext_ref') else 'внутренняя'} и исправна; "
                     f"|Err_Ref_LO| ≤ {lim['err_ref_lo']:g}")]


def _supply_name(st):
    src = st.data.get("supply", "none")
    if src == "none":
        return None
    kind, ch = src.split(":")
    return ("HMP4040" if kind == "hmp" else "N6701C") + f" {L('output', 'выход')} {ch}"


def t_supply(st, demo, state):
    name = _supply_name(st) if not demo else L("simulated supply", "имитация источника")
    if not name:
        return [Step("info", L("no supply output selected (Unit / signal) → test skipped",
                               "выход источника не выбран (Блок / сигнал) → проверка пропускается"))]
    att = state["att"]
    out = set_mode(st, "rf_off", att, demo)
    out.append(Step("psu", L(f"current reading, RF off — {name} (last value polled by its tab)",
                             f"измерение тока, ВЧ выкл. — {name} (последнее значение с его вкладки)")))
    out += set_mode(st, "cw", att, demo)
    out.append(Step("wait", "0.5 s" if not _ru() else "0,5 с"))
    out.append(Step("psu", L(f"current reading, RF on (CW) — {name}", f"измерение тока, ВЧ вкл. (CW) — {name}")))
    out.append(analysis(f"limits: standby ≤ {st.lim['i_standby_max']:g} A, session ≤ {st.lim['i_max']:g} A, "
                        f"PA rise ≥ {st.lim['pa_delta_i_min']:g} A",
                        f"нормы: дежурный ≤ {st.lim['i_standby_max']:g} А, сеанс ≤ {st.lim['i_max']:g} А, "
                        f"прирост тока УМ ≥ {st.lim['pa_delta_i_min']:g} А"))
    state["mode"] = "cw"
    return out


def t_cw(st, demo, state):
    c = st.cfg
    out = set_mode(st, "cw", state["att"], demo)
    state["mode"] = "cw"
    out += keep_session_note(st)
    out += fsw_capture(st, c["carrier"], c["cw_fs"], 1 << 15, L("carrier", "несущая"), demo)
    out.append(analysis(f"carrier frequency (Hz, ppm), power (+{c['loss_db']:g} dB cable), integrated phase noise "
                        f"{c['pn_lo'] / 1e3:g}…{c['pn_hi'] / 1e3:g} kHz, spurs",
                        f"частота несущей (Гц, ppm), мощность (+{c['loss_db']:g} дБ кабель), интегральный фазовый "
                        f"шум {c['pn_lo'] / 1e3:g}…{c['pn_hi'] / 1e3:g} кГц, побочные"))
    return out


def _mod_steps(st, mod, demo, label=None):
    c = st._mcfg(mod)
    n = st._mod_n(c)
    out = fsw_capture(st, c["carrier"], c["fs"], n, label or mod, demo)
    rs = abs(c["fs"] / c["symbol_rate"] - round(c["fs"] / c["symbol_rate"])) > 1e-6 or \
        round(c["fs"] / c["symbol_rate"]) < ba.DEMOD_SPS
    txt_en = (f"demodulation at {c['symbol_rate'] / 1e3:g} ksym/s ({c['nsym'] if mod != 'BPSK' else c['nsym']:,} "
              f"symbols{', resampled to 4 samples/symbol' if rs else ''}): BPSK / QPSK / OQPSK tried → detected "
              f"modulation, MER, EVM, frequency and clock error, carrier leakage, I/Q imbalance, phase error, PAPR "
              f"loss, OBW, ACLR, spurs")
    txt_ru = (f"демодуляция при {c['symbol_rate'] / 1e3:g} кСимв/с ({c['nsym']:,} символов"
              f"{', передискретизация до 4 отсчётов/символ' if rs else ''}): пробуются BPSK / QPSK / OQPSK → "
              f"модуляция, MER, EVM, ошибки частоты и такта, просачивание несущей, разбаланс I/Q, фазовая "
              f"ошибка, потери PAPR, полоса, ACLR, побочные")
    out.append(analysis(txt_en, txt_ru))
    if mod == "BPSK" and st.cfg.get("decode_ur", True):
        out.append(analysis(f"УР decoding: SP-L, sync 0x1ACFFC1D, RS(255,223)×5, transport-frame CRC, SCID "
                            f"{st.cfg.get('scid', 0)}, idle frames, frame counter, channel BER",
                            f"декодирование УР: SP-L, синхромаркер 0x1ACFFC1D, RS(255,223)×5, CRC транспортного "
                            f"кадра, SCID {st.cfg.get('scid', 0)}, пустые кадры, счётчик кадров, BER канала"))
    return out


def t_mod(mod):
    def f(st, demo, state):
        m = mod.lower()
        out = set_mode(st, m, state["att"], demo)
        state["mode"] = m
        out += keep_session_note(st)
        return out + _mod_steps(st, mod, demo)
    return f


def t_shr(st, demo, state):
    import bos_shr
    c = st._mcfg("OQPSK")
    fs, sr = c["fs"], c["symbol_rate"]
    rate = bp.RATES[int(st.ctl.get("rate_shr", 4))]
    spc = fs / sr / 2
    sfp, sf = bos_shr.chips_per_bit(rate, True), bos_shr.chips_per_bit(rate)
    proto_ctl = st.ctl.get("method") == "protocol"
    out = [Step("info", L(f"ШР {rate} kbit/s: {sfp:,} chips per preamble bit, {sf:,} per КБД bit",
                          f"ШР {rate} кбит/с: {sfp:,} чипов на бит преамбулы, {sf:,} на бит КБД"))]
    # 1) preambles
    if proto_ctl:
        out += session(st, "shr_pre", state["att"])
        out.append(Step("wait", "0.6 s" if not _ru() else "0,6 с"))
    elif not demo:
        out.append(Step("op", L("dialog: start a ШР session with preambles only, press OK",
                                "окно: запустите сеанс ШР только с преамбулами, нажмите OK")))
    n = int(72 * sfp * spc)
    out += fsw_capture(st, c["carrier"], fs, n, L(f"ШР preambles {rate} kbit/s", f"ШР преамбулы {rate} кбит/с"),
                       demo)
    out.append(analysis("chip receiver (OQPSK, RRC), despreading with PRS 3 → preambles found, preamble value, "
                        "despread SNR and efficiency",
                        "приём чипов (OQPSK, RRC), сжатие ПСП 3 → найденные преамбулы, значение преамбулы, "
                        "ОСШ и эффективность сжатия"))
    if rate < st.cfg.get("shr_decode_min", 512):
        out.append(Step("info", L(f"КБД decoding skipped: {rate} kbit/s < {st.cfg.get('shr_decode_min', 512)} kbit/s "
                                  f"(one КБД = {17912 * sf / 1e6:.1f} M chips)",
                                  f"декодирование КБД пропускается: {rate} кбит/с < "
                                  f"{st.cfg.get('shr_decode_min', 512)} кбит/с (один КБД = {17912 * sf / 1e6:.1f} "
                                  f"млн чипов)")))
        state["mode"] = "shr_pre"
        return out
    # 2) whole packet from the RF onset
    n_pre = 5
    n2 = int(((n_pre + 1) * 32 * sfp + 2 * bos_shr.CBD_SR_BITS * sf) * spc * 1.03) + 20000
    lvl = st.cfg["power_nom"] - st.cfg.get("loss_db", 0)
    if proto_ctl:
        out += session(st, "rf_off", state["att"])
    if demo:
        out.append(Step("fsw", L(f"simulated packet capture, {n2:,} samples", f"имитация записи пакета, {n2:,} отсчётов")))
    else:
        out.append(Step("fsw", L(f"arm a triggered capture: {_hz(c['carrier'])}, {_sps(fs)}, {n2:,} samples "
                                 f"({n2 / fs * 1e3:.4g} ms)", f"взвод записи по запуску: {_hz(c['carrier'])}, "
                                 f"{_sps(fs)}, {n2:,} отсчётов ({n2 / fs * 1e3:.4g} мс)"),
                        [Step("info", s) for s in
                         ["INST:SEL 'IQ Analyzer'", f"FREQ:CENT {c['carrier']:.12g}", f"TRAC:IQ:SRAT {fs:.10g}",
                          f"TRAC:IQ:RLEN {n2}", f"DISP:WIND:TRAC:Y:SCAL:RLEV {lvl + 5:.4g}",
                          "TRIG:SOUR IFP  " + L("(IF power trigger)", "(запуск по мощности ПЧ)"),
                          f"TRIG:LEV:IFP {lvl - 20:.4g}", "*OPC?", "SYST:ERR?", "TRAC:IQ:SRAT?",
                          "*CLS;:INIT:IMM;*OPC  " + L("(waits for the RF)", "(ждёт появления ВЧ)")]]))
    if proto_ctl:
        out += session(st, "shr_kbd", state["att"])
    elif not demo:
        out.append(Step("op", L("dialog: the FSW waits — start a ШР session with КБД now, press OK",
                                "окно: FSW ждёт — запустите сеанс ШР с КБД, нажмите OK")))
    if not demo:
        out.append(Step("fsw", L("fetch the triggered record", "чтение записанного"),
                        [Step("info", "TRAC:IQ:DATA:MEM?  " + L(f"→ {n2:,} I/Q pairs", f"→ {n2:,} пар I/Q")),
                         Step("info", "TRIG:SOUR IMM  " + L("(trigger back to free run)", "(запуск снова свободный)"))]))
    out.append(analysis("preambles + end-of-preamble marker 0xDA2B1E5E, КБД despread with PRS 1 ⊕ 2, ASM "
                        "0x034776C7272895B0, de-randomised, turbo decoded (r 1/2, k 8920), CRC, idle frames, counter",
                        "преамбулы + маркер конца 0xDA2B1E5E, сжатие КБД ПСП 1 ⊕ 2, ASM 0x034776C7272895B0, "
                        "дерандомизация, турбодекодирование (1/2, k 8920), CRC, пустые кадры, счётчик"))
    state["mode"] = "shr_kbd"
    return out


def t_att(st, demo, state):
    c = st.cfg
    out = set_mode(st, "cw", state["att"], demo)
    state["mode"] = "cw"
    codes = ba.ATT_CODES_DEFAULT
    body = []
    for k, code in enumerate(codes):
        sub = set_att(st, code, "cw", demo)
        sub += fsw_capture(st, c["carrier"], c["cw_fs"], 1 << 13, L(f"code {code}", f"код {code}"), demo)
        sub.append(analysis("tone power", "мощность тона"))
        body.append(Step("loop", L(f"{k + 1}/{len(codes)}: Atten_EIRP = {code} dB", f"{k + 1}/{len(codes)}: "
                                                                                    f"Atten_EIRP = {code} дБ"), sub))
    out.append(Step("loop", L(f"{len(codes)} settings: {', '.join(map(str, codes))} dB",
                              f"{len(codes)} установок: {', '.join(map(str, codes))} дБ"), body))
    out += set_att(st, c["att_code"], "cw", demo)
    out.append(analysis("least-squares weights of the 1 / 2 / 4 / 8 / 16 dB steps, residual, deviation from nominal",
                        "веса ступеней 1 / 2 / 4 / 8 / 16 дБ по МНК, остаток, отклонение от номинала"))
    return out


def t_lin(st, demo, state):
    c = st.cfg
    mod = "BPSK" if st._oqpsk_band_limited() else "OQPSK"
    out = [Step("info", L(f"signal: {mod}" + (" (FSW bandwidth narrower than OQPSK)" if mod == "BPSK" else ""),
                          f"сигнал: {mod}" + (" (полоса FSW уже сигнала OQPSK)" if mod == "BPSK" else "")))]
    out += set_mode(st, mod.lower(), state["att"], demo)
    state["mode"] = mod.lower()
    out += _mod_steps(st, mod, demo, L("nominal attenuation", "номинальное ослабление"))
    out += set_att(st, c["att_code"] + c["lin_step"], mod.lower(), demo)
    out += _mod_steps(st, mod, demo, f"+{c['lin_step']:g} dB")
    out += set_att(st, c["att_code"], mod.lower(), demo)
    out.append(analysis(f"gain compression (expected −{c['lin_step']:g} dB), PAPR loss and ACLR at both settings",
                        f"компрессия усиления (ожидается −{c['lin_step']:g} дБ), потери PAPR и ACLR при обеих "
                        f"установках"))
    return out


def t_harm(st, demo, state):
    c = st.cfg
    out = set_mode(st, "cw", state["att"], demo)
    state["mode"] = "cw"
    out += fsw_capture(st, c["carrier"], c["cw_fs"], 1 << 13, L("carrier", "несущая"), demo)
    m = int(c["lo_mult"])
    body = []
    for k in range(1, 2 * m):
        if k == m:
            continue
        f = c["carrier"] * k / m
        if f > 50e9 or f < 1e6:
            continue
        name = f"{k}/{m} × " + L("carrier", "несущая") + (L(" (LO)", " (гетеродин)") if k == 1 else "")
        body.append(Step("loop", f"{name}: {_hz(f)}", fsw_capture(st, f, 2e6, 1 << 13, name, demo)))
    out.append(Step("loop", L(f"{len(body)} LO-related frequencies", f"{len(body)} частот, связанных с гетеродином"),
                    body))
    out.append(analysis(f"line levels in dBc vs. the carrier, limit ≤ {st.lim['lo_harm_max_dbc']:g} dBc",
                        f"уровни линий в дБн относительно несущей, норма ≤ {st.lim['lo_harm_max_dbc']:g} дБн"))
    return out


def t_drift(st, demo, state):
    c = st.cfg
    out = set_mode(st, "oqpsk", state["att"], demo)
    state["mode"] = "oqpsk"
    n = int(c["drift_n"])
    sub = keep_session_note(st) + _mod_steps(st, "OQPSK", demo, L("stability", "стабильность"))
    sub.append(Step("rx", L("temperature Temp_DT1 from the telemetry", "температура Temp_DT1 из телеметрии")))
    sub.append(Step("wait", L(f"{c['drift_s']:g} s (not after the last)", f"{c['drift_s']:g} с (кроме последней)")))
    out.append(Step("loop", L(f"{n} × (capture, wait {c['drift_s']:g} s) ≈ {n * c['drift_s'] / 60:.1f} min",
                              f"{n} × (запись, пауза {c['drift_s']:g} с) ≈ {n * c['drift_s'] / 60:.1f} мин"), sub))
    out.append(analysis("power and frequency drift (peak-peak), worst MER, temperature change",
                        "дрейф мощности и частоты (размах), худший MER, изменение температуры"))
    return out


def t_ber(st, demo, state):
    c = st.cfg
    if not c.get("decode_ur", True):
        return [Step("info", L("УР decoding switched off → test skipped", "декодирование УР выключено → "
                                                                          "проверка пропускается"))]
    out = set_mode(st, "bpsk", state["att"], demo)
    state["mode"] = "bpsk"
    m = st._mcfg("BPSK")
    bits_per = max(1, int(m["nsym"] / 2 * 0.9))                 # data bits per capture (SP-L: 2 symbols per bit)
    k = math.ceil(int(c.get("ber_bits", 1e6)) / bits_per)
    sub = keep_session_note(st) + _mod_steps(st, "BPSK", demo, "BER")
    out.append(Step("loop", L(f"captures until {int(c.get('ber_bits', 1e6)):,} code bits (≈ {k} captures, "
                              f"max 2000; stops after 3 without frames)",
                              f"записи до {int(c.get('ber_bits', 1e6)):,} кодовых бит (≈ {k} записей, не более "
                              f"2000; стоп после 3 без кадров)"), sub))
    out.append(analysis("totals: frames, RS corrections / failures, CRC errors, BER with 95 % bound, SCID, counter gaps",
                        "итоги: кадры, исправления / отказы RS, ошибки CRC, BER с границей 95 %, SCID, пропуски "
                        "счётчика"))
    return out


TESTS = {"ref": t_ref, "can12": t_can12, "link": t_link, "pre": t_pre, "supply": t_supply, "cw": t_cw, "bpsk": t_mod("BPSK"),
         "oqpsk": t_mod("OQPSK"), "shr": t_shr, "att": t_att, "lin": t_lin, "harm": t_harm, "drift": t_drift,
         "ber": t_ber}


def describe(st, plan):
    """[(test id or '', title, [Step])] for the plan as the runner would execute it now."""
    tests = list(st.data["plans"].get(plan, []))
    demo = st._is_demo()
    method = st.ctl.get("method", "protocol")
    if method == "protocol" and "link" not in tests:
        tests.insert(0, "link")
    if method == "protocol" and st.ctl.get("can_check", True) and "can12" not in tests:
        tests.insert(0, "can12")
    if "ref" not in tests:
        tests.insert(0, "ref")
    out = []
    prep = [Step("info", L("FSW must be connected (Spectrum Analyzer tab); its live view is stopped",
                           "FSW должен быть подключён (вкладка Анализатор спектра); его просмотр останавливается"))]
    if st.cfg.get("fsw_bw_auto", True):
        prep.append(Step("fsw", L("*OPT? → I/Q bandwidth option (B160 / B320 / B512 …) → bandwidth and OQPSK sample "
                                  "rate", "*OPT? → опция полосы I/Q (B160 / B320 / B512 …) → полоса и частота "
                                  "дискретизации OQPSK")))
    mtxt = {"protocol": L("control: BOS protocol over CAN (ПИЛВ 0102-004)", "управление: протокол БОС по CAN "
                          "(ПИЛВ 0102-004)"),
            "library": L("control: commands from the CAN library", "управление: команды из библиотеки CAN"),
            "prompt": L("control: the operator switches the BOS", "управление: оператор переключает БОС")}
    prep.append(Step("info", mtxt.get(method, method)))
    if method == "protocol":
        prep.append(Step("info", L(f"CAN line '{st.ctl.get('line', 'BOS CAN1')}', sender 0x{st.ctl.get('sender', 0x18):02X}, "
                                   f"BOS 0x{bp.BOS:02X}, N = {st.ctl.get('freq_n', 800)} → "
                                   f"{_hz(bp.freq_hz(int(st.ctl.get('freq_n', 800))))}",
                                   f"линия CAN '{st.ctl.get('line', 'BOS CAN1')}', отправитель "
                                   f"0x{st.ctl.get('sender', 0x18):02X}, БОС 0x{bp.BOS:02X}, N = "
                                   f"{st.ctl.get('freq_n', 800)} → {_hz(bp.freq_hz(int(st.ctl.get('freq_n', 800))))}")))
    if demo:
        prep.append(Step("info", L("DEMO: commands and captures go to the simulated BOS / FSW",
                                   "ДЕМО: команды и записи идут в имитатор БОС / FSW")))
    out.append(("", L("Before the plan", "Перед планом"), prep))
    state = {"mode": None, "att": int(st.cfg.get("att_code", 0))}
    for t in tests:
        fn = TESTS.get(t)
        try:
            steps = fn(st, demo, state) if fn else [Step("info", "?")]
        except Exception as e:                       # never break the GUI for a description
            steps = [Step("info", f"{type(e).__name__}: {e}")]
        out.append((t, None, steps))
    end = []
    if method == "protocol":
        end += [Step("can", L("MODE_D — leave the BOS in standby, RF off (if a session is on)",
                              "MODE_D — оставить БОС в дежурном режиме, ВЧ выкл. (если сеанс идёт)"),
                     [Step("info", can_frame(st, 0x01, ack=False))])]
    end += [Step("pc", L("verdict PASS / PASS (warnings) / FAIL, findings, corrections",
                         "заключение НОРМА / НОРМА (замечания) / НЕ НОРМА, выводы, поправки")),
            Step("pc", L("HTML report bos_reports/BOS_<serial>_<date>.html, history bos_history.csv",
                         "отчёт HTML bos_reports/BOS_<номер>_<дата>.html, история bos_history.csv"))]
    out.append(("", L("After the plan", "После плана"), end))
    return out


def count_steps(steps):
    return sum(1 + count_steps(s.children) for s in steps if s.kind != "info")
