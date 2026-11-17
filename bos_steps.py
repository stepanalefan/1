# -*- coding: utf-8 -*-
"""
bos_steps.py
============
User-defined test plans for the BOS test station.

A plan (bos_station.json -> "plans" -> name) is a list of items:
    "cw"                       a built-in test (code in bos_station.py, BosStation._t_<id>)
    {"type": "bos_cmd", ...}   a step defined by the user (types below)
Plan options (bos_station.json -> "plan_opts" -> name):
    {"prep": "auto" | "on" | "off",          reference / CAN lines / link setup before the plan
     "ctl": {key: value}}                    parameters of the built-in tests for this plan only

Step types
    bos_cmd   BOS command from bos_commands_0102.json with its parameters, expected КВ+ / КВ- / none
    can       frame from the CAN tab library (any unit, any line)
    rf        BOS RF mode: off / carrier / BPSK / OQPSK session (reference gate included)
    psu       power supply: set V / I limit, output on / off, measure V and I against limits
    tlm       telemetry value against limits (checked on the PC)
    scpi      SCPI command or query to the FSW / TFG6806, numeric limits or expected text
    operator  instruction to the operator: confirm, pass / fail question, or a value with limits
    wait      pause
    note      section title / note in the report and the log
"""

from __future__ import annotations

import copy

from PyQt5 import QtCore, QtWidgets

import bos_protocol as bp

# type: (name, plan view kind)
TYPES = {
    "bos_cmd": ("BOS command (0102)", "can"),
    "can": ("CAN frame (CAN tab library)", "can"),
    "rf": ("BOS RF mode", "can"),
    "psu": ("Power supply", "psu"),
    "tlm": ("Telemetry check", "pc"),
    "scpi": ("Instrument SCPI (FSW / TFG)", "fsw"),
    "operator": ("Operator step", "op"),
    "wait": ("Wait", "wait"),
    "note": ("Note / section", "info"),
}
DEFAULTS = {
    "bos_cmd": {"code": 0x00, "params": {}, "expect": "КВ+", "line": 1},
    "can": {"uid": ""},
    "rf": {"mode": "cw", "att": None},
    "psu": {"supply": "plan", "action": "measure", "v": 14.0, "i": 1.5, "settle_s": 0.5,
            "v_min": None, "v_max": None, "i_min": None, "i_max": None},
    "tlm": {"key": "Temp_DT1", "min": None, "max": 70.0, "timeout_s": 3.0},
    "scpi": {"inst": "FSW", "cmd": "*IDN?", "check": "none", "min": None, "max": None, "text": ""},
    "operator": {"text": "", "mode": "confirm", "unit": "", "min": None, "max": None},
    "wait": {"s": 1.0},
    "note": {"text": ""},
}
RF_MODES = [("rf_off", "RF off (MODE_D, standby)"), ("cw", "Carrier (CW)"), ("bpsk", "BPSK session"),
            ("oqpsk", "OQPSK session")]
SUPPLIES = [("plan", "Station supply (Unit / signal page)")] + \
    [(f"hmp:{c}", f"HMP4040 OUT{c}") for c in range(1, 5)] + \
    [(f"n67:{c}", f"N6701C output {c}") for c in range(1, 5)] + [("it68:1", "IT6832")]
PSU_ACTIONS = [("measure", "Measure V / I (check limits)"), ("set", "Set V and I limit (output unchanged)"),
               ("set_on", "Set V and I limit, output ON"), ("on", "Output ON"), ("off", "Output OFF")]
OP_MODES = [("confirm", "Instruction — operator confirms (Done / Abort)"),
            ("passfail", "Question — operator answers Pass / Fail"),
            ("value", "Measurement — operator enters a value (limits)")]
TLM_KEYS = ["Temp_DT1", "Temp_DT2", "Temp_DT3", "vdd_1v", "vdd_1_8v", "vdd_2_5v", "PulsNumb_10MHz", "Err_Ref_LO",
            "Stat_10MHz", "pll_lmk", "pll_lmx (DAC)", "pll_lmx (mod)", "pll_jesd", "pll_all", "jesd_err"]

