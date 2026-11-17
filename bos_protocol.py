# -*- coding: utf-8 -*-
"""
bos_protocol.py
===============
BOS control protocol over CAN 2.0B (ПИЛВ № 0102-004, part 1). Pure Python, no GUI.

29-bit identifier
    bit 28      C/D: 0 = command / acknowledgement, 1 = data array
    bits 27-22  sender address (AO)          bits 21-16  receiver address (AP)
    commands:   bit 15 = 0 command / 1 acknowledgement, bit 14 = acknowledgement request (in an
                acknowledgement: 1 = negative), bit 13 = command has a data field, bits 12-8 = 0,
                bits 7-0 = command code
    arrays:     bits 15-12 start 0000 / current 0100 / stop 1000 / stop + ack 1100,
                bits 11-8 packet counter, bits 7-0 array number
Addresses (table 3.1 / 3.2): BOS 0x29; senders БИОС 1.1-2.2 0x1C-0x1F, ВМ 1-2 0x20-0x21, БЭС 0x16,
КПА ИВУ 0x18, УПНГ 0x2A, АФАР 0x2B; group УПНГ+АФАР+БОС 0x0D (target designation), telemetry
from the BOS goes to БИОС+ВМ 0x06 (-> the 0x1A46xxxx prefix).
Acknowledgement (4.3): sender 0x29, receiver = command sender, bit 15 = 1, bit 14 = 1 negative,
data[0..4] = command data[0..4], data[5] = acknowledgement byte, data[6..7] = array packets.
Data are little-endian. CRC-16 (CCITT, 0x1021, init 0xFFFF) protects МД_КОНФИГ (bytes 134-135).
"""

from __future__ import annotations

BOS = 0x29
GROUP_ANGLE = 0x0D            # УПНГ+АФАР+БОС: target designation commands, no acknowledgement
SENDER_DEFAULT = 0x21          # station sender address (АО): ВМ 2 -> command IDs 0x0869xxxx (BOS 0x29)
SENDERS = [  # address, name
    (0x21, "ВМ 2"), (0x20, "ВМ 1"), (0x18, "КПА ИВУ (test equipment)"),
    (0x1C, "БИОС 1.1"), (0x1D, "БИОС 1.2"), (0x1E, "БИОС 2.1"), (0x1F, "БИОС 2.2"), (0x16, "БЭС"),
]

# code: (mnemonic, data bytes, description). Built-in fallback; the full list (tables 4.6 / 4.7 / 4.8 with the
# data-field layouts of tables 4.9-4.35) is loaded from bos_commands_0102.json below.
COMMANDS = {
    0x00: ("NOP", 0, "Empty command (link check, acknowledged)"),
    0x01: ("MODE_D", 0, "Standby mode (ДЖР): ends any session, RF off"),
    0x02: ("SET_Sour_10MHz", 1, "10 MHz reference source (ДЖР only)"),
    0x03: ("SET_Data_Randomiz", 1, "Randomisation in ШР/КБД (ДЖР only)"),
    0x04: ("SET_Time_SNR", 1, "Session duration limit, s (ДЖР only)"),
    0x06: ("SET_CAN", 1, "CAN buses for transmission (ДЖР only)"),
    0x0C: ("Angle_ErdStation_BKU", 2, "Target designation to the ground station from the БКУ"),
    0x0D: ("SET_Angle_ErdStation_Err", 1, "Permissible target designation angle (ДЖР only)"),
    0x0E: ("SAVE_CONFIG_NEW", 0, "Save settings in the КОНФИГ file"),
    0x10: ("SET_Period_TM", 8, "Telemetry output period"),
    0x11: ("REQ_TM", 2, "Request telemetry now"),
    0x17: ("SET_Temp_mode", 1, "Over-temperature shutdown mode"),
    0x19: ("Park", 0, "Data parking"),
    0xA0: ("Send_Carrier", 8, "Session: transmit carrier"),
    0xA1: ("Send_UB_Data", 8, "Session: narrowband (УР) КБД"),
    0xA2: ("Send_Preamb", 8, "Session: wideband (ШР) preambles"),
    0xA3: ("Send_SNR_Sequens", 8, "Session: ШР ПООСШ (SNR sequence)"),
    0xA4: ("Send_KBD", 8, "Session: ШР КБД"),
}
SR_COMMANDS = {0xA0: "Carrier", 0xA1: "УР КБД", 0xA2: "ШР preambles", 0xA3: "ШР ПООСШ", 0xA4: "ШР КБД"}
TM_TYPES = {0xFC: "ИОК_ОСН", 0xFB: "ИОК_ДОП", 0xFD: "ИОК_СОСТОЯНИЕ", 0xFA: "ИОК_КОНФИГ", 0xF9: "ИОК_АРХИВ"}
RATES = {0: 8, 1: 16, 2: 32, 3: 64, 4: 128, 5: 256, 6: 512, 7: 1024}     # kbit/s
F0_MHZ, F_STEP_MHZ = 37650.0, 0.125                                        # Freq = 37650 + 0.125·N MHz
FREQ_N_RANGE = {0xA0: (8, 1592), 0xA1: (8, 1592), 0xA2: (656, 944), 0xA3: (656, 944), 0xA4: (656, 944)}
ATTEN_MAX_DB = 20                                                          # Atten_EIRP saturates above 20 dB
T_KVIT = 0.05                                                              # acknowledgement delay < 50 ms
T_ANGLE_VALID = 3.0                                                        # target designation valid for 3 s


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


