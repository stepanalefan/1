# -*- coding: utf-8 -*-
"""
telemetry.py
============
"Telemetry" tab for fsw_vsa_monitor.py: decodes the FRM state arrays received over RS-485
(rs485_monitor.py, MOXA UPort 1450I; protocol in frm_protocol.py).

Arrays are answers to the "FRM state" command (function 0x150B, data = array number):
    1  state (TLM)            62 B   Table 5.1 bytes 1..62
    2  receiver (TLM)         16 B   Table 5.1 bytes 63..78
    3  SETTINGS (technol.)   234 B   Table 6.8
    4  DAC parameters        16 B    Table 6.9
    5  power correction      42 B    Table 6.7 bytes 1..42
The "Byte" column shows the byte number of the protocol table; the value is decoded from the
data field of the response (little-endian).
"""

from __future__ import annotations

import datetime as _dt

from PyQt5 import QtCore, QtGui, QtWidgets

try:
    import frm_protocol as frm
except Exception:  # pragma: no cover
    frm = None

# --------------------------------------------------------------------------- #
#  Arrays                                                                      #
# --------------------------------------------------------------------------- #
FRM_ARRAYS = [  # type (array number), name, size
    (1, "Array 1 - state", 62),
    (2, "Array 2 - receiver", 16),
    (3, "Array 3 - SETTINGS", 234),
    (4, "Array 4 - DAC", 16),
    (5, "Array 5 - power correction", 42),
]
DOC_OFFSET = {1: 1, 2: 63, 3: 0, 4: 0, 5: 1}     # protocol table byte number = data index + offset
# unit, RS-485 port name (None = any port), arrays
UNITS = [
    ("FRM", None, FRM_ARRAYS),
]
WARN_ENUM: set = {("ADC load", 0)}   # (field name, value) pairs that are always shown as warnings


def frm_parse(frame):
    """rs485_monitor.Frame -> list of (array number, data) of every FRM state response in it."""
    if frm is None:
        return []
    out = []
    for part in frm.split(frame.data):
        m = frm.parse(part)
        if m is not None and m.crc_ok:
            n = m.array()
            if n:
                out.append((n, m.data))
    return out