# ---- parameters of the built-in tests that can be set per plan (bos_station.json "ctl" keys) ----
_SESS = [(c, f"0x{c:02X}  {n}") for c, n in bp.SR_COMMANDS.items() if c != 0xA0]
PARAMS = {
    "freq_n": ("Frequency code N (F = 37650 + 0.125·N MHz)", "int", (8, 1592), ""),
    "bpsk_cmd": ("BPSK session command", "choice", _SESS, ""),
    "oqpsk_cmd": ("OQPSK session command", "choice", _SESS, ""),
    "rate_ur": ("УР rate", "choice", [(k, f"{bp.RATES[k]} kbit/s") for k in range(0, 6)], ""),
    "rate_shr": ("ШР rate", "choice", [(k, f"{bp.RATES[k]} kbit/s") for k in range(1, 8)], ""),
    "time_snr": ("Session limit SET_Time_SNR", "int", (0, 255), " s"),
    "sr_min_s": ("Minimum session duration (re-issued before it ends)", "int", (0, 3600), " s"),
    "tm_enable": ("Set telemetry periods (SET_PeriodTM)", "bool", None, ""),
    "sender": ("Sender address", "choice", [(a, f"0x{a:02X}  {n}") for a, n in bp.SENDERS], ""),
    "line": ("CAN line 1", "str", None, ""),
    "line2": ("CAN line 2", "str", None, ""),
    "can1_only": ("SET_CAN = CAN1 only after the line check", "bool", None, ""),
    "keepalive": ("Target designation keep-alive (Angle_ErdStation_BKU)", "bool", None, ""),
    "settle_ms": ("Settle time after a mode change", "int", (0, 20000), " ms"),
    "tfg_setup": ("Set the TFG6806 up as reference", "bool", None, ""),
    "tfg_src": ("TFG6806 own clock", "choice", [("EXT", "External"), ("INT", "Internal"), ("", "don't change")], ""),
    "tfg_feed": ("BOS 10 MHz from", "choice", [(1, "TFG CH1"), (2, "TFG CH2"), (0, "TFG rear 10 MHz Out"),
                                               (-1, "don't touch the TFG outputs")], ""),
    "tfg_ref_apply": ("Apply the standard 10 MHz settings (sine, 2 V, 2 V offset)", "bool", None, ""),
    "tfg_ref_amp": ("10 MHz amplitude", "float", (0.01, 10.0), " Vpp"),
    "tfg_ref_offs": ("10 MHz offset", "float", (-5.0, 5.0), " V"),
    "power_up": ("BOS power-up", "choice", [("prompt", "Ask the operator"), ("supply", "Switch the supply on"),
                                            ("none", "Already on")], ""),
    "ref_wait_s": ("Wait for the reference up to", "int", (0, 600), " s"),
}
_MOD = ["freq_n", "settle_ms", "keepalive", "time_snr", "sr_min_s"]
TEST_PARAMS = {
    "ref": ["tfg_setup", "tfg_src", "tfg_feed", "tfg_ref_apply", "tfg_ref_amp", "tfg_ref_offs", "power_up",
            "ref_wait_s"],
    "can12": ["line", "line2", "can1_only"],
    "link": ["sender", "time_snr", "tm_enable"],
    "cmds": ["sender", "time_snr", "tm_enable", "can1_only"],
    "pre": [],
    "supply": _MOD,
    "cw": _MOD,
    "bpsk": _MOD + ["bpsk_cmd", "rate_ur"],
    "oqpsk": _MOD + ["oqpsk_cmd", "rate_shr"],
    "shr": _MOD + ["oqpsk_cmd", "rate_shr"],
    "ber": _MOD + ["bpsk_cmd", "rate_ur"],
}
FSW_TESTS = {"cw", "bpsk", "oqpsk", "att", "lin", "harm", "drift", "shr", "ber", "live"}


def test_params(tid: str) -> list[str]:
    return TEST_PARAMS.get(tid, _MOD + ["bpsk_cmd", "oqpsk_cmd", "rate_ur", "rate_shr"])


def is_step(item) -> bool:
    return isinstance(item, dict)


def new_step(kind: str) -> dict:
    s = {"type": kind, "title": ""}
    s.update(copy.deepcopy(DEFAULTS[kind]))
    return s


def _num(v, unit=""):
    return "—" if v is None else f"{v:g}{unit}"


def _lim(lo, hi, unit=""):
    if lo is None and hi is None:
        return ""
    if lo is not None and hi is not None:
        return f"{lo:g}…{hi:g}{unit}" if lo != hi else f"= {lo:g}{unit}"
    return f"≥ {lo:g}{unit}" if lo is not None else f"≤ {hi:g}{unit}"


def cmd_params_text(code, params) -> str:
    d = bp.DEFS.get(code, {})
    out = []
    for f in d.get("fields", []):
        if f["name"] in params:
            v = params[f["name"]]
            vals = f.get("values", {})
            txt = vals.get(str(v)) or vals.get(f"0x{v:02X}" if isinstance(v, int) else "") if vals else None
            unit = f" {f['unit']}" if f.get("unit") and not txt and f["unit"] != "°" else f.get("unit", "") if not txt else ""
            wide = isinstance(v, int) and (int(f.get("len", 1)) > 1 or "parts" in f) and "scale" not in f
            out.append(f"{f['name']}={txt or (f'{v:g}' if isinstance(v, float) else f'0x{v:X}' if wide else v)}{unit}")
    return ", ".join(out)


def _L(en, ru):
    try:
        import i18n
        return ru if i18n.CURRENT == "ru" else en
    except Exception:
        return en


SUPPLIES_RU = {"plan": "Источник стенда (страница Блок / сигнал)", "it68:1": "IT6832"}
RF_RU = {"rf_off": "ВЧ выкл. (MODE_D, ДЖР)", "cw": "Несущая (CW)", "bpsk": "сеанс BPSK", "oqpsk": "сеанс OQPSK"}


def _sup(sel):
    if _L("en", "ru") == "ru":
        if sel in SUPPLIES_RU:
            return SUPPLIES_RU[sel]
        if sel.startswith("hmp:"):
            return f"HMP4040 ВЫХ{sel[4:]}"
        if sel.startswith("n67:"):
            return f"N6701C выход {sel[4:]}"
    return dict(SUPPLIES).get(sel, sel)