def freq_hz(n: int) -> float:
    return (F0_MHZ + F_STEP_MHZ * n) * 1e6


def cmd_id(code: int, sender: int, receiver: int = BOS, ack: bool = True, has_data: bool = True) -> int:
    return ((sender & 0x3F) << 22) | ((receiver & 0x3F) << 16) | (int(ack) << 14) | (int(has_data) << 13) | (code & 0xFF)


def command(code: int, sender: int, data: bytes = b"", ack: bool = True, receiver: int = BOS):
    """(can_id, data) of a command frame. Commands with parameters always carry 8 bytes. A command with a fixed
    sender in the library (Angle_ErdStation_BKU: 0x1C БИОС 1.1 -> 0x070D200C) always uses it."""
    fixed = fixed_sender(code)
    if fixed is not None:
        sender = fixed
    n = COMMANDS.get(code, ("", 8, ""))[1]
    payload = (bytes(data) + bytes(8))[:8] if n else b""
    return cmd_id(code, sender, receiver, ack, bool(n)), payload


# ---- parameter builders (tables 4.9 - 4.20, 4.35) ----------------------------------------------
def p_byte(v: int) -> bytes:
    return bytes([v & 0xFF])


def p_period_tm(tm_type: int, seconds: float) -> bytes:
    """SET_Period_TM: type, TM_EXP 0, 2 reserve, period in 1/16 s (0 = off), 2 reserve."""
    v = max(0, min(0x0FFF, int(round(seconds * 16))))
    return bytes([tm_type, 0, 0, 0, v & 0xFF, v >> 8, 0, 0])


def p_req_tm(tm_type: int) -> bytes:
    return bytes([tm_type, 0])


def angle_code(deg: float) -> int:
    """0.2° per LSB, two's complement, ±25°; 0x80 = no data / out of range."""
    if abs(deg) > 25:
        return 0x80
    return int(round(deg / 0.2)) & 0xFF


def p_angle(angx: float = 0.0, angy: float = 0.0) -> bytes:
    return bytes([angle_code(angx), angle_code(angy)])


