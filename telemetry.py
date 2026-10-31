# -*- coding: utf-8 -*-
"""
telemetry.py
============
"Telemetry" tab for fsw_vsa_monitor.py: decodes the telemetry of the FRM, received
over RS-485 (rs485_monitor.py, MOXA UPort 1450I). Live data and the trace are both fed here.

The FRM command / telemetry layout is not entered yet. Until then the tab shows every
frame received on the FRM port as raw bytes. To add the layout:
  * FRM_ARRAYS: (type, name, size) of every telemetry array,
  * frm_parse(): turns one received RS-485 frame into (type, payload) - or None,
  * TelemetryTab.LAYOUTS: (unit, type) -> (list of Field, notes) for the decoded view.
Field (below) describes one value: byte position, size / bits, kind, enum, limits.
"""

from __future__ import annotations

import datetime as _dt

from PyQt5 import QtCore, QtGui, QtWidgets

# --------------------------------------------------------------------------- #
#  Arrays                                                                      #
# --------------------------------------------------------------------------- #
FRM_PORT = "FRM"            # RS-485 port name (RS-485 tab) the FRM is connected to
FRM_ARRAYS: list = [  # type, name, size  - to be filled in with the FRM layout
]
# unit, RS-485 port name, arrays
UNITS = [
    ("FRM", FRM_PORT, FRM_ARRAYS),
]
WARN_ENUM: set = set()      # (field name, value) pairs that are always shown as warnings


def frm_parse(frame):
    """rs485_monitor.Frame -> (type, data) of a telemetry array, or None.
    Placeholder until the FRM layout is known: nothing is decoded."""
    return None


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

    def pos(self) -> tuple[str, str]:
        b = str(self.byte) if self.size == 1 else f"{self.byte}..{self.byte + self.size - 1}"
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

    def __init__(self, md_type, name, size, fields, notes=""):
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
            b, bits = f.pos()
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
    """Last received RS-485 frames of one unit (raw), until its layout is entered."""
    MAX = 500

    def __init__(self, unit, port):
        super().__init__()
        self.unit, self.port, self.count = unit, port, 0
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.header = QtWidgets.QLabel(f"no frame received yet on RS-485 port '{port}' - "
                                       f"connect it on the RS-485 tab")
        self.header.setStyleSheet("color:#8a96a3;padding:4px;")
        v.addWidget(self.header)
        info = QtWidgets.QLabel(f"{unit} telemetry layout not entered yet - raw received frames:")
        info.setStyleSheet("color:#8a96a3;")
        v.addWidget(info)
        self.dump = QtWidgets.QPlainTextEdit()
        self.dump.setReadOnly(True)
        self.dump.setFont(_mono())
        self.dump.setMaximumBlockCount(self.MAX)
        v.addWidget(self.dump, 1)

    def add(self, f):
        self.count += 1
        t = _dt.datetime.fromtimestamp(f.ts).strftime("%H:%M:%S.%f")[:-3]
        self.header.setText(f"<b>{self.unit}</b> &nbsp; port {self.port} &nbsp;·&nbsp; last {t} &nbsp;·&nbsp; "
                            f"received {self.count} frame(s)")
        self.dump.appendPlainText(f"{t}  [{len(f.data):3d}]  " + " ".join(f"{b:02X}" for b in f.data))


class TelemetryTab(QtWidgets.QTabWidget):
    """Top-level Telemetry tab: FRM, one sub-tab per array (+ raw frames)."""

    LAYOUTS: dict = {  # (unit, type) -> (fields, notes)  - FRM layout to be added
    }

    def __init__(self):
        super().__init__()
        self.views = {}                  # (unit, type) -> view
        self.raw = {}                    # port -> FrameView
        self.array_listeners = []        # callables(unit, type, data, ts) for every decoded array
        self.errors = 0
        self.port_unit = {p: u for u, p, _ in UNITS}
        for unit, port, arrays in UNITS:
            sub = QtWidgets.QTabWidget()
            for t, name, size in arrays:
                key = (unit, t)
                if key in self.LAYOUTS:
                    fields, notes = self.LAYOUTS[key]
                    view = FieldView(t, name, size, fields, notes=notes)
                else:
                    view = RawView(t, name, size)
                self.views[key] = view
                sub.addTab(view, f"{name} (0x{t:02X})")
            self.raw[port] = FrameView(unit, port)
            sub.addTab(self.raw[port], "Received frames")
            self.addTab(sub, f"{unit}  (RS-485 port {port})")
        self.setCurrentIndex(0)

    def on_frames(self, frames):
        """Listener for rs485_monitor.Rs485Tab.frame_listeners."""
        for f in frames:
            if f.dir != "RX" or f.port not in self.port_unit:
                continue
            unit = self.port_unit[f.port]
            self.raw[f.port].add(f)
            r = frm_parse(f)
            if r is None:
                continue
            t, data = r
            view = self.views.get((unit, t))
            if view is not None:
                view.update_array(f.port, data, f.ts, self.errors)
            for cb in self.array_listeners:
                cb(unit, t, data, f.ts)
