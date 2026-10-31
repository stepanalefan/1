# -*- coding: utf-8 -*-
"""
frm_protocol.py
===============
FRM RS-485 protocol ("Protocol of information-logical conjugation, RS485 interface",
edition 3.0, 27.11.2025).

Data link: UART 115200 bit/s, 8 data bits, no parity, 1 stop bit, LSB first.
A command starts after >= 2.75 ms of silence; the FRM answers within 1.75 ms; a pause
> 750 us between characters ends the reception.

Command (master -> FRM):
    address(1) | function code(2, LE) | data length(1) | data(n) | CRC-16/MODBUS(2, LE)
Response (FRM -> master):
    address | 0x80 (1) | function code(2) | response code(1) | state(1) | data length(1) | data(n) | CRC(2)
FRM address: 0x79 (response address 0xF9). Response codes used by the FRM: 0x00 accepted,
0x02 cannot be executed. Multi-byte fields are little-endian.

This module has no Qt dependency except the CommandEditor widget at the bottom.
"""

from __future__ import annotations

FRM_ADDR = 0x79
RESP_FLAG = 0x80
BAUD = 115200

FN_MODE = 0x1501        # Standby / Service / Session
FN_RESET = 0x150A       # reset FRM counters (Service mode)
FN_DAC_OG = 0x150D      # DAC OG control (reference oscillator, 150 Hz steps)
FN_DAC_ENC1 = 0x1507    # DAC encoder 1 setup (qmc_*, coarse_dac)
FN_DAC_ENC2 = 0x1506    # DAC encoder 2 setup (pll_vco)
FN_SETTINGS = 0x1509    # SETTINGS array (technological)
FN_PWR_CORR = 0x150F    # power temperature correction
FN_PARKING = 0x37F7     # data parking (write to ROM)
FN_STATE = 0x150B       # FRM state, array 1..5

FUNCTIONS = {
    FN_MODE: "Mode", FN_RESET: "Reset counters", FN_DAC_OG: "DAC OG control",
    FN_DAC_ENC1: "DAC encoder 1 setup", FN_DAC_ENC2: "DAC encoder 2 setup",
    FN_SETTINGS: "SETTINGS array", FN_PWR_CORR: "Power temperature correction",
    FN_PARKING: "Data parking", FN_STATE: "FRM state",
}
RESPONSE_CODES = {0x00: "accepted", 0x01: "invalid function code", 0x02: "cannot be executed",
                  0x03: "invalid parameter set", 0x10: "data preparation"}
MODE_DATA = {"standby": 0x01, "service": 0x03}      # data byte 0 of the mode command (Table 6.1)
ARRAY_SIZES = {1: 0x3E, 2: 0x10, 3: 0xEA, 4: 0x10, 5: 0x2A}   # data length of the state responses

# Session command parameters = bytes 25..38 of Table 5.1
FUNCTION_ENUM = {1: "PRM (receiver)", 2: "PRD (beacon)", 3: "PRD + PRM"}
PRM_MOD_ENUM = {1: "BPSK", 2: "QPSK", 3: "OQPSK"}
PRM_INDEX_ENUM = {0: "±60°", 1: "±90°"}
PRM_SPEED_ENUM = {0: "8 kbps", 1: "16 kbps"}
PRD_MOD_ENUM = {1: "BPSK", 2: "carrier"}
PRD_DATA_ENUM = {0: "PSP (pseudo-random)", 1: "fixed frame"}
PRD_SPEED_ENUM = {1: "8 kbps", 2: "16 kbps", 3: "32 kbps", 4: "64 kbps", 5: "128 kbps", 6: "256 kbps",
                  7: "512 kbps"}
FREQ_MAX = 0x063E

# Reset counters, Table 6.2: (byte, bit, text)
RESET_BITS = [
    (0, 7, "cyclic counter of 'FRM state' messages"),
    (0, 6, "number of control codes received"),
    (0, 5, "number of control codes with CRC error"),
    (0, 4, "number of control codes with parameter error"),
    (0, 2, "operating time since power-on"),
    (0, 1, "total operating time (Service only)"),
    (0, 0, "operating time in Standby (Service only)"),
    (1, 7, "operating time in PRM (Service only)"),
    (1, 6, "operating time in PRD (Service only)"),
    (1, 0, "operating time in Session (Service only)"),
]