# --------------------------------------------------------------------------- #
#  Field description                                                           #
# --------------------------------------------------------------------------- #
class Field:
    """byte = first byte, size = bytes (multi-byte, little-endian) or bits=(hi, lo)
    inside one byte. kind: u / i (signed) / bits / ang (INT8, 0.2 deg, 0x80 = none)
    / mask (channel bits) / res (reserve)."""

    def __init__(self, byte, name, desc, size=1, bits=None, kind="u", enum=None, expect=None,
                 compare=None, limit=None, scale=1.0, offset=0.0, unit="", rng=None):
        self.rng = rng                  # (lo, hi): warn outside
        self.low_part = None            # byte of the low 16 bits (counter split over two places)
        self.byte, self.name, self.desc = byte, name, desc
        self.scale, self.offset, self.unit = scale, offset, unit    # kind "lin"/"slin": v*scale+offset
        self.limit = limit              # warn when abs(value) > limit
        self.compare = compare          # (byte, name): value must equal that field (CRC check)
        self.size, self.bits, self.kind, self.enum, self.expect = size, bits, kind, enum or {}, expect

    def pos(self, offset: int = 0) -> tuple[str, str]:
        a = self.byte + offset
        b = str(a) if self.size == 1 else f"{a}..{a + self.size - 1}"
        if self.bits:
            hi, lo = self.bits
            return b, (str(hi) if hi == lo else f"{hi}-{lo}")
        return b, f"{8 * self.size - 1}-0"

    def raw(self, data: bytes) -> int | None:
        if self.byte + self.size > len(data):
            return None
        v = int.from_bytes(data[self.byte:self.byte + self.size], "little")
        if self.bits:
            hi, lo = self.bits
            v = (v >> lo) & ((1 << (hi - lo + 1)) - 1)
        if self.kind in ("i", "ang", "temp", "talarm", "slin", "talarm2"):
            n = 8 * self.size
            if v >= 1 << (n - 1):
                v -= 1 << n
        return v

    def value_str(self, v: int) -> str:
        if self.bits:
            w = self.bits[0] - self.bits[1] + 1
            return f"0b{v:0{w}b}"
        if self.kind in ("ang", "byte"):
            return f"0x{v & 0xFF:02X}"
        if self.size >= 8:              # parameter blocks: bytes in transmission order
            return " ".join(f"{b:02X}" for b in v.to_bytes(self.size, "little"))
        if self.kind in ("i", "temp", "talarm", "slin", "talarm2"):
            return f"{v}"
        return f"0x{v:0{2 * self.size}X}  ({v})" if self.size > 1 or v > 9 else f"0x{v:02X}"

    def meaning(self, v: int) -> tuple[str, bool]:
        """(text, warn)"""
        if self.kind == "ang":
            return ("no angle (0x80)", False) if v == -128 else (f"{v * 0.2:+.1f}°", False)
        if self.kind == "temp":
            return f"{v} °C", False
        if self.kind == "db" and (not self.rng or self.rng[0] <= v <= self.rng[1]):
            return f"{v} dB", False
        if self.kind == "t2s":          # LSB = 2 s
            sec = 2 * v
            return f"{sec // 3600} h {sec % 3600 // 60:02d} min {sec % 60:02d} s", False
        if self.kind == "tzch":
            return ("effective TZCH detected", True) if v == 0x0E else ("not detected", False)
        if self.kind == "mask8":
            on = [str(i + 1) for i in range(8) if v >> i & 1]
            return (("channels " + ", ".join(on) + " on") if on else "all channels off"), False
        if self.kind == "talarm":
            return ("not triggered (initial -128)", False) if v == -128 else (f"{v} °C at shutdown", True)
        if self.kind in ("lin", "slin"):
            return f"{v * self.scale + self.offset:.3f} {self.unit}".rstrip(), False
        if self.kind == "talarm2":      # trigger, T = v * 2 degC, initial 0x7F / 0x80
            return (("not triggered (initial value)", False) if v in (127, -128)
                    else (f"{2 * v} °C at shutdown", True))
        if self.kind == "tmode":
            if v == 0:
                return "auto power-off disabled", False
            if v == (1 << (self.bits[0] - self.bits[1] + 1)) - 1:
                return "on any sensor", False
            hit = [str(i + 1) for i in range(8) if v >> i & 1]
            return "on sensor " + ", ".join(hit), False
        if self.kind == "trig":
            w = self.bits[0] - self.bits[1] + 1 if self.bits else 8 * self.size
            hit = [str(i + 1) for i in range(w) if v >> i & 1]
            return (("TRIGGERED: sensor " + ", ".join(hit)), True) if hit else ("normal", False)
        if self.kind == "ms10":
            ms = v * 10
            return f"{ms // 3600000} h {ms // 60000 % 60:02d} min {ms // 1000 % 60:02d}.{ms % 1000:03d} s", False
        if self.kind == "fix3":     # bit 15 sign, 14..3 integer, 2..0 fraction; unit mV
            mv = (v & 0x7FFF) / 8 * (-1 if v & 0x8000 else 1)
            return f"{mv / 1000:.4f} V  ({mv:.3f} mV)", False
        if self.kind == "period":
            return ("disabled", False) if v == 0 else (f"every {v} s", False)
        if self.rng and not self.rng[0] <= v <= self.rng[1]:
            return f"outside 0x{self.rng[0]:02X}…0x{self.rng[1]:02X}", True
        if self.limit is not None and abs(v) > self.limit:
            return f"outside ±{self.limit}", True
        if self.kind == "byte":
            return (f"{v:3d}   '{chr(v)}'" if 32 <= v < 127 else f"{v:3d}"), False
        if self.kind == "dur":
            return f"{v // 3600} h {v % 3600 // 60:02d} min {v % 60:02d} s  ({v / 86400:.1f} days)", False
        if self.kind == "mask":
            on = [str(i + 1) for i in range(self.bits[0] - self.bits[1] + 1) if v >> i & 1]
            return (("channels " + ", ".join(on) + " on") if on else "all off"), False
        if self.kind == "res":
            return ("" if v == 0 else "reserve bits not 0"), v != 0
        if self.expect is not None and v != self.expect:
            return f"expected 0x{self.expect:02X}", True
        if self.enum:
            if v in self.enum:
                return self.enum[v], False
            return "undefined value", True
        return "", False