def p_session(code: int, freq_n: int, atten_db: int = 0, coding: int = 0, polyn1: int = 0, polyn2: int = 0,
              continuous: bool = True, rate: int = 0, count: int = 2047, reg: int = 0) -> bytes:
    """Session (СР) command parameters, table 4.35.
    byte 0 + byte 1 bits 2-0: Freq N (11 bit), byte 1 bits 7-3: Atten_EIRP (1 dB),
    byte 2: Mode_Coding (7-6), PolynX.1 / preamble polynomial (5-3), PolynX.2 (2-0),
    byte 3: bit 0 Mode_Send (0 = continuous NeprRP, 1 = pulse ImpRP), bits 4-1 Rate,
    byte 3 bits 7-5 + byte 4: N (КБД count / preambles / ПООСШ length / carrier time in s, 11 bit),
    bytes 5-7: register start value / number of idle frames (24 bit)."""
    lo, hi = FREQ_N_RANGE.get(code, (0, 0x7FF))
    if not lo <= freq_n <= hi:
        raise ValueError(f"frequency code {freq_n} outside {lo}…{hi} for {COMMANDS[code][0]}")
    a = max(0, min(31, int(atten_db)))
    b = bytearray(8)
    b[0] = freq_n & 0xFF
    b[1] = ((freq_n >> 8) & 0x07) | (a << 3)
    b[2] = ((coding & 3) << 6) | ((polyn1 & 7) << 3) | (polyn2 & 7)
    count = max(0, min(2047, int(count)))
    b[3] = (0 if continuous else 1) | ((rate & 0x7) << 1) | ((count & 0x7) << 5)
    b[4] = count >> 3
    b[5:8] = (reg & 0xFFFFFF).to_bytes(3, "little")
    return bytes(b)


KBD_UR_BITS = 10232            # КБД in УР: 1279 bytes (marker + code block + check symbols)
KBD_SR_BITS = 17912            # КБД in ШР: 2239 bytes (turbo coded, bos_shr.CBD_SR_BITS)
PREAMB_BITS = 32 * 4           # one ШР preamble: 32 bits, 4 × the chips of a data bit


def session_seconds(code: int, count: int, rate: int, n_pre: int = 5):
    """Estimated length of a session (СР) from its command parameters (without Time_SNR); None = unknown.
    Carrier: count = seconds; УР КБД: count КБД at the УР rate; ШР: count preambles / КБД (+ n_pre preambles)."""
    bps = RATES.get(int(rate), 8) * 1e3
    if code == 0xA0:
        return float(count)
    if code in (0xA1, 0xAD, 0xAE):
        return count * KBD_UR_BITS / bps
    if code in (0xA2, 0xA5):
        return count * PREAMB_BITS / bps
    if code in (0xA4, 0xA7, 0xAF):
        return (n_pre * PREAMB_BITS + count * KBD_SR_BITS) / bps
    return None


def parse_session(data: bytes) -> dict:
    d = bytes(data) + bytes(8)
    return {"freq_n": d[0] | (d[1] & 7) << 8, "atten_db": d[1] >> 3, "coding": d[2] >> 6,
            "polyn1": (d[2] >> 3) & 7, "polyn2": d[2] & 7, "continuous": not (d[3] & 1),
            "rate": (d[3] >> 1) & 7, "count": (d[3] >> 5) | (d[4] << 3), "reg": int.from_bytes(d[5:8], "little")}


# ---- acknowledgement ---------------------------------------------------------------------------
def parse_ack(can_id: int, ext: bool, data: bytes, sender: int):
    """Acknowledgement from the BOS to `sender`, or None."""
    if not ext or can_id >> 28 & 1 or (can_id >> 22) & 0x3F != BOS or (can_id >> 16) & 0x3F != sender:
        return None
    if not can_id >> 15 & 1:
        return None
    d = bytes(data) + bytes(8)
    kb = d[5]
    return {"code": can_id & 0xFF, "negative": bool(can_id >> 14 & 1), "byte": kb, "can2": bool(kb >> 7 & 1),
            "array": bool(kb >> 6 & 1), "unknown": bool(kb >> 3 & 1), "echo": d[:5]}


def ack_text(a: dict) -> str:
    name = COMMANDS.get(a["code"], (f"0x{a['code']:02X}",))[0]
    if not a["negative"]:
        return f"{name}: КВ+ (via CAN{2 if a['can2'] else 1})"
    why = "command does not exist" if a["unknown"] else "sender address not allowed / command not in the list"
    return f"{name}: КВ- ({why})"


# ---- КОНФИГ (table 4.4), 136 bytes; ИОК_КОНФИГ = 8-byte header + КОНФИГ ---------------------------
CONFIG_LEN = 136