# DAC encoder 1, Table 6.4: value bits LSB first -> (byte, hi, lo) segments
ENC1_FIELDS = [  # name, bits, default, segments
    ("qmc_offseta", 13, 0b1111110000100, [(0, 7, 0), (1, 7, 3)]),
    ("qmc_offsetb", 13, 0b1111000110110, [(1, 2, 0), (2, 7, 0), (3, 7, 6)]),
    ("qmc_gaina", 11, 0b01111110100, [(3, 5, 0), (4, 7, 3)]),
    ("qmc_gainb", 11, 0b01111110100, [(4, 2, 0), (5, 7, 0)]),
    ("qmc_phaseab", 12, 0b111111110110, [(6, 7, 0), (7, 7, 4)]),
    ("coarse_dac", 4, 0b0111, [(7, 3, 0)]),
]
PLL_VCO_DEFAULT, PLL_VCO_RANGE = 0b100101, (0b100010, 0b101000)
DAC_OG_DEFAULT = 0x7FF


# --------------------------------------------------------------------------- #
#  CRC / frames                                                                #
# --------------------------------------------------------------------------- #
def crc16(data: bytes) -> int:
    """CRC-16/MODBUS (poly 0x8005 reflected = 0xA001, init 0xFFFF)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def with_crc(body: bytes) -> bytes:
    return bytes(body) + crc16(body).to_bytes(2, "little")


def build(fn: int, data: bytes = b"", addr: int = FRM_ADDR) -> bytes:
    data = bytes(data)
    return with_crc(bytes([addr]) + fn.to_bytes(2, "little") + bytes([len(data)]) + data)


def build_response(fn: int, data: bytes = b"", code: int = 0, state: int = 0, addr: int = FRM_ADDR) -> bytes:
    data = bytes(data)
    return with_crc(bytes([addr | RESP_FLAG]) + fn.to_bytes(2, "little") + bytes([code, state, len(data)]) + data)


class Msg:
    """Parsed frame. kind: 'cmd' / 'resp'. crc_ok False = checksum wrong."""
    __slots__ = ("kind", "addr", "fn", "code", "state", "data", "crc_ok", "size")

    def __init__(self, kind, addr, fn, data, crc_ok, size, code=None, state=None):
        self.kind, self.addr, self.fn, self.data, self.crc_ok, self.size = kind, addr, fn, data, crc_ok, size
        self.code, self.state = code, state

    def array(self) -> int | None:
        """State array number of an 'FRM state' response (1..5), else None."""
        if self.kind != "resp" or self.fn != FN_STATE or self.code != 0:
            return None
        n = len(self.data)
        if n in (ARRAY_SIZES[1], ARRAY_SIZES[2], ARRAY_SIZES[3], ARRAY_SIZES[4]) and self.data[:1] and \
                self.data[0] in (1, 2, 3, 4) and ARRAY_SIZES[self.data[0]] == n:
            return self.data[0]
        if n == ARRAY_SIZES[5]:
            return 5
        return None


def frame_size(buf: bytes) -> int | None:
    """Expected size of the frame at the start of buf, or None if not enough bytes to tell."""
    if not buf:
        return None
    if buf[0] & RESP_FLAG:
        return buf[5] + 8 if len(buf) >= 6 else None
    return buf[3] + 6 if len(buf) >= 4 else None


def parse(buf: bytes) -> Msg | None:
    """Parse one complete frame (exact size). None = not an FRM frame."""
    n = frame_size(buf)
    if n is None or n != len(buf) or n < 6:
        return None
    crc_ok = crc16(buf[:-2]) == int.from_bytes(buf[-2:], "little")
    fn = int.from_bytes(buf[1:3], "little")
    if buf[0] & RESP_FLAG:
        return Msg("resp", buf[0] & 0x7F, fn, bytes(buf[6:-2]), crc_ok, n, code=buf[3], state=buf[4])
    return Msg("cmd", buf[0], fn, bytes(buf[4:-2]), crc_ok, n)


def split(buf: bytes) -> list[bytes]:
    """Split a chunk that may hold several back-to-back frames; the rest stays one piece."""
    out, i = [], 0
    while i < len(buf):
        n = frame_size(buf[i:])
        if n is None or i + n > len(buf) or n < 6:
            break
        part = buf[i:i + n]
        if crc16(part[:-2]) != int.from_bytes(part[-2:], "little"):
            break
        out.append(part)
        i += n
    if i < len(buf):
        out.append(buf[i:])
    return out


# --------------------------------------------------------------------------- #
#  Commands                                                                    #
# --------------------------------------------------------------------------- #
def pack_segments(fields, values: dict, size: int) -> bytes:
    out = bytearray(size)
    for name, bits, _, segs in fields:
        v = int(values[name]) & ((1 << bits) - 1)
        for byte, hi, lo in segs:
            w = hi - lo + 1
            out[byte] |= (v & ((1 << w) - 1)) << lo
            v >>= w
    return bytes(out)


def unpack_segments(fields, data: bytes) -> dict:
    vals = {}
    for name, bits, _, segs in fields:
        v, shift = 0, 0
        for byte, hi, lo in segs:
            w = hi - lo + 1
            v |= ((data[byte] >> lo) & ((1 << w) - 1)) << shift
            shift += w
        vals[name] = v
    return vals


def session_data(p: dict) -> bytes:
    """14 bytes = Table 5.1 bytes 25..38."""
    return bytes([int(p["function"]), 0x01,
                  *int(p["prm_freq"]).to_bytes(2, "little"),
                  (int(p["prm_index"]) & 1) << 1 | (int(p["prm_speed"]) & 1),
                  int(p["prm_mod"]),
                  *int(p["prd_freq"]).to_bytes(2, "little"),
                  int(p["prd_mod"]), int(p["prd_data"]), int(p["prd_speed"]), int(p["att"]) & 0x1F,
                  0, 0])


SESSION_DEFAULTS = {"function": 1, "prm_freq": 0, "prm_index": 0, "prm_speed": 0, "prm_mod": 2,
                    "prd_freq": 0, "prd_mod": 1, "prd_data": 0, "prd_speed": 1, "att": 0}

# key, label, needs params
COMMANDS = [
    ("standby", "Standby (Duty) mode"),
    ("service", "Service mode"),
    ("session", "Session mode (parameters)"),
    ("read", "FRM state - read array"),
    ("reset", "Reset FRM counters"),
    ("dac_og", "DAC OG control"),
    ("dac_enc1", "DAC encoder 1 setup"),
    ("dac_enc2", "DAC encoder 2 setup"),
    ("settings", "SETTINGS array (232 B)"),
    ("pwr_corr", "Power temperature correction (47 B)"),
    ("parking", "Data parking (write to ROM)"),
]
COMMAND_LABELS = dict(COMMANDS)


def _hex(text) -> bytes:
    s = "".join(str(text).replace("0x", "").replace(",", " ").split())
    return bytes.fromhex(s)


def command(key: str, p: dict | None = None, addr: int = FRM_ADDR) -> bytes:
    """Complete command frame for a COMMANDS key and its parameters."""
    p = p or {}
    if key in MODE_DATA:
        return build(FN_MODE, bytes([MODE_DATA[key], 0]), addr)
    if key == "session":
        return build(FN_MODE, session_data({**SESSION_DEFAULTS, **p}), addr)
    if key == "read":
        n = int(p.get("array", 1))
        if n not in ARRAY_SIZES:
            raise ValueError("array number must be 1..5")
        return build(FN_STATE, bytes([n, 0]), addr)
    if key == "reset":
        b = bytearray(2)
        for byte, bit, _ in RESET_BITS:
            if f"{byte}.{bit}" in p.get("bits", []):
                b[byte] |= 1 << bit
        return build(FN_RESET, bytes(b), addr)
    if key == "dac_og":
        v = int(p.get("value", DAC_OG_DEFAULT))
        if not 0 <= v <= 0xFFF:
            raise ValueError("DATA_DAC_OG must be 0..4095")
        return build(FN_DAC_OG, v.to_bytes(2, "little"), addr)
    if key == "dac_enc1":
        vals = {n: p.get(n, d) for n, _, d, _ in ENC1_FIELDS}
        return build(FN_DAC_ENC1, pack_segments(ENC1_FIELDS, vals, 8), addr)
    if key == "dac_enc2":
        v = int(p.get("pll_vco", PLL_VCO_DEFAULT))
        if not PLL_VCO_RANGE[0] <= v <= PLL_VCO_RANGE[1]:
            raise ValueError(f"pll_vco must be {PLL_VCO_RANGE[0]}..{PLL_VCO_RANGE[1]}")
        return build(FN_DAC_ENC2, bytes([v & 0x3F, 0]), addr)
    if key == "settings":
        data = _hex(p.get("hex", ""))
        if len(data) != 232 or data[0] != 0xF1:
            raise ValueError(f"SETTINGS array must be 232 bytes starting with F1 (got {len(data)} B)")
        return build(FN_SETTINGS, data, addr)
    if key == "pwr_corr":
        data = _hex(p.get("hex", ""))
        if len(data) != 47 or data[0] != 0xF2:
            raise ValueError(f"power correction array must be 47 bytes starting with F2 (got {len(data)} B)")
        return build(FN_PWR_CORR, data, addr)
    if key == "parking":
        return build(FN_PARKING, bytes([0xE4, 0x00]), addr)
    raise ValueError(f"unknown FRM command {key!r}")


def describe(key: str, p: dict | None = None) -> str:
    p = p or {}
    if key == "session":
        q = {**SESSION_DEFAULTS, **p}
        parts = [FUNCTION_ENUM.get(q["function"], "?")]
        if q["function"] in (1, 3):
            parts.append(f"PRM code {q['prm_freq']} {PRM_MOD_ENUM.get(q['prm_mod'], '?')} "
                         f"{PRM_SPEED_ENUM[q['prm_speed'] & 1]} {PRM_INDEX_ENUM[q['prm_index'] & 1]}")
        if q["function"] in (2, 3):
            parts.append(f"PRD code {q['prd_freq']} {PRD_MOD_ENUM.get(q['prd_mod'], '?')} "
                         f"{PRD_SPEED_ENUM.get(q['prd_speed'], '?')} {PRD_DATA_ENUM.get(q['prd_data'], '?')} "
                         f"ATT {q['att']} dB")
        return "Session: " + ", ".join(parts)
    if key == "read":
        return f"read array {p.get('array', 1)}"
    if key == "reset":
        return f"reset counters ({len(p.get('bits', []))} selected)"
    if key == "dac_og":
        return f"DAC OG = {p.get('value', DAC_OG_DEFAULT)}"
    if key == "dac_enc1":
        return "DAC encoder 1: " + ", ".join(f"{n}={p.get(n, d)}" for n, _, d, _ in ENC1_FIELDS)
    if key == "dac_enc2":
        return f"DAC encoder 2: pll_vco={p.get('pll_vco', PLL_VCO_DEFAULT)}"
    return COMMAND_LABELS.get(key, key)


def decode_text(buf: bytes) -> tuple[str, str]:
    """(text, level) for the trace: level '' / 'ok' / 'err'."""
    parts = split(buf)
    texts, level = [], ""
    for part in parts:
        m = parse(part)
        if m is None:
            texts.append("not an FRM frame" if len(parts) == 1 else f"+{len(part)} B ?")
            level = level or ""
            continue
        name = FUNCTIONS.get(m.fn, f"fn 0x{m.fn:04X}")
        crc = "" if m.crc_ok else "  CRC ERROR"
        if m.kind == "cmd":
            extra = ""
            if m.fn == FN_MODE and len(m.data) == 2:
                extra = {0x01: " Standby", 0x03: " Service"}.get(m.data[0], f" 0x{m.data[0]:02X}")
            elif m.fn == FN_MODE and len(m.data) == 14:
                extra = " Session"
            elif m.fn == FN_STATE and m.data:
                extra = f" array {m.data[0]}"
            texts.append(f"→ 0x{m.addr:02X} {name}{extra} [{len(m.data)} B]{crc}")
        else:
            res = RESPONSE_CODES.get(m.code, f"code 0x{m.code:02X}")
            arr = m.array()
            texts.append(f"← 0x{m.addr:02X} {name}{f' array {arr}' if arr else ''}: {res}, state 0x{m.state:02X}, "
                         f"{len(m.data)} B{crc}")
            if m.code != 0:
                level = "err"
        if not m.crc_ok:
            level = "err"
        elif level == "":
            level = "ok"
    return "  |  ".join(texts), level


# --------------------------------------------------------------------------- #
#  Simulator                                                                   #
# --------------------------------------------------------------------------- #
class FrmSimulator:
    """Answers FRM commands like the real unit (demo mode)."""

    def __init__(self, addr=FRM_ADDR):
        import random
        import time
        self.rnd, self.t0 = random.Random(1), time.monotonic()
        self.time = time
        self.addr = addr
        self.mode = 0                       # 0 standby, 1 session, 2 service
        self.session = dict(SESSION_DEFAULTS)
        self.cnt = 0
        self.rx_ok = 0
        self.rx_crc = 0
        self.dac_og = DAC_OG_DEFAULT
        self.enc1 = {n: d for n, _, d, _ in ENC1_FIELDS}
        self.pll_vco = PLL_VCO_DEFAULT
        self.settings = bytes([0xF1, 0]) + bytes(230)
        self.pwr = bytes([0xF2]) + bytes(46)
        self.tc = 0

    def handle(self, frame: bytes) -> bytes | None:
        m = parse(frame)
        if m is None or m.kind != "cmd" or m.addr != self.addr:
            return None
        if not m.crc_ok:
            self.rx_crc += 1
            return build_response(m.fn, code=0x02, addr=self.addr)
        self.rx_ok += 1
        ok = build_response(m.fn, addr=self.addr)
        if m.fn == FN_MODE:
            if len(m.data) == 14:
                if self.mode == 2:
                    return build_response(m.fn, code=0x02, addr=self.addr)
                d = m.data
                self.session = {"function": d[0], "prm_freq": d[2] | d[3] << 8, "prm_index": d[4] >> 1 & 1,
                                "prm_speed": d[4] & 1, "prm_mod": d[5], "prd_freq": d[6] | d[7] << 8,
                                "prd_mod": d[8], "prd_data": d[9], "prd_speed": d[10], "att": d[11]}
                self.mode = 1
            elif m.data[:1] == b"\x01":
                self.mode = 0
            elif m.data[:1] == b"\x03":
                if self.mode == 1:
                    return build_response(m.fn, code=0x02, addr=self.addr)
                self.mode = 2
            return ok
        if m.fn == FN_STATE and m.data:
            self.cnt = (self.cnt + 1) & 0xFF
            return build_response(FN_STATE, self._array(m.data[0]), addr=self.addr)
        if m.fn == FN_RESET:
            if self.mode != 2 and (m.data[0] & 0x03 or m.data[1] & 0xC1):
                return build_response(m.fn, code=0x02, addr=self.addr)
            if m.data[0] & 0x80:
                self.cnt = 0
            if m.data[0] & 0x40:
                self.rx_ok = 0
            if m.data[0] & 0x20:
                self.rx_crc = 0
            return ok
        if m.fn == FN_DAC_OG:
            self.dac_og = int.from_bytes(m.data[:2], "little") & 0xFFF
            return ok
        if m.fn == FN_DAC_ENC1 and len(m.data) == 8:
            self.enc1 = unpack_segments(ENC1_FIELDS, m.data)
            return ok
        if m.fn == FN_DAC_ENC2:
            self.pll_vco = m.data[0] & 0x3F
            return ok
        if m.fn == FN_SETTINGS and len(m.data) == 232:
            self.settings = m.data
            return ok
        if m.fn == FN_PWR_CORR and len(m.data) == 47:
            self.pwr = m.data
            return ok
        if m.fn == FN_PARKING:
            return ok
        return build_response(m.fn, code=0x02, addr=self.addr)

    def _array(self, n) -> bytes:
        r = self.rnd
        up = int(self.time.monotonic() - self.t0) // 2
        s = self.session
        if n == 1:
            d = bytearray(62)
            d[0], d[1] = 1, self.cnt
            d[2:4] = (self.rx_ok & 0xFFFF).to_bytes(2, "little")
            d[4:6] = (self.rx_crc & 0xFFFF).to_bytes(2, "little")
            d[6:8] = (up & 0xFFFF).to_bytes(2, "little")
            d[8:11] = (123456 + up).to_bytes(3, "little")
            d[11:14] = (60000 + up * (self.mode == 0)).to_bytes(3, "little")
            d[14:17] = (30000).to_bytes(3, "little")
            d[17:20] = (20000).to_bytes(3, "little")
            d[20:23] = (40000 + up * (self.mode == 1)).to_bytes(3, "little")
            d[24], d[25] = s["function"], self.mode
            d[26:28] = s["prm_freq"].to_bytes(2, "little")
            d[28] = s["prm_index"] << 1 | s["prm_speed"]
            d[29] = s["prm_mod"]
            d[30:32] = s["prd_freq"].to_bytes(2, "little")
            d[32], d[33], d[34], d[35] = s["prd_mod"], s["prd_data"], s["prd_speed"], s["att"]
            d[37] = 0x80
            d[38:40] = (0x0F01).to_bytes(2, "little")
            d[40:42] = (0x0300).to_bytes(2, "little")
            for k, base in enumerate((450, 420, 400, 380)):
                d[42 + 2 * k:44 + 2 * k] = (base + r.randint(-3, 3)).to_bytes(2, "little")
            d[50:56] = bytes([1, 1, 1, 1, 1, 1])
            for k, base in enumerate((1000, 1800, 2500)):
                d[56 + 2 * k:58 + 2 * k] = (base + r.randint(-5, 5)).to_bytes(2, "little")
            return bytes(d)
        if n == 2:
            d = bytearray(16)
            d[0], d[1] = 2, self.cnt
            if self.mode == 1:
                self.tc += r.randint(0, 3)
            p = (-600 + r.randint(-20, 20)) & 0xFFFF
            d[2:4] = p.to_bytes(2, "little")
            d[4:6] = ((-610) & 0xFFFF).to_bytes(2, "little")
            d[6:10] = self.tc.to_bytes(4, "little")
            d[10:13] = (self.tc // 50).to_bytes(3, "little")
            d[13:16] = (self.tc - self.tc // 50).to_bytes(3, "little")
            return bytes(d)
        if n == 3:
            return bytes([3, self.cnt]) + self.settings
        if n == 4:
            vals = self.enc1
            d = bytearray(16)
            d[0], d[1] = 4, self.cnt
            d[2], d[3] = vals["qmc_offseta"] & 0xFF, vals["qmc_offseta"] >> 8
            d[4], d[5] = vals["qmc_offsetb"] & 0xFF, vals["qmc_offsetb"] >> 8
            d[6], d[7] = vals["qmc_gaina"] & 0xFF, vals["qmc_gaina"] >> 8
            d[8], d[9] = vals["qmc_gainb"] & 0xFF, vals["qmc_gainb"] >> 8
            d[10] = vals["qmc_phaseab"] & 0xFF
            d[11] = vals["coarse_dac"] << 4 | vals["qmc_phaseab"] >> 8
            d[12] = self.pll_vco
            d[13] = 0b100 << 4
            d[14], d[15] = self.dac_og & 0xFF, self.dac_og >> 8
            return bytes(d)
        if n == 5:
            return self.pwr[1:43]
        return b""


# --------------------------------------------------------------------------- #
#  Command editor widget (RS-485 tab and Main-tab sequence step)               #
# --------------------------------------------------------------------------- #
def _qt():
    from PyQt5 import QtCore, QtWidgets
    return QtCore, QtWidgets


def make_editor(parent=None, key="standby", params=None, sources=None):
    """CommandEditor factory (keeps Qt out of the module import)."""
    QtCore, QtWidgets = _qt()

    class CommandEditor(QtWidgets.QWidget):
        """Pick an FRM command and its parameters. get() -> (key, params); frame(addr) -> bytes.
        sources: optional {"settings": fn() -> bytes|None, "pwr_corr": fn() -> bytes|None} used by the
        'Copy from last array 3 / 5' buttons."""

        def __init__(self):
            super().__init__(parent)
            self.sources = sources or {}
            v = QtWidgets.QVBoxLayout(self)
            v.setContentsMargins(0, 0, 0, 0)
            self.cmd = QtWidgets.QComboBox()
            for k, label in COMMANDS:
                self.cmd.addItem(label, k)
            v.addWidget(self.cmd)
            self.stack = QtWidgets.QStackedWidget()
            v.addWidget(self.stack)
            self.w = {}
            for k, _ in COMMANDS:
                self.stack.addWidget(self._page(k))
            self.cmd.currentIndexChanged.connect(self._switch)
            self.set(key, params or {})

        # pages ------------------------------------------------------------
        def _combo(self, enum, val):
            c = QtWidgets.QComboBox()
            for k, t in enum.items():
                c.addItem(f"{t}  (0x{k:02X})", k)
            c.setCurrentIndex(max(0, c.findData(val)))
            return c

        def _spin(self, lo, hi, val, suffix=""):
            s = QtWidgets.QSpinBox()
            s.setRange(lo, hi)
            s.setValue(val)
            s.setSuffix(suffix)
            return s

        def _page(self, k):
            pw = QtWidgets.QWidget()
            f = QtWidgets.QFormLayout(pw)
            f.setContentsMargins(0, 0, 0, 0)
            w = {}
            d = SESSION_DEFAULTS
            if k == "session":
                w["function"] = self._combo(FUNCTION_ENUM, d["function"])
                w["prm_freq"] = self._spin(0, FREQ_MAX, d["prm_freq"])
                w["prm_mod"] = self._combo(PRM_MOD_ENUM, d["prm_mod"])
                w["prm_speed"] = self._combo(PRM_SPEED_ENUM, d["prm_speed"])
                w["prm_index"] = self._combo(PRM_INDEX_ENUM, d["prm_index"])
                w["prd_freq"] = self._spin(0, FREQ_MAX, d["prd_freq"])
                w["prd_mod"] = self._combo(PRD_MOD_ENUM, d["prd_mod"])
                w["prd_speed"] = self._combo(PRD_SPEED_ENUM, d["prd_speed"])
                w["prd_data"] = self._combo(PRD_DATA_ENUM, d["prd_data"])
                w["att"] = self._spin(0, 31, d["att"], " dB")
                for key, label in (("function", "Function"), ("prm_freq", "PRM frequency code"),
                                   ("prm_mod", "PRM modulation"), ("prm_speed", "PRM speed"),
                                   ("prm_index", "PRM mod. index"), ("prd_freq", "PRD (beacon) frequency code"),
                                   ("prd_mod", "PRD modulation"), ("prd_speed", "PRD speed"),
                                   ("prd_data", "PRD data"), ("att", "PRD attenuator")):
                    f.addRow(label, w[key])
                w["prm_freq"].setToolTip("Frequency letter 0x0000…0x063E")
                w["prd_freq"].setToolTip("Frequency letter 0x0000…0x063E")
            elif k == "read":
                w["array"] = QtWidgets.QComboBox()
                for n, t in ((1, "1 - state (TLM)"), (2, "2 - receiver (TLM)"), (3, "3 - SETTINGS (techn.)"),
                             (4, "4 - DAC parameters (techn.)"), (5, "5 - power correction (techn.)")):
                    w["array"].addItem(t, n)
                f.addRow("Array", w["array"])
            elif k == "reset":
                for byte, bit, text in RESET_BITS:
                    c = QtWidgets.QCheckBox(text)
                    w[f"{byte}.{bit}"] = c
                    f.addRow(c)
            elif k == "dac_og":
                w["value"] = self._spin(0, 0xFFF, DAC_OG_DEFAULT)
                w["value"].setToolTip("DATA_DAC_OG, 12 bit; 150 Hz per step; power-on default 2047")
                f.addRow("DATA_DAC_OG", w["value"])
            elif k == "dac_enc1":
                for name, bits, dflt, _ in ENC1_FIELDS:
                    w[name] = self._spin(0, (1 << bits) - 1, dflt)
                    w[name].setToolTip(f"{bits} bit, default {dflt} (0b{dflt:0{bits}b})")
                    f.addRow(name, w[name])
            elif k == "dac_enc2":
                w["pll_vco"] = self._spin(*PLL_VCO_RANGE, PLL_VCO_DEFAULT)
                f.addRow("pll_vco", w["pll_vco"])
                f.addRow(QtWidgets.QLabel("Loaded only in Standby mode."))
            elif k in ("settings", "pwr_corr"):
                w["hex"] = QtWidgets.QPlainTextEdit()
                w["hex"].setPlaceholderText("F1 …  (232 bytes)" if k == "settings" else "F2 …  (47 bytes)")
                w["hex"].setMaximumHeight(90)
                f.addRow(w["hex"])
                b = QtWidgets.QPushButton("Copy from last array " + ("3" if k == "settings" else "5"))
                b.clicked.connect(lambda _, k=k: self._copy(k))
                f.addRow(b)
            elif k == "parking":
                f.addRow(QtWidgets.QLabel("Writes SETTINGS / power correction to ROM (data E4 00)."))
            elif k in MODE_DATA:
                f.addRow(QtWidgets.QLabel("Mode change takes up to 2 s. Session ↔ Service is not allowed."))
            self.w[k] = w
            return pw

        def _copy(self, k):
            fn = self.sources.get(k)
            data = fn() if fn else None
            if not data:
                QtWidgets.QMessageBox.information(self, "FRM", "No array received yet - read it first.")
                return
            if k == "settings":
                body = data[2:]                                   # array 3 = no., counter, SETTINGS
            else:
                body = bytes([0xF2]) + data[:42] + bytes(4)       # array 5 = bytes 1..42 of Table 6.7
            self.w[k]["hex"].setPlainText(" ".join(f"{b:02X}" for b in body))

        def _switch(self, *_):
            self.stack.setCurrentIndex(self.cmd.currentIndex())

        # values -----------------------------------------------------------
        def get(self):
            k = self.cmd.currentData()
            w, p = self.w[k], {}
            for name, wdg in w.items():
                if isinstance(wdg, QtWidgets.QComboBox):
                    p[name] = wdg.currentData()
                elif isinstance(wdg, QtWidgets.QSpinBox):
                    p[name] = wdg.value()
                elif isinstance(wdg, QtWidgets.QPlainTextEdit):
                    p[name] = " ".join(wdg.toPlainText().split())
            if k == "reset":
                p = {"bits": [name for name, c in w.items() if c.isChecked()]}
            return k, p

        def set(self, k, p):
            i = self.cmd.findData(k)
            self.cmd.setCurrentIndex(max(0, i))
            self._switch()
            for name, wdg in self.w.get(k, {}).items():
                if k == "reset":
                    wdg.setChecked(name in p.get("bits", []))
                elif name not in p:
                    continue
                elif isinstance(wdg, QtWidgets.QComboBox):
                    wdg.setCurrentIndex(max(0, wdg.findData(p[name])))
                elif isinstance(wdg, QtWidgets.QSpinBox):
                    wdg.setValue(int(p[name]))
                elif isinstance(wdg, QtWidgets.QPlainTextEdit):
                    wdg.setPlainText(p[name])

        def frame(self, addr=FRM_ADDR) -> bytes:
            k, p = self.get()
            return command(k, p, addr)

    return CommandEditor()