# --------------------------------------------------------------------------- #
#  FRM layouts (data index = protocol byte - DOC_OFFSET)                       #
# --------------------------------------------------------------------------- #
_FUNC = {0: "not installed", 1: "PRM (receiver)", 2: "PRD (beacon)", 3: "PRD + PRM"}
_MODE = {0: "Standby (Duty)", 1: "Session", 2: "Service"}

ARRAY1 = [
    Field(0, "Array no.", "Array number, 0x01", expect=0x01),
    Field(1, "Total TLM", "Cyclic counter of issued messages 'Information about the state of the FRM'"),
    Field(2, "Total UPR", "Number of control codes received via RS-485", size=2),
    Field(4, "Total UPR CRCErr", "Number of control codes received via RS-485 with checksum error", size=2),
    Field(6, "Time ON", "Operating time since power-on, LSB = 2 s", size=2, kind="t2s"),
    Field(8, "Time FRM", "Total operating time of the FRM, LSB = 2 s", size=3, kind="t2s"),
    Field(11, "Time Duty", "Total operating time in Standby (Duty) mode, LSB = 2 s", size=3, kind="t2s"),
    Field(14, "Time PRM", "Total operating time in PRM mode, LSB = 2 s", size=3, kind="t2s"),
    Field(17, "Time PRD", "Total operating time in PRD mode, LSB = 2 s", size=3, kind="t2s"),
    Field(20, "Time Session", "Total operating time in Session mode, LSB = 2 s", size=3, kind="t2s"),
    Field(23, "Param errors", "Control codes with parameter error (not used)"),
    Field(24, "Function", "FRM function (session parameter)", enum=_FUNC),
    Field(25, "FRM mode", "Operating mode of the FRM", enum=_MODE),
    Field(26, "PRM freq code", "PRM frequency letter, 0x0000…0x063E", size=2, rng=(0, 0x063E)),
    Field(28, "reserve", "Reserve ('0')", bits=(7, 2), kind="res"),
    Field(28, "PRM index", "PRM modulation index", bits=(1, 1), enum={0: "±60°", 1: "±90°"}),
    Field(28, "PRM speed", "PRM data rate", bits=(0, 0), enum={0: "8 kbps", 1: "16 kbps"}),
    Field(29, "PRM modulation", "PRM modulation type", enum={1: "BPSK", 2: "QPSK", 3: "OQPSK"}),
    Field(30, "PRD freq code", "TxD (beacon) frequency letter, 0x0000…0x063E", size=2, rng=(0, 0x063E)),
    Field(32, "PRD modulation", "TxD (beacon) modulation type", enum={1: "BPSK", 2: "carrier"}),
    Field(33, "PRD data", "PRD data type", enum={0: "PSP (pseudo-random)", 1: "fixed frame"}),
    Field(34, "PRD speed", "Beacon data rate", enum={1: "8 kbps", 2: "16 kbps", 3: "32 kbps", 4: "64 kbps",
                                                     5: "128 kbps", 6: "256 kbps", 7: "512 kbps"}),
    Field(35, "ATT PRD", "Attenuator gain, LSB = 1 dB, 0x00…0x1F", kind="db", rng=(0, 0x1F)),
    Field(36, "TZCH", "Sign of detection of effective TZCH (0x0E = detected)", kind="tzch"),
    Field(37, "ADC load", "ADC loading indicator (normal byte = 0x80)", bits=(7, 7),
          enum={0: "loading incorrect", 1: "loaded OK"}),
    Field(37, "ADC reg errors", "Counter of incorrectly loaded ADC registers", bits=(6, 0), limit=0),
    Field(38, "PR_ID", "Product identifier", size=2),
    Field(40, "SW_ID", "Firmware version", size=2),
    Field(42, "Temp FPGA", "FPGA temperature code", size=2, kind="i"),
    Field(44, "Temp DAC", "DAC temperature code", size=2, kind="i"),
    Field(46, "Temp MOD", "Modulator temperature code", size=2, kind="i"),
    Field(48, "Temp DMD", "Demodulator temperature code", size=2, kind="i"),
    Field(50, "Synth UPC", "Capture of the PLL synthesizer of the UPC"),
    Field(51, "Synth JESD", "PLL capture, JESD DAC"),
    Field(52, "Synth DAC", "Encoder DAC PLL capture"),
    Field(53, "Synth MOD", "PLL modulator capture"),
    Field(54, "JESD INT", "JESD DAC interface"),
    Field(55, "35MOD", "Modulator voltage 3.5 V within 10 % tolerance"),
    Field(56, "1CODE", "Encoder voltage 1 V (code)", size=2),
    Field(58, "18CODE", "Encoder voltage 1.8 V (code)", size=2),
    Field(60, "25CODE", "Encoder voltage 2.5 V (code)", size=2),
]
ARRAY1_NOTES = ("Temperature, PLL and voltage values are shown as codes: the protocol gives no conversion. "
                "Times: LSB = 2 s.")