def summary(s: dict, station=None) -> str:
    """One line describing a step (plan editor list, plan view, report)."""
    t = s.get("type")
    if s.get("title"):
        return s["title"]
    if t == "bos_cmd":
        name = bp.COMMANDS.get(s.get("code", 0), (f"0x{s.get('code', 0):02X}",))[0]
        p = cmd_params_text(s.get("code"), s.get("params", {}))
        exp = {"none": _L("no КВ", "без КВ")}.get(s.get("expect", "КВ+"), s.get("expect", "КВ+"))
        return f"{name}" + (f" ({p})" if p else "") + f" → {exp}" + (_L(" via CAN2", " по CAN2")
                                                                    if s.get("line") == 2 else "")
    if t == "can":
        c = None
        cw = getattr(getattr(station, "host", None), "can_win", None) if station else None
        if cw is not None and s.get("uid"):
            c = cw.store.by_uid(s["uid"])
        return f"CAN: {c.label() if c else s.get('uid') or _L('(not chosen)', '(не выбрана)')}"
    if t == "rf":
        m = s.get("mode")
        return _L(f"RF mode: {dict(RF_MODES).get(m, m)}", f"Режим ВЧ: {RF_RU.get(m, m)}") + \
            (_L(f", attenuation {s['att']} dB", f", ослабление {s['att']} дБ") if s.get("att") is not None else "")
    if t == "psu":
        sup = _sup(s.get("supply"))
        a = s.get("action")
        if a == "measure":
            lim = ", ".join(x for x in (_lim(s.get("v_min"), s.get("v_max"), " V"),
                                        _lim(s.get("i_min"), s.get("i_max"), " A")) if x)
            return f"{sup}: " + _L("measure V / I", "измерить U / I") + (f" ({lim})" if lim else "")
        if a in ("set", "set_on"):
            return f"{sup}: {s.get('v', 0):g} V, {s.get('i', 0):g} A" + (_L(", output ON", ", выход ВКЛ.")
                                                                         if a == "set_on" else "")
        return f"{sup}: " + (_L("output ON", "выход ВКЛ.") if a == "on" else _L("output OFF", "выход ВЫКЛ."))
    if t == "tlm":
        return (_L("Telemetry", "Телеметрия") + f" {s.get('key')} {_lim(s.get('min'), s.get('max'))}").rstrip()
    if t == "scpi":
        chk = {"number": " " + _lim(s.get("min"), s.get("max")),
               "text": _L(f" contains '{s.get('text', '')}'", f" содержит «{s.get('text', '')}»")}.get(s.get("check"), "")
        return f"{s.get('inst')}: {s.get('cmd')}{chk}"
    if t == "operator":
        txt = (s.get("text") or "").splitlines()[0][:60] if s.get("text") else _L("(no text)", "(нет текста)")
        m = {"confirm": "", "passfail": _L(" [Pass / Fail]", " [Норма / Не норма]"),
             "value": _L(" [value ", " [значение ") + f"{_lim(s.get('min'), s.get('max'), ' ' + s.get('unit', '')).strip()}]"}
        return _L("Operator: ", "Оператор: ") + f"{txt}{m.get(s.get('mode'), '')}"
    if t == "wait":
        return _L(f"Wait {s.get('s', 0):g} s", f"Пауза {s.get('s', 0):g} с")
    if t == "note":
        return f"— {s.get('text') or _L('note', 'примечание')} —"
    return str(t)


def item_name(item, station=None) -> str:
    """Name of a plan item as shown in the progress line, rows ('Test' column) and reports."""
    if is_step(item):
        return summary(item, station)
    from bos_station import TEST_NAME
    return TEST_NAME.get(item, item)


# ------------------------------------------------------------------------------- plan expansion
def expand(items, ctl, prep="auto"):
    """Items the runner executes: the plan plus the automatic preparation (reference & BOS power-up,
    CAN1 / CAN2 check, link setup) according to the plan's preparation option."""
    tests = list(items)
    builtin = [t for t in tests if not is_step(t)]
    if prep == "off" or tests in (["live"], ["file"]):
        return tests
    if prep == "auto" and not builtin:
        return tests
    proto = ctl.get("method") == "protocol"
    if proto and "link" not in tests:
        tests.insert(0, "link")
    if proto and ctl.get("can_check", True) and tests != ["link"] and "can12" not in tests:
        tests.insert(0, "can12")
    if tests != ["link"] and "ref" not in tests:
        tests.insert(0, "ref")
    return tests


def needs_fsw(items) -> bool:
    return any((not is_step(t) and t in FSW_TESTS) or (is_step(t) and t.get("type") == "scpi" and
                                                         t.get("inst") == "FSW") for t in items)


# ------------------------------------------------------------------------------- widgets
def _opt_spin(value, lo=-1e9, hi=1e9, dec=4, suffix=""):
    """(checkbox, spinbox): unchecked = no limit."""
    cb = QtWidgets.QCheckBox()
    sp = QtWidgets.QDoubleSpinBox()
    sp.setRange(lo, hi)
    sp.setDecimals(dec)
    sp.setSuffix(suffix)
    cb.setChecked(value is not None)
    sp.setValue(value if value is not None else 0.0)
    sp.setEnabled(value is not None)
    cb.toggled.connect(sp.setEnabled)
    w = QtWidgets.QWidget()
    h = QtWidgets.QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.addWidget(cb)
    h.addWidget(sp, 1)
    w.get = lambda: sp.value() if cb.isChecked() else None
    return w


def _combo(items, current):
    cb = QtWidgets.QComboBox()
    for k, txt in items:
        cb.addItem(txt, k)
    i = cb.findData(current)
    cb.setCurrentIndex(max(0, i))
    return cb


def _param_widget(key, value):
    label, kind, rng, suffix = PARAMS[key]
    if kind == "bool":
        w = QtWidgets.QCheckBox()
        w.setChecked(bool(value))
        w.get = w.isChecked
    elif kind == "int":
        w = QtWidgets.QSpinBox()
        w.setRange(*rng)
        w.setSuffix(suffix)
        w.setValue(int(value or 0))
        w.get = w.value
    elif kind == "float":
        w = QtWidgets.QDoubleSpinBox()
        w.setRange(*rng)
        w.setDecimals(3)
        w.setSuffix(suffix)
        w.setValue(float(value or 0))
        w.get = w.value
    elif kind == "choice":
        w = _combo(rng, value)
        w.get = w.currentData
    else:
        w = QtWidgets.QLineEdit(str(value or ""))
        w.get = lambda: w.text().strip()
    return w