def decode_config(cfg: bytes) -> dict:
    c = bytes(cfg)
    if len(c) < CONFIG_LEN:
        raise ValueError(f"КОНФИГ must be {CONFIG_LEN} bytes, got {len(c)}")
    u32 = lambda i: int.from_bytes(c[i:i + 4], "little")
    s8 = lambda v: v - 256 if v > 127 else v
    tm = (c[3] >> 5) & 7
    d = {
        "Config_Number": c[0], "Config_Nmb_Cmd": c[1], "Config_Type": c[2] >> 7 & 1,
        "Sour_10MHz": c[2] >> 6 & 1, "Data_Random": c[2] >> 5 & 1, "BPU_AFAR_mode": c[2] >> 2 & 3,
        "QuickOn_mode": c[2] & 3, "Temp_mode": tm, "Sour_Angle": c[3] >> 2 & 3, "Numb_BOS": c[3] >> 1 & 1,
        "Time_SNR": c[4], "Angle_ErdStation_Err": c[5], "Numb_Preamb": c[6] + 1,
        "ID_KA": int.from_bytes(c[7:9], "little") & 0x3FF, "Preamb": u32(9), "Registr_Preamb": u32(13),
        "Registr1_KBD": u32(49), "Registr2_KBD": u32(53),
        "Ttlm_main_FC": c[121], "Ttlm_dop_FB": c[122], "Ttlm_stat_FD": c[123], "Ttlm_conf_FA": c[124],
        "Temp_Max_DT1": s8(c[125]), "Temp_Max_DT2": s8(c[126]), "Temp_Max_DT3": s8(c[127]),
        "Checksum": int.from_bytes(c[134:136], "little"),
    }
    d["Polyn_Preamb"] = [u32(17 + 4 * k) for k in range(8)]
    d["Polyn_KBD"] = [(u32(57 + 8 * k), u32(61 + 8 * k)) for k in range(8)]
    calc = crc16(c[:134])
    d["crc_calc"] = calc
    d["crc_ok"] = calc == d["Checksum"] or crc16(c[:134]) == int.from_bytes(c[134:136], "big")
    return d


# ---- МД_СОСТОЯНИЕ (40 bytes) -----------------------------------------------------------------------
STATUS_FIELDS = [(0, 2, "Cnt_Config_Save"), (2, 2, "Cnt_Data_Save"), (4, 2, "Cnt_Alarm_Temp_All"),
                 (6, 2, "Cnt_Turn_ON"), (8, 3, "Cnt_SNR_Cmd_All"), (11, 3, "Cnt_SNR_Sgnl_All"),
                 (14, 3, "Cnt_SNR_BB_Sgnl_All"), (17, 4, "Cnt_TX_KBD_All"), (21, 4, "Cnt_TX_KBD_BB_All"),
                 (25, 4, "Work_Time"), (29, 4, "T_SNR_All"), (33, 4, "T_SNR_BB_All")]


# ---- command library from the 0102 document (bos_commands_0102.json) ------------------------------
import json as _json
import os as _os
import time as _time

def _library_path() -> str:
    """bos_commands_0102.json next to the program (editable copy beside KTR_TEST.exe wins), else the bundled one."""
    import sys
    here = _os.path.dirname(_os.path.abspath(__file__))
    dirs = ([_os.path.dirname(sys.executable)] if getattr(sys, "frozen", False) else []) + [here]
    for d in dirs:
        f = _os.path.join(d, "bos_commands_0102.json")
        if _os.path.exists(f):
            return f
    return _os.path.join(here, "bos_commands_0102.json")


LIBRARY_FILE = _library_path()
DEFS: dict[int, dict] = {}          # code -> command definition (to the BOS)
FROM_BOS: dict[int, dict] = {}      # code -> commands the BOS sends (table 4.8)