ARRAY2 = [
    Field(0, "Array no.", "Array number, 0x02", expect=0x02),
    Field(1, "Total TLM", "Cyclic counter of issued messages 'Information about the state of the FRM'"),
    Field(2, "UR signal PRM", "Received signal power level, instantaneous value", size=2, kind="i"),
    Field(4, "UR signal PRM SR", "Received signal power level, average per session", size=2, kind="i"),
    Field(6, "PRM TK", "Number of received TCs per session", size=4),
    Field(10, "PRM TK CRC err", "Number of received TCs with CRC error", size=3),
    Field(13, "PRM TK CRC ok", "Number of received TCs without CRC errors", size=3),
]

ARRAY3 = [
    Field(0, "Array no.", "Array number, 0x03", expect=0x03),
    Field(1, "Total TLM", "Counter of issued messages 'Information on the state of the FRM'"),
    Field(2, "Type", "Constant 0xF1", expect=0xF1),
    Field(3, "reserve", "Reserve, not used", bits=(7, 1), kind="res"),
    Field(3, "FRM_number", "FRM kit number", bits=(0, 0), enum={0: "first set", 1: "second set"}),
] + [Field(4 + k, f"DAC_pll_vco_{k}", f"pll_vco of the DAC encoder above DAC_pll_vco_temp_{k} (6 LSB used)",
           bits=(5, 0)) for k in range(3)] \
  + [Field(7 + k, f"DAC_pll_vco_temp_{k}", f"DAC temperature threshold for DAC_pll_vco_{k}", kind="temp")
     for k in range(3)] \
  + [Field(10 + 8 * k, f"DAC_offsets_{k}", f"DAC encoder settings above DAC_offsets_temp_{k}", size=8)
     for k in range(16)] \
  + [Field(138 + k, f"DAC_offsets_temp_{k}", f"DAC temperature threshold for DAC_offsets_{k}", kind="temp")
     for k in range(16)] \
  + [Field(186 + 2 * k, f"DAC_OG_data_{k}", f"OG frequency adjustment above DAC_OG_data_temp_{k} (12 LSB used)",
           size=2, bits=(11, 0)) for k in range(16)] \
  + [Field(218 + k, f"DAC_OG_data_temp_{k}", f"Modulator temperature threshold for DAC_OG_data_{k}", kind="temp")
     for k in range(16)]
ARRAY3_NOTES = "Bytes 154…185 reserved. Read from ROM; written by 'SETTINGS array' + 'Data parking'."