def field_widget(f, value, changed):
    """Input for one 0102 data field; w.get() returns the parameter value; changed() on every edit."""
    vals = f.get("values")
    if vals and (f.get("strict") or len(vals) <= 8):
        cb = QtWidgets.QComboBox()
        for k, txt in vals.items():
            cb.addItem(f"{k} — {txt}", int(k, 0))
        cb.setCurrentIndex(max(0, cb.findData(value)))
        cb.currentIndexChanged.connect(changed)
        cb.get = cb.currentData
        return cb
    if "scale" in f or f.get("encoding"):
        sp = QtWidgets.QDoubleSpinBox()
        sp.setDecimals(4 if "scale" in f and f["scale"] < 0.1 else 1)
        sp.setRange(float(f.get("min", -1e9)), float(f.get("max", 1e9)))
        sp.setSingleStep(f.get("scale", 1.0))
        sp.setSuffix(" " + f.get("unit", "") if f.get("unit") else "")
        if "none" in f:
            w = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            none = QtWidgets.QCheckBox("no direction (0x80)")
            none.setChecked(value is None)
            sp.setEnabled(value is not None)
            none.toggled.connect(lambda on: sp.setEnabled(not on))
            none.toggled.connect(changed)
            sp.setValue(float(value or 0))
            sp.valueChanged.connect(changed)
            h.addWidget(sp, 1)
            h.addWidget(none)
            w.get = lambda: None if none.isChecked() else sp.value()
            return w
        sp.setValue(float(value or 0))
        sp.valueChanged.connect(changed)
        sp.get = sp.value
        return sp
    is_now = isinstance(value, str) and value.startswith("now")
    wide = int(f.get("len", 1)) > 1 or "parts" in f
    le = QtWidgets.QLineEdit(value if is_now else f"0x{int(value or 0):X}" if wide else str(int(value or 0)))
    timef = f["name"] in ("bshv", "seconds") and int(f.get("len", 1)) == 4
    le.setToolTip(f"{f.get('min', 0)} … {f.get('max', '')}  (decimal or 0x hex" +
                  (", 'now' / 'now+30' = current time (+ s) when sent" if timef else "") + ")")
    le.textChanged.connect(changed)

    def get():
        t = le.text().strip()
        return t.replace(" ", "") if t.startswith("now") else int(t, 0)
    le.get = get
    return le


class PlanParamsDialog(QtWidgets.QDialog):
    """Parameters of built-in tests for one plan: tick 'this plan' to override the station setting."""

    def __init__(self, parent, plan, base_ctl, over, keys, title=""):
        super().__init__(parent)
        self.setWindowTitle(f"Plan parameters — {plan}" + (f" — {title}" if title else ""))
        self.over = dict(over)
        self.keys = [k for k in keys if k in PARAMS]
        v = QtWidgets.QVBoxLayout(self)
        v.addWidget(QtWidgets.QLabel("Ticked parameters apply to this plan only; the others follow the station "
                                     "settings (Control / Reference pages)."))
        g = QtWidgets.QGridLayout()
        g.addWidget(QtWidgets.QLabel("<b>This plan</b>"), 0, 0)
        g.addWidget(QtWidgets.QLabel("<b>Parameter</b>"), 0, 1)
        g.addWidget(QtWidgets.QLabel("<b>Value</b>"), 0, 2)
        self.rows = {}
        for r, k in enumerate(self.keys, 1):
            on = QtWidgets.QCheckBox()
            on.setChecked(k in over)
            w = _param_widget(k, over.get(k, base_ctl.get(k)))
            w.setEnabled(k in over)
            on.toggled.connect(w.setEnabled)
            g.addWidget(on, r, 0)
            g.addWidget(QtWidgets.QLabel(PARAMS[k][0]), r, 1)
            g.addWidget(w, r, 2)
            self.rows[k] = (on, w)
        if not self.keys:
            g.addWidget(QtWidgets.QLabel("This test has no plan-specific parameters (its limits are on the Limits "
                                         "page)."), 1, 0, 1, 3)
        v.addLayout(g)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def value(self) -> dict:
        out = {k: v for k, v in self.over.items() if k not in self.rows}
        for k, (on, w) in self.rows.items():
            if on.isChecked():
                out[k] = w.get()
        return out