def load_library(path: str = LIBRARY_FILE) -> int:
    """Load the 0102 command definitions; COMMANDS gains every command of tables 4.6 / 4.7.
    Returns the number of commands to the BOS (0 if the file is missing; the built-in list stays)."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = _json.load(f)
    except (OSError, ValueError):
        return 0
    DEFS.clear()
    FROM_BOS.clear()
    for d in doc.get("commands", []):
        code = int(d["code"], 16)
        if d.get("direction") == "from_bos":
            FROM_BOS[code] = d
            continue
        DEFS[code] = d
        nb = 8 if d.get("encoder") == "session" else int(d.get("bytes", 0))
        COMMANDS[code] = (d["mnemonic"], nb, d["name"])
    for code in [c for c in COMMANDS if c not in DEFS and DEFS]:
        COMMANDS.pop(code)
    return len(DEFS)


def group_of(code: int) -> str:
    return DEFS.get(code, {}).get("group", "basic")


def code_of(name_or_code) -> int:
    """Command code from a code (int / '0x..') or a mnemonic (case-insensitive)."""
    if isinstance(name_or_code, int):
        return name_or_code
    t = str(name_or_code).strip()
    if t.lower().startswith("0x"):
        return int(t, 16)
    for c, v in COMMANDS.items():
        if v[0].lower() == t.lower():
            return c
    raise KeyError(f"unknown BOS command '{name_or_code}'")


def _field_width(f) -> int:
    if "parts" in f:
        return sum(hi - lo + 1 for _, hi, lo in f["parts"])
    return 8 * int(f.get("len", 1)) if "bits" not in f else f["bits"][0] - f["bits"][1] + 1


def _get_raw(f, b) -> int:
    """Raw value of a field from the 8 data bytes."""
    if "parts" in f:
        r, sh = 0, 0
        for i, hi, lo in f["parts"]:
            w = hi - lo + 1
            r |= ((b[i] >> lo) & ((1 << w) - 1)) << sh
            sh += w
        return r
    i = f["byte"]
    if "bits" in f:
        hi, lo = f["bits"]
        return (b[i] >> lo) & ((1 << (hi - lo + 1)) - 1)
    return int.from_bytes(b[i:i + int(f.get("len", 1))], "little")


def _put_raw(f, b: bytearray, r: int):
    if "parts" in f:
        for i, hi, lo in f["parts"]:
            w = hi - lo + 1
            mask = ((1 << w) - 1) << lo
            b[i] = (b[i] & ~mask) | (((r & ((1 << w) - 1)) << lo) & mask)
            r >>= w
        return
    i = f["byte"]
    if "bits" in f:
        hi, lo = f["bits"]
        mask = ((1 << (hi - lo + 1)) - 1) << lo
        b[i] = (b[i] & ~mask) | ((r << lo) & mask)
    else:
        n = int(f.get("len", 1))
        b[i:i + n] = int(r).to_bytes(n, "little")


def _raw_value(f, v):
    """Parameter value (engineering units) -> raw field value; ValueError when outside the document range."""
    name = f["name"]
    if v is None:
        if "none" in f:
            return f["none"]
        raise ValueError(f"{name}: value required")
    if isinstance(v, str) and v.replace(" ", "").startswith("now"):
        t = v.replace(" ", "")[3:]
        v = int(_time.time()) + (int(t) if t else 0)          # 'now', 'now+30', 'now-5'
    elif isinstance(v, str):
        v = float(v) if "." in v else int(v, 0)
    if f.get("encoding") == "signmag_minus1":
        v = int(round(v))
        if not f.get("min", -128) <= v <= f.get("max", 127):
            raise ValueError(f"{name}={v} outside {f.get('min')}…{f.get('max')}")
        return v if v >= 0 else 0x80 | (-v - 1)
    if "scale" in f:
        if "min" in f and not f["min"] - 1e-9 <= v <= f["max"] + 1e-9:
            raise ValueError(f"{name}={v:g} {f.get('unit', '')} outside {f['min']:g}…{f['max']:g}")
        r = int(round(v / f["scale"]))
        return r & ((1 << _field_width(f)) - 1) if f.get("signed") else r
    v = int(v)
    if "values" in f and f.get("strict") and str(v) not in f["values"] and f"0x{v:02X}" not in f["values"]:
        raise ValueError(f"{name}={v:#x} not allowed ({', '.join(f['values'])})")
    if "min" in f and not f["min"] <= v <= f["max"]:
        raise ValueError(f"{name}={v:#x} outside {f['min']:#x}…{f['max']:#x}")
    if v >> _field_width(f):
        raise ValueError(f"{name}={v:#x} does not fit {_field_width(f)} bits")
    return v


def encode(name_or_code, params: dict | None = None) -> bytes:
    """Data field (8 bytes, or b'' for a command without data) of a 0102 command from its parameters.
    Missing parameters take the field default (or 0). Raises ValueError for values the document prohibits."""
    code = code_of(name_or_code)
    d = DEFS.get(code)
    params = dict(params or {})
    if d is None:
        raise KeyError(f"command 0x{code:02X} not in {_os.path.basename(LIBRARY_FILE)}")
    if d.get("encoder") == "session":
        lo, hi = d.get("freq_n_range", (0, 0x7FF))
        n = int(params.get("freq_n", 800))
        if not lo <= n <= hi:
            raise ValueError(f"freq_n={n} outside {lo}…{hi} for {d['mnemonic']}")
        kw = {k: params[k] for k in ("atten_db", "coding", "polyn1", "polyn2", "rate", "count", "reg") if k in params}
        if "continuous" in params:
            kw["continuous"] = bool(int(params["continuous"]))
        FREQ_N_RANGE.setdefault(code, (lo, hi))
        return p_session(code, n, **kw)
    if not d.get("bytes") and not d.get("fields"):
        return b""
    b = bytearray(8)
    for f in d.get("fields", []):
        r = _raw_value(f, params.get(f["name"], f.get("default", 0)))
        _put_raw(f, b, int(r))
    err = validate(code, bytes(b))
    if err:
        raise ValueError(err)
    return bytes(b)


def validate(name_or_code, data: bytes) -> str:
    """'' when the data field is acceptable to the BOS, otherwise why the command would be ignored."""
    code = code_of(name_or_code)
    d = DEFS.get(code)
    if d is None:
        return ""
    if d.get("encoder") == "session":
        p = parse_session(data)
        lo, hi = d.get("freq_n_range", (0, 0x7FF))
        return "" if lo <= p["freq_n"] <= hi else f"freq_n={p['freq_n']} outside {lo}…{hi}"
    b = bytes(data) + bytes(8)
    for z in d.get("zero_bits", []):
        hi_, lo_ = z["bits"]
        if (b[z["byte"]] >> lo_) & ((1 << (hi_ - lo_ + 1)) - 1):
            return f"byte {z['byte']} bits {hi_}-{lo_} must be 0"
    vals = {}
    for f in d.get("fields", []):
        r = _get_raw(f, b)
        vals[f["name"]] = r
        if "none" in f and r == f["none"]:
            continue
        if f.get("strict") and "values" in f and str(r) not in f["values"] and f"0x{r:02X}" not in f["values"]:
            return f"{f['name']}={r:#x} prohibited"
        if "scale" not in f and "encoding" not in f and "min" in f and not f["min"] <= r <= f["max"]:
            return f"{f['name']}={r:#x} outside {f['min']:#x}…{f['max']:#x}"
        if "scale" in f and not f.get("signed") and "min" in f and r * f["scale"] > f["max"] + 1e-9:
            return f"{f['name']}={r * f['scale']:g} above {f['max']:g}"
        if "scale" in f and f.get("signed"):
            sv = (r - 256 if r & 0x80 else r) * f["scale"]
            if not f["min"] - 1e-9 <= sv <= f["max"] + 1e-9:
                return f"{f['name']}={sv:g} outside {f['min']:g}…{f['max']:g}"
    for rule in d.get("rules", []):
        kind, _, names = rule.partition(":")
        if kind == "not_both_zero" and all(vals.get(n) == 0 for n in names.split(",")):
            return f"{names.replace(',', ' and ')} both 0 (prohibited)"
    return ""


def decode(name_or_code, data: bytes) -> dict:
    """Parameters (engineering units, as encode() takes them) of a data field - the inverse of encode()."""
    code = code_of(name_or_code)
    d = DEFS.get(code)
    if d is None or not d.get("fields"):
        return {}
    if d.get("encoder") == "session":
        p = parse_session(data)
        p["continuous"] = int(p["continuous"])
        return p
    b = bytes(data) + bytes(8)
    out = {}
    for f in d["fields"]:
        r = _get_raw(f, b)
        if "none" in f and r == f["none"]:
            v = None
        elif f.get("encoding") == "signmag_minus1":
            v = r if r < 0x80 else -((r & 0x7F) + 1)
        elif "scale" in f:
            if f.get("signed") and r >> (_field_width(f) - 1):
                r -= 1 << _field_width(f)
            v = round(r * f["scale"], 6)
        else:
            v = r
        out[f["name"]] = v
    return out


def fixed_sender(code: int):
    """Sender address the document / bench fixes for this command (bos_commands_0102.json "sender"), or None."""
    v = DEFS.get(code, {}).get("sender")
    return int(v, 0) if isinstance(v, str) else v


def receiver_of(code: int) -> int:
    d = DEFS.get(code, {})
    return GROUP_ANGLE if d.get("receiver") == "group" else BOS


def wants_ack(code: int) -> bool:
    return bool(DEFS.get(code, {}).get("ack", True))


def standby_only(code: int) -> bool:
    return DEFS.get(code, {}).get("modes") == "ДЖР"


def presets(code: int | None = None) -> list[tuple[int, dict]]:
    """[(code, preset)] in document order (only commands to the BOS)."""
    return [(c, p) for c, d in DEFS.items() if code in (None, c) for p in d.get("presets", [])]


def library(sender: int = SENDER_DEFAULT, line: str = "BOS CAN1") -> list[dict]:
    """Every preset as a CAN-tab command (can_commands.json format). Acknowledgement requested where the
    document requires it; target designation goes to the group address."""
    out = []
    for code, p in presets():
        data = encode(code, p.get("params"))
        can_id, payload = command(code, sender, data, wants_ack(code), receiver_of(code))
        out.append({"uid": f"0102-{code:02X}-{len([o for o in out if o['uid'].startswith(f'0102-{code:02X}-')])}",
                    "key": "", "name": p["name"], "id": f"{can_id:08X}", "ext": True, "rtr": False,
                    "dlc": len(payload), "data": payload.hex(" ").upper(), "line": line})
    return out


load_library()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="BOS 0102 command library: list / build frames / export for the CAN tab")
    ap.add_argument("--list", action="store_true", help="list every command and preset with its CAN frame")
    ap.add_argument("--sender", type=lambda x: int(x, 0), default=SENDER_DEFAULT, help="sender address (default 0x21 ВМ 2)")
    ap.add_argument("--line", default="BOS CAN1")
    ap.add_argument("--export", metavar="FILE", help="write the presets as a can_commands.json-style file")
    ap.add_argument("cmd", nargs="?", help="mnemonic or code to encode, followed by name=value parameters")
    ap.add_argument("params", nargs="*")
    a = ap.parse_args()
    if a.export:
        with open(a.export, "w", encoding="utf-8") as f:
            _json.dump({"commands": library(a.sender, a.line), "scripts": {}}, f, ensure_ascii=False, indent=1)
        print(f"{len(library(a.sender, a.line))} commands → {a.export}")
    if a.cmd:
        c = code_of(a.cmd)
        kv = {k: (None if v == "none" else v if v == "now" else float(v) if "." in v else int(v, 0))
              for k, v in (x.split("=", 1) for x in a.params)}
        cid, pl = command(c, a.sender, encode(c, kv), wants_ack(c), receiver_of(c))
        print(f"{COMMANDS[c][0]}: ID {cid:08X}  data [{len(pl)}] {pl.hex(' ').upper()}")
    if a.list or not (a.cmd or a.export):
        for c, p in presets():
            cid, pl = command(c, a.sender, encode(c, p.get("params")), wants_ack(c), receiver_of(c))
            print(f"0x{c:02X} {COMMANDS[c][0]:<26} {cid:08X}  {pl.hex(' ').upper():<23}  {p['name']}")