ARRAY4 = [
    Field(0, "Array no.", "Array number, 0x04", expect=0x04),
    Field(1, "Total TLM", "Counter of issued messages 'Information on the state of the FRM'"),
    Field(2, "qmc_offseta", "Encoder DAC qmc_offseta (12:0), DAC38J82 reg 0x08", size=2, bits=(12, 0)),
    Field(4, "qmc_offsetb", "Encoder DAC qmc_offsetb (12:0), DAC38J82 reg 0x09", size=2, bits=(12, 0)),
    Field(6, "qmc_gaina", "Encoder DAC qmc_gaina (10:0), DAC38J82 reg 0x0C", size=2, bits=(10, 0)),
    Field(8, "qmc_gainb", "Encoder DAC qmc_gainb (10:0), DAC38J82 reg 0x0D", size=2, bits=(10, 0)),
    Field(10, "qmc_phaseab", "Encoder DAC qmc_phaseab (11:0), DAC38J82 reg 0x10", size=2, bits=(11, 0)),
    Field(11, "coarse_dac", "Encoder DAC coarse_dac, DAC38J82 reg 0x03", bits=(7, 4)),
    Field(12, "pll_vco", "Encoder DAC pll_vco, DAC38J82 reg 0x33", bits=(5, 0)),
    Field(13, "memin_pll_lfvolt", "Encoder DAC memin_pll_lfvolt, DAC38J82 reg 0x31 (must be 3 or 4)",
          bits=(6, 4), rng=(3, 4)),
    Field(14, "DAC_DATA", "DAC OG DAC_DATA (11:0), DAC60501 reg 0x08", size=2, bits=(11, 0)),
]
ARRAY4_NOTES = "Encoder DAC values are valid only in Session TX or TX+RX modes (DAC off in Standby / RX)."

ARRAY5 = [Field(0, "pwr_align", "Adjusts the instantaneous power in array 2", size=2, kind="i")] \
  + [Field(2 + 2 * k, f"pwr_temp_{k}", f"Temperature at which power correction pwr_{k} is loaded", size=2,
           kind="temp") for k in range(10)] \
  + [Field(22 + 2 * k, f"pwr_{k}", f"Power correction value pwr_{k}", size=2, kind="i") for k in range(10)]
ARRAY5_NOTES = ("Negative values in two's complement; extreme temperatures 0xFF00 (-256 °C) / 0x00FF (255 °C); "
                "unused corrections 0x0000.")


C_WARN = QtGui.QColor("#ff6b6b")
C_DIM = QtGui.QColor("#5c6570")
C_NAME = QtGui.QColor("#79c0ff")
C_HL = QtGui.QColor("#5a3a12")


def _mono():
    f = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
    f.setPointSize(max(9, f.pointSize()))
    return f


def hexdump(data: bytes) -> str:
    return "\n".join(f"{i:3d}:  " + " ".join(f"{b:02X}" for b in data[i:i + 8])
                     for i in range(0, len(data), 8))


# --------------------------------------------------------------------------- #
#  Views                                                                       #
# --------------------------------------------------------------------------- #
class ArrayHeader(QtWidgets.QLabel):
    def __init__(self):
        super().__init__("no array received yet - connect the RS-485 tab")
        self.setStyleSheet("color:#8a96a3;padding:4px;")
        self.setWordWrap(True)

    def show_info(self, base, name, size, data, ts, count, errors):
        ok = len(data) == size
        t = _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
        self.setText(f"<b>{name}</b> &nbsp; port {base} &nbsp;·&nbsp; last {t} &nbsp;·&nbsp; "
                     f"received {count} &nbsp;·&nbsp; "
                     + (f"{len(data)} B" if ok else f"<span style='color:#ff6b6b'>{len(data)} B, "
                                                   f"expected {size} B</span>")
                     + (f" &nbsp;·&nbsp; <span style='color:#f0b03e'>incomplete arrays: {errors}</span>"
                        if errors else ""))