class StepDialog(QtWidgets.QDialog):
    """Edit one user-defined step."""

    def __init__(self, parent, step: dict, station=None):
        super().__init__(parent)
        self.st = station
        self.s = copy.deepcopy(step)
        t = self.s["type"]
        self.setWindowTitle(f"Step — {TYPES[t][0]}")
        self.setMinimumWidth(520)
        self.form = QtWidgets.QFormLayout(self)
        self.title = QtWidgets.QLineEdit(self.s.get("title", ""))
        self.title.setPlaceholderText("(automatic: " + summary({**self.s, "title": ""}, station) + ")")
        self.err = QtWidgets.QLabel()
        self.err.setStyleSheet("color:#ff6b6b;")
        self.err.setWordWrap(True)
        getattr(self, f"_build_{t}")()
        self.form.addRow("Title (report / log)", self.title)
        self.form.addRow(self.err)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self.form.addRow(bb)

    # ---- BOS command: parameters from the 0102 field definitions
    def _build_bos_cmd(self):
        self.cmd = QtWidgets.QComboBox()
        for c, d in bp.DEFS.items():
            self.cmd.addItem(f"0x{c:02X}  {d['mnemonic']} — {d['name']}", c)
        self.cmd.setCurrentIndex(max(0, self.cmd.findData(self.s.get("code", 0))))
        self.cmd.currentIndexChanged.connect(self._cmd_changed)
        self.preset = QtWidgets.QComboBox()
        self.preset.activated.connect(self._preset_chosen)
        self.pbox = QtWidgets.QWidget()
        self.pform = QtWidgets.QFormLayout(self.pbox)
        self.pform.setContentsMargins(0, 0, 0, 0)
        self.expect = _combo([("КВ+", "КВ+ (acknowledged)"), ("КВ-", "КВ- (negative acknowledgement expected)"),
                              ("none", "no acknowledgement (send only)")], self.s.get("expect", "КВ+"))
        self.line = _combo([(1, "CAN line 1"), (2, "CAN line 2")], self.s.get("line", 1))
        self.info = QtWidgets.QLabel()
        self.info.setWordWrap(True)
        self.frame = QtWidgets.QLabel()
        self.frame.setStyleSheet("font-family:'DejaVu Sans Mono',Consolas,monospace;")
        self.frame.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.form.addRow("Command", self.cmd)
        self.form.addRow("Preset", self.preset)
        self.form.addRow(self.info)
        self.form.addRow(self.pbox)
        self.form.addRow("Expected answer", self.expect)
        self.form.addRow("Send on", self.line)
        self.form.addRow("Frame", self.frame)
        self.pw = {}
        self._cmd_changed(keep=True)

    def _cmd_changed(self, *_, keep=False):
        code = self.cmd.currentData()
        d = bp.DEFS.get(code, {})
        params = self.s.get("params", {}) if keep else {}
        while self.pform.rowCount():
            self.pform.removeRow(0)
        self.pw = {}
        self.preset.clear()
        self.preset.addItem("(choose to fill the parameters)", None)
        for p in d.get("presets", []):
            self.preset.addItem(p["name"], p.get("params", {}))
        fields = d.get("fields", [])
        for f in fields:
            self.pw[f["name"]] = self._field_widget(f, params.get(f["name"], f.get("default")))
            lab = f["name"] + (f"  ({f['note']})" if f.get("note") else "")
            lbl = QtWidgets.QLabel(lab)
            lbl.setWordWrap(True)
            lbl.setMaximumWidth(300)
            self.pform.addRow(lbl, self.pw[f["name"]])
        if not fields:
            self.pform.addRow(QtWidgets.QLabel("no data field"))
        modes = "only in standby (ДЖР)" if d.get("modes") == "ДЖР" else "in any mode"
        recv = {"group": "group УПНГ+АФАР+БОС (0x0D)"}.get(d.get("receiver"), "BOS (0x29)")
        self.info.setText(f"§{d.get('section', '')}: executed {modes}; receiver {recv}; "
                          f"{'acknowledgement requested' if d.get('ack', True) else 'no acknowledgement'}."
                          + (f"\n{d['note']}" if d.get("note") else ""))
        if not keep:
            self.expect.setCurrentIndex(self.expect.findData("КВ-" if d.get("reserved") else
                                                             "КВ+" if d.get("ack", True) else "none"))
        self._update_frame()

    def _field_widget(self, f, value):
        return field_widget(f, value, self._update_frame)

    def _preset_chosen(self, i):
        p = self.preset.itemData(i)
        if p is None:
            return
        self.s["params"] = dict(p)
        self._cmd_changed(keep=True)

    def _params(self):
        return {k: w.get() for k, w in self.pw.items()}

    def _update_frame(self, *_):
        code = self.cmd.currentData()
        try:
            data = bp.encode(code, self._params())
            ack = self.expect.currentData() != "none" and bp.wants_ack(code)
            sender = (self.st.ctl.get("sender", bp.SENDER_DEFAULT) if self.st else bp.SENDER_DEFAULT)
            cid, pl = bp.command(code, sender, data, ack, bp.receiver_of(code))
            self.frame.setText(f"ID 0x{cid:08X}  [{len(pl)}] {pl.hex(' ').upper() or '—'}")
            self.err.setText("")
            return True
        except (ValueError, KeyError) as e:
            self.frame.setText("—")
            self.err.setText(str(e))
            return False

    # ---- CAN library frame
    def _build_can(self):
        self.can = QtWidgets.QComboBox()
        cw = getattr(getattr(self.st, "host", None), "can_win", None) if self.st else None
        for c in (cw.store.commands if cw is not None else []):
            self.can.addItem(f"{c.label()}  {c.id_str()}: {c.data_str()}", c.uid)
        if not self.can.count():
            self.can.addItem("(the CAN tab library is empty)", "")
        self.can.setCurrentIndex(max(0, self.can.findData(self.s.get("uid", ""))))
        self.form.addRow("Library command", self.can)
        self.form.addRow(QtWidgets.QLabel("Sent on the line stored with the command (or the Transmit panel's line). "
                                          "No acknowledgement is checked."))

    # ---- RF mode
    def _build_rf(self):
        self.mode = _combo(RF_MODES, self.s.get("mode", "cw"))
        self.att = _opt_spin(self.s.get("att"), 0, 31, 0, " dB")
        self.form.addRow("Mode", self.mode)
        self.form.addRow("Attenuation (Atten_EIRP)", self.att)
        self.form.addRow(QtWidgets.QLabel("Uses the station's control method; sessions are preceded by the 10 MHz "
                                          "reference check and target designation, as in the built-in tests."))

    # ---- power supply
    def _build_psu(self):
        self.sup = _combo(SUPPLIES, self.s.get("supply", "plan"))
        self.act = _combo(PSU_ACTIONS, self.s.get("action", "measure"))
        self.v = QtWidgets.QDoubleSpinBox()
        self.v.setRange(0, 150)
        self.v.setDecimals(3)
        self.v.setSuffix(" V")
        self.v.setValue(float(self.s.get("v", 14.0)))
        self.i = QtWidgets.QDoubleSpinBox()
        self.i.setRange(0, 20)
        self.i.setDecimals(3)
        self.i.setSuffix(" A")
        self.i.setValue(float(self.s.get("i", 1.5)))
        self.settle = QtWidgets.QDoubleSpinBox()
        self.settle.setRange(0, 600)
        self.settle.setSuffix(" s")
        self.settle.setValue(float(self.s.get("settle_s", 0.5)))
        self.vmin, self.vmax = _opt_spin(self.s.get("v_min"), 0, 150, 3, " V"), _opt_spin(self.s.get("v_max"), 0, 150, 3, " V")
        self.imin, self.imax = _opt_spin(self.s.get("i_min"), 0, 20, 4, " A"), _opt_spin(self.s.get("i_max"), 0, 20, 4, " A")
        for lab, w in (("Supply", self.sup), ("Action", self.act), ("Voltage", self.v), ("Current limit", self.i),
                       ("Settle before measuring", self.settle), ("V min", self.vmin), ("V max", self.vmax),
                       ("I min", self.imin), ("I max", self.imax)):
            self.form.addRow(lab, w)

        def upd():
            a = self.act.currentData()
            for w in (self.v, self.i):
                w.setEnabled(a in ("set", "set_on"))
            for w in (self.vmin, self.vmax, self.imin, self.imax, self.settle):
                w.setEnabled(a == "measure")
        self.act.currentIndexChanged.connect(upd)
        upd()

    # ---- telemetry
    def _build_tlm(self):
        self.key = QtWidgets.QComboBox()
        self.key.setEditable(True)
        keys = list(TLM_KEYS)
        for k in sorted(getattr(self.st, "tlm", {}) or {}):
            if k not in keys:
                keys.append(k)
        self.key.addItems(keys)
        self.key.setCurrentText(self.s.get("key", "Temp_DT1"))
        self.tmin, self.tmax = _opt_spin(self.s.get("min")), _opt_spin(self.s.get("max"))
        self.tout = QtWidgets.QDoubleSpinBox()
        self.tout.setRange(0.5, 120)
        self.tout.setSuffix(" s")
        self.tout.setValue(float(self.s.get("timeout_s", 3.0)))
        self.form.addRow("Telemetry value", self.key)
        self.form.addRow("Min", self.tmin)
        self.form.addRow("Max", self.tmax)
        self.form.addRow("Wait for it up to", self.tout)
        self.form.addRow(QtWidgets.QLabel("Flags (locked / OK = 1): set min = max = 1."))

    # ---- SCPI
    def _build_scpi(self):
        self.inst = _combo([("FSW", "R&S FSW (Spectrum Analyzer tab)"), ("TFG", "TFG6806 (TFG6806 tab)")],
                           self.s.get("inst", "FSW"))
        self.cmd_t = QtWidgets.QLineEdit(self.s.get("cmd", ""))
        self.cmd_t.setPlaceholderText("e.g. FREQ:CENT 37.75GHz  or  CALC:MARK1:Y?")
        self.check = _combo([("none", "no check (a query is logged)"), ("number", "numeric reply within limits"),
                             ("text", "reply contains text")], self.s.get("check", "none"))
        self.smin, self.smax = _opt_spin(self.s.get("min")), _opt_spin(self.s.get("max"))
        self.text = QtWidgets.QLineEdit(self.s.get("text", ""))
        for lab, w in (("Instrument", self.inst), ("Command / query", self.cmd_t), ("Check", self.check),
                       ("Min", self.smin), ("Max", self.smax), ("Expected text", self.text)):
            self.form.addRow(lab, w)

    # ---- operator
    def _build_operator(self):
        self.op_text = QtWidgets.QPlainTextEdit(self.s.get("text", ""))
        self.op_text.setPlaceholderText("What the operator must do or check, e.g. 'Connect the RF cable to "
                                        "the FSW input via the 30 dB attenuator'")
        self.op_text.setMinimumHeight(90)
        self.op_mode = _combo(OP_MODES, self.s.get("mode", "confirm"))
        self.unit = QtWidgets.QLineEdit(self.s.get("unit", ""))
        self.omin, self.omax = _opt_spin(self.s.get("min")), _opt_spin(self.s.get("max"))
        for lab, w in (("Instruction", self.op_text), ("Answer", self.op_mode), ("Unit", self.unit),
                       ("Min", self.omin), ("Max", self.omax)):
            self.form.addRow(lab, w)

    def _build_wait(self):
        self.wait = QtWidgets.QDoubleSpinBox()
        self.wait.setRange(0, 86400)
        self.wait.setDecimals(1)
        self.wait.setSuffix(" s")
        self.wait.setValue(float(self.s.get("s", 1.0)))
        self.form.addRow("Duration", self.wait)

    def _build_note(self):
        self.note = QtWidgets.QLineEdit(self.s.get("text", ""))
        self.form.addRow("Text", self.note)

    # ---- result
    def accept(self):
        t = self.s["type"]
        s = self.s
        s["title"] = self.title.text().strip()
        if t == "bos_cmd":
            if not self._update_frame():
                return
            s.update(code=self.cmd.currentData(), params=self._params(), expect=self.expect.currentData(),
                     line=self.line.currentData())
        elif t == "can":
            if not self.can.currentData():
                self.err.setText("choose a command (add it on the CAN tab first)")
                return
            s["uid"] = self.can.currentData()
        elif t == "rf":
            s.update(mode=self.mode.currentData(), att=None if self.att.get() is None else int(self.att.get()))
        elif t == "psu":
            s.update(supply=self.sup.currentData(), action=self.act.currentData(), v=self.v.value(),
                     i=self.i.value(), settle_s=self.settle.value(), v_min=self.vmin.get(), v_max=self.vmax.get(),
                     i_min=self.imin.get(), i_max=self.imax.get())
        elif t == "tlm":
            s.update(key=self.key.currentText().strip(), min=self.tmin.get(), max=self.tmax.get(),
                     timeout_s=self.tout.value())
        elif t == "scpi":
            if not self.cmd_t.text().strip():
                self.err.setText("enter a command")
                return
            s.update(inst=self.inst.currentData(), cmd=self.cmd_t.text().strip(), check=self.check.currentData(),
                     min=self.smin.get(), max=self.smax.get(), text=self.text.text())
        elif t == "operator":
            if not self.op_text.toPlainText().strip():
                self.err.setText("enter the instruction for the operator")
                return
            s.update(text=self.op_text.toPlainText().strip(), mode=self.op_mode.currentData(),
                     unit=self.unit.text().strip(), min=self.omin.get(), max=self.omax.get())
        elif t == "wait":
            s["s"] = self.wait.value()
        elif t == "note":
            s["text"] = self.note.text().strip()
        super().accept()

    def value(self) -> dict:
        return self.s


# ------------------------------------------------------------------- commands inside built-in tests
def cmd_key(tid: str, code: int, data: bytes = b"") -> str:
    """Identity of a BOS command inside a built-in test of a plan: test id + command code (+ the telemetry
    array type for SET_PeriodTM / REQ_TM, which are sent once per array). 'end' = the MODE_D after the plan."""
    k = f"{tid}:{code:02X}"
    if code in (0x10, 0x11) and data:
        k += f":{data[0]:02X}"
    return k


def apply_cmd(over: dict | None, code: int, data: bytes):
    """(send?, data) after a plan's command control: {"skip": True} = not sent, {"params": {...}} = these data
    fields replaced (the others stay as the test computed them)."""
    if not over:
        return True, data
    if over.get("skip"):
        return False, data
    p = over.get("params") or {}
    if p and data is not None:
        cur = bp.decode(code, data)
        cur.update(p)
        try:
            return True, bp.encode(code, cur)
        except (ValueError, KeyError):
            return True, data
    return True, data


class CmdDialog(QtWidgets.QDialog):
    """One BOS command of a built-in test, for one plan: send it or not, and replace any data field."""

    def __init__(self, parent, plan, key, code, data, test_name, over):
        super().__init__(parent)
        self.code, self.data0, self.key = code, bytes(data or b""), key
        d = bp.DEFS.get(code, {})
        self.setWindowTitle(f"Command in plan '{plan}'")
        self.setMinimumWidth(560)
        f = QtWidgets.QFormLayout(self)
        f.addRow(QtWidgets.QLabel(f"<b>{test_name}</b> — 0x{code:02X} {bp.COMMANDS.get(code, ('?',))[0]}"
                                  f"<br><span style='color:#8a96a3'>{d.get('name', '')}"
                                  f"{' · §' + d['section'] if d.get('section') else ''}</span>"))
        self.send = QtWidgets.QCheckBox("Send this command in this plan")
        self.send.setChecked(not (over or {}).get("skip"))
        self.send.setToolTip("Unticked: the test goes on without sending it (the log and the report say so).\n"
                             "A test that needs its answer (acknowledgement, telemetry) may then fail.")
        f.addRow(self.send)
        cur = bp.decode(code, self.data0) if self.data0 else {}
        po = dict((over or {}).get("params") or {})
        self.rows = {}
        box = QtWidgets.QGroupBox("Data fields — tick a field to replace the value the test computes")
        g = QtWidgets.QGridLayout(box)
        for r, fd in enumerate(d.get("fields", []) if self.data0 else []):
            on = QtWidgets.QCheckBox()
            on.setChecked(fd["name"] in po)
            w = field_widget(fd, po.get(fd["name"], cur.get(fd["name"], fd.get("default"))), self._update)
            w.setEnabled(on.isChecked())
            on.toggled.connect(w.setEnabled)
            on.toggled.connect(self._update)
            lab = QtWidgets.QLabel(fd["name"] + (f"  ({fd['note']})" if fd.get("note") else ""))
            lab.setWordWrap(True)
            lab.setMaximumWidth(280)
            g.addWidget(on, r, 0)
            g.addWidget(lab, r, 1)
            g.addWidget(w, r, 2)
            g.addWidget(QtWidgets.QLabel(f"test: {cur.get(fd['name'])}"), r, 3)
            self.rows[fd["name"]] = (on, w)
        if not self.rows:
            g.addWidget(QtWidgets.QLabel("no data field"), 0, 0)
        f.addRow(box)
        if d.get("encoder") == "session" or code in (0x0C,):
            f.addRow(QtWidgets.QLabel("The test changes this command's data while it runs (mode, attenuation, "
                                      "frequency); ticked fields are fixed for every time it is sent."))
        mono = "font-family:'DejaVu Sans Mono',Consolas,monospace;"
        self.f0 = QtWidgets.QLabel(f"[{len(self.data0)}] {self.data0.hex(' ').upper() or '—'}")
        self.f1 = QtWidgets.QLabel()
        for lab in (self.f0, self.f1):
            lab.setStyleSheet(mono)
        f.addRow("Test sends", self.f0)
        f.addRow("This plan sends", self.f1)
        self.err = QtWidgets.QLabel()
        self.err.setStyleSheet("color:#ff6b6b;")
        f.addRow(self.err)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        self.b_reset = bb.addButton("Restore original", QtWidgets.QDialogButtonBox.ResetRole)
        self.b_reset.clicked.connect(self._reset)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.send.toggled.connect(self._update)
        self._reset_flag = False
        self._update()

    def _params(self):
        return {k: w.get() for k, (on, w) in self.rows.items() if on.isChecked()}

    def _update(self, *_):
        if not self.send.isChecked():
            self.f1.setText("— not sent —")
            self.err.setText("")
            return True
        try:
            p = bp.decode(self.code, self.data0) if self.data0 else {}
            p.update(self._params())
            data = bp.encode(self.code, p) if self.data0 else b""
            self.f1.setText(f"[{len(data)}] {data.hex(' ').upper() or '—'}")
            self.err.setText("")
            return True
        except (ValueError, KeyError) as e:
            self.f1.setText("—")
            self.err.setText(str(e))
            return False

    def _reset(self):
        self._reset_flag = True
        self.accept()

    def accept(self):
        if not self._reset_flag and not self._update():
            return
        super().accept()

    def value(self):
        """None = original (no entry), else {"skip": bool, "params": {...}}."""
        if self._reset_flag:
            return None
        out = {}
        if not self.send.isChecked():
            out["skip"] = True
        p = self._params()
        if p:
            out["params"] = p
        return out or None