class FieldView(QtWidgets.QWidget):
    """Table: Byte | Bits | Designation | Value | Meaning | Description."""
    COLS = ["Byte", "Bits", "Designation", "Value", "Meaning", "Description"]

    def __init__(self, md_type, name, size, fields, notes="", offset=0):
        super().__init__()
        self.md_type, self.name, self.size, self.fields = md_type, name, size, fields
        self.prev: list | None = None
        self.count = 0
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.header = ArrayHeader()
        v.addWidget(self.header)
        self.table = QtWidgets.QTableWidget(len(fields), len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setDefaultSectionSize(QtGui.QFontMetrics(_mono()).height() + 6)
        mono = _mono()
        for r, f in enumerate(fields):
            b, bits = f.pos(offset)
            for c, text in enumerate((b, bits, f.name, "—", "", f.desc)):
                it = QtWidgets.QTableWidgetItem(text)
                if c in (0, 1, 3):
                    it.setFont(mono)
                    it.setTextAlignment(QtCore.Qt.AlignCenter if c < 3 else
                                        QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
                if c == 2:
                    it.setForeground(C_DIM if f.kind == "res" else C_NAME)
                if f.kind == "res":
                    it.setForeground(C_DIM)
                self.table.setItem(r, c, it)
            self.table.item(r, 5).setToolTip(f.desc)
        h = self.table.horizontalHeader()
        for c, w in enumerate((60, 50, 170, 185, 250)):
            self.table.setColumnWidth(c, w)
        h.setStretchLastSection(True)
        v.addWidget(self.table, 1)
        if notes:
            note = QtWidgets.QLabel(notes)
            note.setWordWrap(True)
            note.setStyleSheet("color:#6e7681;font-size:11px;")
            v.addWidget(note)
        self.hl = QtCore.QTimer(self, singleShot=True, interval=1500, timeout=self._clear_hl)

    def update_array(self, base, data, ts, errors):
        self.count += 1
        self.header.show_info(base, self.name, self.size, data, ts, self.count, errors)
        vals = [f.raw(data) for f in self.fields]
        for r, (f, v) in enumerate(zip(self.fields, vals)):
            val_it, mean_it = self.table.item(r, 3), self.table.item(r, 4)
            if v is None:
                val_it.setText("—")
                mean_it.setText("not in array")
                mean_it.setForeground(C_WARN)
                continue
            text, warn = f.meaning(v)
            warn = warn or (f.name, v) in WARN_ENUM
            if f.low_part is not None and f.low_part + 2 <= len(data):
                lo = int.from_bytes(data[f.low_part:f.low_part + 2], "little")
                text = f"total (high·65536 + low) = {v << 16 | lo}"
            if f.compare and f.byte + f.size <= len(data):
                ref = int.from_bytes(data[f.compare[0]:f.compare[0] + f.size], "little")
                text, warn = ((f"= {f.compare[1]}", False) if ref == v else
                              (f"MISMATCH with {f.compare[1]} (0x{ref:04X})", True))
            val_it.setText(f.value_str(v))
            mean_it.setText(text)
            mean_it.setForeground(C_WARN if warn else QtGui.QBrush())
            changed = self.prev is not None and self.prev[r] != v
            for it in (val_it, mean_it):
                it.setBackground(C_HL if changed else QtGui.QBrush())
        self.prev = vals
        self.hl.start()

    def _clear_hl(self):
        for r in range(self.table.rowCount()):
            for c in (3, 4):
                self.table.item(r, c).setBackground(QtGui.QBrush())


class RawView(QtWidgets.QWidget):
    """Arrays whose layout is not described yet: hex dump of the last one."""

    def __init__(self, md_type, name, size):
        super().__init__()
        self.md_type, self.name, self.size, self.count = md_type, name, size, 0
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.header = ArrayHeader()
        v.addWidget(self.header)
        info = QtWidgets.QLabel(f"Field layout of {name} not entered yet - raw bytes of the last array:")
        info.setStyleSheet("color:#8a96a3;")
        v.addWidget(info)
        self.dump = QtWidgets.QPlainTextEdit()
        self.dump.setReadOnly(True)
        self.dump.setFont(_mono())
        v.addWidget(self.dump, 1)

    def update_array(self, base, data, ts, errors):
        self.count += 1
        self.header.show_info(base, self.name, self.size, data, ts, self.count, errors)
        self.dump.setPlainText(hexdump(data))



class FrameView(QtWidgets.QWidget):
    """All FRM frames seen on the RS-485 ports (commands and responses), decoded headers + hex."""
    MAX = 1000

    def __init__(self, unit):
        super().__init__()
        self.unit, self.count = unit, 0
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.header = QtWidgets.QLabel("no frame received yet - connect the FRM port on the RS-485 tab")
        self.header.setStyleSheet("color:#8a96a3;padding:4px;")
        v.addWidget(self.header)
        self.dump = QtWidgets.QPlainTextEdit()
        self.dump.setReadOnly(True)
        self.dump.setFont(_mono())
        self.dump.setMaximumBlockCount(self.MAX)
        self.dump.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        v.addWidget(self.dump, 1)

    def add(self, f):
        self.count += 1
        t = _dt.datetime.fromtimestamp(f.ts).strftime("%H:%M:%S.%f")[:-3]
        self.header.setText(f"<b>{self.unit}</b> &nbsp;·&nbsp; last frame {t} on {f.port} &nbsp;·&nbsp; "
                            f"{self.count} frame(s)")
        text = frm.decode_text(f.data)[0] if frm else ""
        self.dump.appendPlainText(f"{t} {f.port:>4} {f.dir}  {text}\n"
                                  f"{'':17}[{len(f.data):3d}] " + " ".join(f"{b:02X}" for b in f.data))


class TelemetryTab(QtWidgets.QTabWidget):
    """Top-level Telemetry tab: FRM, one sub-tab per state array + all frames."""

    LAYOUTS = {  # (unit, type) -> (fields, notes)
        ("FRM", 1): (ARRAY1, ARRAY1_NOTES),
        ("FRM", 2): (ARRAY2, ""),
        ("FRM", 3): (ARRAY3, ARRAY3_NOTES),
        ("FRM", 4): (ARRAY4, ARRAY4_NOTES),
        ("FRM", 5): (ARRAY5, ARRAY5_NOTES),
    }

    def __init__(self):
        super().__init__()
        self.views = {}                  # (unit, type) -> view
        self.raw = {}                    # unit -> FrameView
        self.array_listeners = []        # callables(unit, type, data, ts) for every decoded array
        self.last = {}                   # (unit, type) -> last data
        self.errors = 0
        for unit, port, arrays in UNITS:
            sub = QtWidgets.QTabWidget()
            for t, name, size in arrays:
                key = (unit, t)
                if key in self.LAYOUTS:
                    fields, notes = self.LAYOUTS[key]
                    view = FieldView(t, name, size, fields, notes=notes, offset=DOC_OFFSET.get(t, 0))
                else:
                    view = RawView(t, name, size)
                self.views[key] = view
                sub.addTab(view, name)
            self.raw[unit] = FrameView(unit)
            sub.addTab(self.raw[unit], "All frames")
            self.addTab(sub, f"{unit}  (RS-485, address 0x{frm.FRM_ADDR if frm else 0x79:02X})")
        self.setCurrentIndex(0)

    def on_frames(self, frames):
        """Listener for rs485_monitor.Rs485Tab.frame_listeners (all ports, RX and TX)."""
        unit = "FRM"
        for f in frames:
            if frm is None or not any(frm.parse(p) for p in frm.split(f.data)):
                continue
            self.raw[unit].add(f)
            if f.dir != "RX":
                continue
            for t, data in frm_parse(f):
                self.last[(unit, t)] = data
                view = self.views.get((unit, t))
                if view is not None:
                    view.update_array(f.port, data, f.ts, self.errors)
                for cb in self.array_listeners:
                    cb(unit, t, data, f.ts)