def add_step_menu(parent, on_pick, tests=None) -> QtWidgets.QMenu:
    """Menu: built-in tests (sub-menu) + every step type; on_pick(item) gets the new item."""
    m = QtWidgets.QMenu(parent)
    if tests:
        sub = m.addMenu("Built-in test")
        for tid, name, desc in tests:
            a = sub.addAction(name)
            a.setToolTip(desc)
            a.triggered.connect(lambda _=False, t=tid: on_pick(t))
        m.addSeparator()
    for k, (name, _) in TYPES.items():
        m.addAction(name, lambda k=k: on_pick(new_step(k)))
    return m


class PlanEditor(QtWidgets.QDialog):
    """Name, preparation, items (built-in tests and user steps) with add / edit / move / delete, parameters."""

    def __init__(self, parent, station, name, items, opts, tests):
        super().__init__(parent)
        self.st = station
        self.tests = tests
        self.names = {t: n for t, n, _ in tests}
        self.items = copy.deepcopy(list(items))
        self.opts = copy.deepcopy(opts or {})
        self.setWindowTitle("Test plan")
        self.resize(760, 560)
        v = QtWidgets.QVBoxLayout(self)
        f = QtWidgets.QFormLayout()
        self.name = QtWidgets.QLineEdit(name)
        f.addRow("Name", self.name)
        self.prep = _combo([("auto", "Automatic: before built-in BOS tests only"),
                            ("on", "Always (reference & power-up, CAN lines, link setup)"),
                            ("off", "Never (the plan does everything itself)")], self.opts.get("prep", "auto"))
        self.prep.setToolTip("Reference & BOS power-up, CAN1 / CAN2 check and link setup run before the plan.\n"
                             "'Automatic' adds them when the plan has built-in BOS tests; a plan of supply,\n"
                             "operator or PC steps then runs without them (and without the FSW).")
        f.addRow("Preparation", self.prep)
        self.k70 = QtWidgets.QCheckBox("Also measure BPSK / OQPSK with the FSW-K70 (VSA) when it is installed")
        self.k70.setChecked(bool(self.opts.get("k70", False)))
        self.k70.setToolTip("After the software demodulation of each modulation test the FSW-K70 measures the same "
                            "signal:\nits Tx filter is set to RRC with the station's roll-off (OQPSK) / off (SP-L "
                            "BPSK), the measurement filter to auto,\nboth read back and checked; MER, EVM, frequency, "
                            "symbol rate, I/Q offset, gain imbalance and quadrature error\nare reported next to the "
                            "software values. Skipped (SKIP row) when the FSW has no K70.")
        f.addRow("FSW-K70", self.k70)
        v.addLayout(f)
        h = QtWidgets.QHBoxLayout()
        self.list = QtWidgets.QListWidget()
        self.list.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.list.model().rowsMoved.connect(self._moved)
        self.list.itemDoubleClicked.connect(lambda *_: self.edit())
        h.addWidget(self.list, 1)
        bv = QtWidgets.QVBoxLayout()
        b_add = QtWidgets.QPushButton("+ Add ▾")
        b_add.setMenu(add_step_menu(b_add, self._add, tests))
        bv.addWidget(b_add)
        for txt, fn in (("Edit…", self.edit), ("Duplicate", self.dup), ("Delete", self.delete),
                        ("▲ Up", lambda: self.move(-1)), ("▼ Down", lambda: self.move(1))):
            b = QtWidgets.QPushButton(txt)
            b.clicked.connect(fn)
            bv.addWidget(b)
        bv.addSpacing(12)
        b_par = QtWidgets.QPushButton("Parameters…")
        b_par.setToolTip("Built-in test parameters (frequency code, rates, session command, Time_SNR, reference …)\n"
                         "for this plan only")
        b_par.clicked.connect(self.params)
        bv.addWidget(b_par)
        bv.addStretch(1)
        h.addLayout(bv)
        v.addLayout(h, 1)
        self.hint = QtWidgets.QLabel("Double-click a step to edit it; drag to reorder. Built-in tests: Edit… opens "
                                     "their parameters for this plan.")
        self.hint.setWordWrap(True)
        v.addWidget(self.hint)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._fill()

    def _text(self, it):
        if is_step(it):
            return f"[{TYPES[it['type']][0]}]  {summary(it, self.st)}"
        return f"[Built-in test]  {self.names.get(it, it)}"

    def _fill(self, sel=None):
        self.list.clear()
        for k, it in enumerate(self.items):
            self.list.addItem(f"{k + 1}. {self._text(it)}")
        if sel is not None and 0 <= sel < len(self.items):
            self.list.setCurrentRow(sel)

    def _moved(self, *_):
        order = [int(self.list.item(r).text().split(".")[0]) - 1 for r in range(self.list.count())]
        self.items = [self.items[k] for k in order]
        QtCore.QTimer.singleShot(0, lambda: self._fill(self.list.currentRow()))

    def _row(self):
        return self.list.currentRow()

    def _add(self, item):
        if is_step(item):
            dlg = StepDialog(self, item, self.st)
            if dlg.exec_() != QtWidgets.QDialog.Accepted:
                return
            item = dlg.value()
        r = self._row()
        pos = r + 1 if r >= 0 else len(self.items)
        self.items.insert(pos, item)
        self._fill(pos)

    def edit(self):
        r = self._row()
        if r < 0:
            return
        it = self.items[r]
        if is_step(it):
            dlg = StepDialog(self, it, self.st)
            if dlg.exec_() == QtWidgets.QDialog.Accepted:
                self.items[r] = dlg.value()
                self._fill(r)
        else:
            self.params(test_params(it), self.names.get(it, it))

    def params(self, keys=None, title=""):
        keys = keys or list(PARAMS)
        dlg = PlanParamsDialog(self, self.name.text().strip(), self.st.data["ctl"] if self.st else {},
                               self.opts.get("ctl", {}), keys, title)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            self.opts["ctl"] = dlg.value()

    def dup(self):
        r = self._row()
        if r >= 0:
            self.items.insert(r + 1, copy.deepcopy(self.items[r]))
            self._fill(r + 1)

    def delete(self):
        r = self._row()
        if r >= 0:
            del self.items[r]
            self._fill(min(r, len(self.items) - 1))

    def move(self, d):
        r = self._row()
        if r < 0 or not 0 <= r + d < len(self.items):
            return
        self.items[r], self.items[r + d] = self.items[r + d], self.items[r]
        self._fill(r + d)

    def accept(self):
        if not self.name.text().strip():
            QtWidgets.QMessageBox.warning(self, "Test plan", "Enter a plan name.")
            return
        if not self.items:
            QtWidgets.QMessageBox.warning(self, "Test plan", "Add at least one step or test.")
            return
        self.opts["prep"] = self.prep.currentData()
        self.opts["k70"] = self.k70.isChecked()
        super().accept()

    def value(self):
        return self.name.text().strip(), self.items, self.opts
