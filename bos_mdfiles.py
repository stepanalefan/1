# -*- coding: utf-8 -*-
"""
bos_mdfiles.py
==============
BOS data arrays to the unit (ПИЛВ 0102-004 v2, §4.1): MD_CONFIG (136 bytes, table 4.4) and MD_SETTINGS
(1024 bytes, tables 4.6 / 4.7), their read-back ИОК_КОНФИГ (0xFA, table 4.59) / ИОК_SETTINGS (0xFE, table 4.60),
file I/O and an editor widget for the Telemetry tab (BOS → MD_CONFIG / MD_SETTINGS).

Array transfer (table 4.2): 29-bit ID = C/D 1 | sender | receiver 0x29 | attribute (bits 15-12: start 0000,
current 0100, stop 1000, stop + КВ request 1100) | packet counter (bits 11-8, 0…F…0) | MD number (bits 7-0:
0x01 MD_CONFIG, 0x03 MD_STATE, 0x04 MD_SETTINGS); every packet carries 8 bytes.
"""

from __future__ import annotations

import json
import os
import time

from PyQt5 import QtCore, QtGui, QtWidgets

import bos_protocol as bp

MD_CONFIG, MD_STATE, MD_SETTINGS = 0x01, 0x03, 0x04
CONFIG_LEN, SETTINGS_LEN = 136, 1024
C_OK, C_ERR, C_DIM, C_WARN = "#4fd18b", "#ff6b6b", "#8a96a3", "#e3b341"


def F(name, byte, **kw):
    d = {"name": name, "byte": byte, "kind": "u"}
    d.update(kw)
    return d


T8 = dict(kind="i8", unit="°C", note="-128…127 °C (0x80 = -128, 0xFF = -1, 0x46 = 70)")

# ---------------------------------------------------------------- MD_CONFIG (table 4.4, 0102 v2)
CONFIG_FIELDS = [
    F("Config_Number", 0, ro=True, note="user configuration number, +1 at every save (set by the BOS)"),
    F("Config_Nmb_Cmd", 1, ro=True, note="sub-number: +1 per configuration command since the last save"),
    F("Config_Type", 2, bits=[7, 7], kind="enum", values={0: "factory", 1: "user"}, default=1, ro=True),
    F("Sour_10MHz", 2, bits=[6, 6], kind="enum", values={0: "internal", 1: "external"}, default=1,
      note="10 MHz reference source"),
    F("Data_Random", 2, bits=[5, 5], kind="enum", values={0: "off", 1: "on"}, default=1,
      note="data randomisation in ШР"),
    F("BPU_AFAR_mode", 2, bits=[3, 2], kind="enum",
      values={0: "automatic (default)", 1: "BPU1", 2: "BPU2", 3: "automatic"}, note="АФАР control unit kit"),
    F("QuickOn_mode", 2, bits=[1, 0], kind="enum",
      values={0: "signal + command (default)", 1: "QuickOn signal only", 2: "CAN commands only",
              3: "signal + command"}, note="fast session switch-on of the АФАР"),
    F("Temp_mode", 3, bits=[7, 5], kind="enum",
      values={0: "auto shutdown off", 1: "sensor 1", 2: "sensor 2", 4: "sensor 3", 7: "any sensor (default)"},
      default=7, note="over-temperature automatic shutdown"),
    F("Sour_Angle", 3, bits=[3, 2], kind="enum",
      values={0: "both sources (default)", 1: "УПНГ only", 2: "БКУ only", 3: "both sources"},
      note="target designation source for starting a session"),
    F("Numb_BOS", 3, bits=[1, 1], kind="enum", values={0: "first kit", 1: "second kit"}, ro=True),
    F("Time_SNR", 4, unit="s", default=30, max=255, note="session limit, 0 = none"),
    F("Angle_ErdStation_Err", 5, default=0x4B, max=0x7D, note="permissible angle, 0.2°/LSB (0x4B = 15°, 0x7D = 25°)"),
    F("Numb_Preamb", 6, default=4, note="preambles in ШР/ПООСШ and ШР/КБД = value + 1 (4 → 5)"),
    F("ID_KA", 7, len=2, default=0x300, max=0x3FF, kind="hex", note="spacecraft identifier, 10 bits"),
    F("Preamb", 9, len=4, default=0xC6DFA688, kind="hex", note="preamble value"),
    F("Registr_Preamb", 13, len=4, default=1, kind="hex", note="preamble PRS register start (lower 2 bytes used)"),
    F("Polyn0_Preamb", 17, len=4, default=0x8805, kind="hex", ro=True, note="factory x16+x12+x3+x+1"),
] + [F(f"Polyn{k}_Preamb", 21 + 4 * (k - 1), len=4, default=1, kind="hex", note="user preamble polynomial")
     for k in range(1, 8)] + [
    F("Registr1_KBD", 49, len=4, default=1, kind="hex", note="КБД PRS register 1 start (X.1)"),
    F("Registr2_KBD", 53, len=4, default=1, kind="hex", note="КБД PRS register 2 start (X.2)"),
    F("Polyn0.1_KBD", 57, len=4, default=0x00800043, kind="hex", ro=True, note="factory"),
    F("Polyn0.2_KBD", 61, len=4, default=0x00400010, kind="hex", ro=True, note="factory"),
] + [F(f"Polyn{k}.{j}_KBD", 65 + 8 * (k - 1) + 4 * (j - 1), len=4, default=0, kind="hex",
       note="user КБД polynomial (0 = none)") for k in range(1, 8) for j in (1, 2)] + [
    F("Ttlm_main_FC", 121, unit="s", default=1, note="ИОК_ОСН period, 0 = off"),
    F("Ttlm_dop_FB", 122, unit="s", default=0, note="ИОК_ДОП period, 0 = off"),
    F("Ttlm_stat_FD", 123, unit="s", default=0, note="ИОК_СОСТОЯНИЕ period, 0 = off"),
    F("Ttlm_conf_FA", 124, unit="s", default=0, note="ИОК_КОНФИГ period, 0 = off"),
    F("Temp_Max_DT1", 125, default=70, **T8),
    F("Temp_Max_DT2", 126, default=70, **T8),
    F("Temp_Max_DT3", 127, default=70, **T8),
]
CONFIG_DEFAULT_CRC = 0xF372                    # document §4.1.2: checksum of the default MD_CONFIG

# ---------------------------------------------------------------- MD_SETTINGS (tables 4.6 / 4.7)
DAC_PARTS = [  # table 4.7 inside DAC_offsets_k (byte offsets relative to the field)
    ("qmc_offseta", [[0, 7, 0], [1, 7, 3]], 0x1FFF), ("qmc_offsetb", [[1, 2, 0], [2, 7, 0], [3, 7, 6]], 0x1FFF),
    ("qmc_gaina", [[3, 5, 0], [4, 7, 3]], 0x7FF), ("qmc_gainb", [[4, 2, 0], [5, 7, 0]], 0x7FF),
    ("qmc_phaseab", [[6, 7, 0], [7, 7, 4]], 0xFFF), ("coarse_dac", [[7, 3, 0]], 0xF),
]
SETTINGS_FIELDS = [F("Type", 0, default=0xF1, kind="hex", ro=True, note="array type 0xF1"),
                   F("BOS_number", 1, bits=[0, 0], kind="enum", values={0: "first kit", 1: "second kit"})]
SETTINGS_TABLES = []   # (title, rows, [(column, field-name template)])
for k in range(3):
    SETTINGS_FIELDS += [F(f"DAC_pll_vco_temp_{k}", 5 + k, **T8),
                        F(f"DAC_pll_vco_{k}", 2 + k, bits=[5, 0], kind="hex", max=0x3F)]
SETTINGS_TABLES.append(("DAC pll_vco (encoder 2)", 3, [("threshold, °C", "DAC_pll_vco_temp_{}"),
                                                         ("pll_vco (6 bit)", "DAC_pll_vco_{}")]))
for k in range(16):
    SETTINGS_FIELDS.append(F(f"DAC_offsets_temp_{k}", 136 + k, **T8))
    for name, parts, mx in DAC_PARTS:
        SETTINGS_FIELDS.append(F(f"DAC_offsets_{k}.{name}", 8 + 8 * k,
                                 parts=[[8 + 8 * k + b, hi, lo] for b, hi, lo in parts], max=mx, kind="hex"))
SETTINGS_TABLES.append(("DAC offsets (encoder 1, table 4.7)", 16,
                        [("threshold, °C", "DAC_offsets_temp_{}")] +
                        [(n, "DAC_offsets_{}." + n) for n, _, _ in DAC_PARTS]))
for k in range(16):
    SETTINGS_FIELDS += [F(f"Power_set_temp_{k}", 168 + k, **T8),
                        F(f"Power_set_{k}", 152 + k, bits=[5, 0], max=0x3F)]
SETTINGS_TABLES.append(("Power adjustment (modulator temperature)", 16,
                        [("threshold, °C", "Power_set_temp_{}"), ("Power_set (6 bit)", "Power_set_{}")]))
for k in range(16):
    SETTINGS_FIELDS += [F(f"DAC_OG_data_temp_{k}", 216 + k, **T8),
                        F(f"DAC_OG_data_{k}", 184 + 2 * k, len=2, max=0xFFF, kind="hex")]
SETTINGS_TABLES.append(("Reference oscillator DAC (DAC_OG_data, modulator temperature)", 16,
                        [("threshold, °C", "DAC_OG_data_temp_{}"), ("DAC_OG_data (12 bit)", "DAC_OG_data_{}")]))
SETTINGS_EXAMPLE = {  # document note 2: OG DAC values by temperature
    "DAC_OG_data_0": 0x9C4, "DAC_OG_data_1": 0xBB8, "DAC_OG_data_2": 0xE74, "DAC_OG_data_3": 0xFA0,
    "DAC_OG_data_temp_0": 50, "DAC_OG_data_temp_1": 0, "DAC_OG_data_temp_2": -10, "DAC_OG_data_temp_3": -128}

KINDS = {
    "config": {"title": "MD_CONFIG", "len": CONFIG_LEN, "md": MD_CONFIG, "tm": 0xFA, "fields": CONFIG_FIELDS,
               "iok": "ИОК_КОНФИГ (0xFA)"},
    "settings": {"title": "MD_SETTINGS", "len": SETTINGS_LEN, "md": MD_SETTINGS, "tm": 0xFE,
                 "fields": SETTINGS_FIELDS, "iok": "ИОК_SETTINGS (0xFE)"},
}


# ---------------------------------------------------------------- model
def get(f, data) -> int:
    r = bp._get_raw(f, bytes(data) + bytes(8))
    if f["kind"] == "i8":
        return r - 256 if r > 127 else r
    return r


def put(f, data: bytearray, v):
    v = int(v)
    if f["kind"] == "i8":
        if not -128 <= v <= 127:
            raise ValueError(f"{f['name']} = {v}: -128…127")
        v &= 0xFF
    mx = f.get("max", (1 << (8 * f.get("len", 1))) - 1 if "bits" not in f and "parts" not in f else
               (1 << bp._field_width(f)) - 1)
    if not 0 <= v <= mx:
        raise ValueError(f"{f['name']} = {v:#x}: 0…{mx:#x}")
    bp._put_raw(f, data, v)


def finish(kind, data: bytearray) -> bytearray:
    """MD_CONFIG: checksum (CRC-16 of bytes 0-133, little-endian in 134-135); MD_SETTINGS: type byte."""
    if kind == "config":
        data[134:136] = bp.crc16(bytes(data[:134])).to_bytes(2, "little")
    else:
        data[0] = 0xF1
    return data


def default(kind) -> bytearray:
    k = KINDS[kind]
    d = bytearray(k["len"])
    for f in k["fields"]:
        if f.get("default"):
            put(f, d, f["default"])
    return finish(kind, d)


def to_values(kind, data) -> dict:
    return {f["name"]: get(f, data) for f in KINDS[kind]["fields"]}


def from_values(kind, values: dict, base=None) -> bytearray:
    d = bytearray(base) if base is not None else default(kind)
    for f in KINDS[kind]["fields"]:
        if f["name"] in values:
            v = values[f["name"]]
            put(f, d, int(v, 0) if isinstance(v, str) else v)
    return finish(kind, d)


def check(kind, data) -> list[str]:
    """Problems the BOS would object to (checksum, prohibited values)."""
    out = []
    if kind == "config":
        crc = int.from_bytes(data[134:136], "little")
        if crc != bp.crc16(bytes(data[:134])):
            out.append(f"checksum 0x{crc:04X} ≠ 0x{bp.crc16(bytes(data[:134])):04X}")
        v = to_values(kind, data)
        if v["Registr1_KBD"] == 0 and v["Registr2_KBD"] == 0:
            out.append("Registr1_KBD and Registr2_KBD both 0")
        if v["Angle_ErdStation_Err"] > 0x7D:
            out.append("Angle_ErdStation_Err > 0x7D (the BOS uses 0x7D)")
    elif data[0] != 0xF1:
        out.append(f"type byte 0x{data[0]:02X} ≠ 0xF1")
    return out


# ---------------------------------------------------------------- files
def save_file(kind, data, path):
    """.bin = raw array; anything else = JSON with named values and the hex bytes."""
    if path.lower().endswith(".bin"):
        with open(path, "wb") as fh:
            fh.write(bytes(data))
        return
    doc = {"type": KINDS[kind]["title"], "document": "ПИЛВ 0102-004 v2", "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
           "values": {k: (f"0x{v:X}" if f["kind"] == "hex" else v)
                      for f in KINDS[kind]["fields"] for k, v in [(f["name"], get(f, data))]},
           "hex": bytes(data).hex(" ").upper()}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)


def load_file(kind, path) -> bytearray:
    """JSON (values or hex), raw .bin (array, or with the 8-byte ИОК header) or a hex text file."""
    n = KINDS[kind]["len"]
    raw = open(path, "rb").read()
    text = None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    if text is not None and text.lstrip().startswith("{"):
        doc = json.loads(text)
        if doc.get("type") and doc["type"] != KINDS[kind]["title"]:
            raise ValueError(f"the file holds {doc['type']}, not {KINDS[kind]['title']}")
        if doc.get("values"):
            return from_values(kind, doc["values"])
        raw = bytes.fromhex(doc.get("hex", ""))
    elif text is not None and len(raw) not in (n, n + 8) and all(c in "0123456789abcdefABCDEFxX ,\r\n\t" for c in text):
        raw = bytes.fromhex(text.replace("0x", "").replace("0X", "").replace(",", " "))
    if len(raw) == n + 8:                     # ИОК with header: marker 0x04, type, length …
        raw = raw[8:]
    if len(raw) != n:
        raise ValueError(f"{KINDS[kind]['title']} must be {n} bytes, the file has {len(raw)}")
    return bytearray(raw)


# ---------------------------------------------------------------- CAN frames of an array
def md_frames(md, data, sender, ack=True):
    """[(can_id, 8-byte payload)] of an array to the BOS (table 4.2)."""
    d = bytes(data)
    d += bytes(-len(d) % 8)
    chunks = [d[i:i + 8] for i in range(0, len(d), 8)]
    out = []
    for k, ch in enumerate(chunks):
        last = k == len(chunks) - 1
        attr = (0b1100 if ack else 0b1000) if last else (0b0000 if k == 0 else 0b0100)
        cid = (1 << 28) | ((sender & 0x3F) << 22) | (bp.BOS << 16) | (attr << 12) | ((k & 0xF) << 8) | md
        out.append((cid, ch))
    return out


# ---------------------------------------------------------------- editor widget
def _mono():
    return QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)


class ValueEdit(QtWidgets.QWidget):
    """One field: combo for coded values, spin box for small numbers / temperatures, hex line for the rest."""
    changed = QtCore.pyqtSignal()

    def __init__(self, f):
        super().__init__()
        self.f = f
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        k = f["kind"]
        if k == "enum":
            self.w = QtWidgets.QComboBox()
            for v, t in f["values"].items():
                self.w.addItem(f"{v} — {t}", v)
            self.w.currentIndexChanged.connect(self.changed)
        elif k == "i8" or (k == "u" and f.get("max", 255) <= 0xFFFF):
            self.w = QtWidgets.QSpinBox()
            self.w.setRange(-128 if k == "i8" else 0, 127 if k == "i8" else f.get("max", 255))
            if f.get("unit"):
                self.w.setSuffix(" " + f["unit"])
            self.w.valueChanged.connect(self.changed)
        else:
            self.w = QtWidgets.QLineEdit()
            self.w.setFont(_mono())
            self.w.editingFinished.connect(self.changed)
        if f.get("ro"):
            self.w.setToolTip("set by the BOS - not checked when written (any value)")
        h.addWidget(self.w)

    def set(self, v):
        self.w.blockSignals(True)
        if isinstance(self.w, QtWidgets.QComboBox):
            i = self.w.findData(v)
            if i < 0:
                self.w.addItem(f"{v} — (not defined)", v)
                i = self.w.count() - 1
            self.w.setCurrentIndex(i)
        elif isinstance(self.w, QtWidgets.QSpinBox):
            self.w.setValue(int(v))
        else:
            n = max(1, (bp._field_width(self.f) + 3) // 4)
            self.w.setText(f"0x{int(v):0{n}X}")
        self.w.blockSignals(False)

    def get(self) -> int:
        if isinstance(self.w, QtWidgets.QComboBox):
            return self.w.currentData()
        if isinstance(self.w, QtWidgets.QSpinBox):
            return self.w.value()
        return int(self.w.text().strip() or "0", 0)


class MdEditor(QtWidgets.QWidget):
    """MD_CONFIG / MD_SETTINGS: new (defaults), load, save, read from the BOS, send to the BOS."""

    def __init__(self, kind, tele):
        super().__init__()
        self.kind, self.k, self.tele = kind, KINDS[kind], tele
        self.data = default(kind)
        self.path = ""
        self.edits = {}
        self.waiting = None                     # ("ack", t_end) / ("read", t_end)
        self.timer = QtCore.QTimer(self, singleShot=True, timeout=self._timeout)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        bar = QtWidgets.QHBoxLayout()
        for txt, fn, tip in (
                ("New (defaults)", self.new, "Default values of the document" +
                 (" (checksum 0xF372)" if kind == "config" else " (all zero, type 0xF1)")),
                ("Load file…", self.load, "JSON saved here, raw .bin (array or ИОК with header) or hex text"),
                ("Save file…", self.save_as, ".json with named values, or .bin raw bytes"),
                ("⇣ Read from BOS", self.read_bos, f"REQ_TM {self.k['iok']}: the settings the BOS uses now"),
                ("⇡ Send to BOS", self.send_bos, f"{self.k['title']} as a data array (КВ requested)")):
            b = QtWidgets.QPushButton(txt)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            bar.addWidget(b)
            if txt.startswith("⇡"):
                b.setStyleSheet("font-weight:700;")
        if kind == "config":
            b = QtWidgets.QPushButton("Save in BOS flash")
            b.setToolTip("SAVE_CONFIG_NEW (0x0E): writes the CONFIG the BOS uses now to flash (standby only).\n"
                         "Without it the sent CONFIG is lost at power-off.")
            b.clicked.connect(self.flash)
            bar.addWidget(b)
        else:
            b = QtWidgets.QPushButton("Example (doc)")
            b.setToolTip("Document example: DAC_OG_data 0x9C4 / 0xBB8 / 0xE74 / 0xFA0 above +50 / 0 / -10 / -128 °C")
            b.clicked.connect(self.example)
            bar.addWidget(b)
        bar.addStretch(1)
        v.addLayout(bar)
        self.info = QtWidgets.QLabel()
        self.info.setWordWrap(True)
        v.addWidget(self.info)
        if kind == "config":
            v.addWidget(self._flat(CONFIG_FIELDS), 1)
        else:
            tabs = QtWidgets.QTabWidget()
            tabs.addTab(self._flat(SETTINGS_FIELDS[:2]), "General")
            for title, rows, cols in SETTINGS_TABLES:
                tabs.addTab(self._grid(rows, cols), title.split(" (")[0])
                tabs.setTabToolTip(tabs.count() - 1, title)
            v.addWidget(tabs, 1)
            note = QtWidgets.QLabel("Each value is loaded when the temperature rises above its threshold; unused "
                                    "entries must be zero. DAC values are loaded only when a session starts (sent "
                                    "during a session they are kept for the next one); the OG DAC value is loaded in "
                                    "standby and turns the 10 MHz reference tuning off.")
            note.setWordWrap(True)
            note.setStyleSheet(f"color:{C_DIM};")
            v.addWidget(note)
        self.hexv = QtWidgets.QPlainTextEdit()
        self.hexv.setReadOnly(True)
        self.hexv.setFont(_mono())
        self.hexv.setMaximumHeight(110 if kind == "config" else 90)
        v.addWidget(self.hexv)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        self.refresh()

    # ---- layout
    def _flat(self, fields):
        t = QtWidgets.QTableWidget(len(fields), 4)
        t.setHorizontalHeaderLabels(["Byte", "Name", "Value", "Description"])
        t.verticalHeader().hide()
        t.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        t.horizontalHeader().setStretchLastSection(True)
        for r, f in enumerate(fields):
            if "parts" in f:
                by = str(f["byte"])
            elif "bits" in f:
                by = f"{f['byte']}.{f['bits'][0]}" + (f"-{f['bits'][1]}" if f["bits"][0] != f["bits"][1] else "")
            else:
                n = f.get("len", 1)
                by = f"{f['byte']}" + (f"-{f['byte'] + n - 1}" if n > 1 else "")
            items = [QtWidgets.QTableWidgetItem(by), QtWidgets.QTableWidgetItem(f["name"]),
                     None, QtWidgets.QTableWidgetItem(f.get("note", ""))]
            for c, it in enumerate(items):
                if it is not None:
                    if f.get("ro"):
                        it.setForeground(QtGui.QColor(C_DIM))
                    t.setItem(r, c, it)
            e = ValueEdit(f)
            e.changed.connect(lambda f=f, e=e: self._edited(f, e))
            t.setCellWidget(r, 2, e)
            self.edits[f["name"]] = e
        t.setColumnWidth(0, 60)
        t.setColumnWidth(1, 160)
        t.setColumnWidth(2, 230)
        return t

    def _grid(self, rows, cols):
        byname = {f["name"]: f for f in SETTINGS_FIELDS}
        t = QtWidgets.QTableWidget(rows, len(cols))
        t.setHorizontalHeaderLabels([c for c, _ in cols])
        t.setVerticalHeaderLabels([str(k) for k in range(rows)])
        for r in range(rows):
            for c, (_, tmpl) in enumerate(cols):
                f = byname[tmpl.format(r)]
                e = ValueEdit(f)
                e.setToolTip(f["name"])
                e.changed.connect(lambda f=f, e=e: self._edited(f, e))
                t.setCellWidget(r, c, e)
                self.edits[f["name"]] = e
        t.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        return t

    # ---- model <-> view
    def _edited(self, f, e):
        try:
            put(f, self.data, e.get())
            finish(self.kind, self.data)
        except ValueError as ex:
            self.msg(str(ex), False)
        self.refresh()

    def refresh(self):
        for f in self.k["fields"]:
            self.edits[f["name"]].set(get(f, self.data))
        raw = bytes(self.data)
        upto = len(raw) if self.kind == "config" else (max((i for i, b in enumerate(raw) if b), default=0) // 16 + 1) * 16
        self.hexv.setPlainText("\n".join(f"{i:04X}  {raw[i:i + 16].hex(' ').upper()}" for i in range(0, upto, 16)) +
                               ("" if upto >= len(raw) else f"\n… bytes {upto}-{len(raw) - 1} = 00"))
        probs = check(self.kind, self.data)
        crc = f" · checksum 0x{int.from_bytes(raw[134:136], 'little'):04X}" if self.kind == "config" else ""
        src = f" · file {os.path.basename(self.path)}" if self.path else ""
        self.info.setText(f"<b>{self.k['title']}</b> — {self.k['len']} bytes, MD number 0x{self.k['md']:02X}, "
                          f"{(len(raw) + 7) // 8} CAN packets{crc}{src}" +
                          (f" · <span style='color:{C_ERR}'>{'; '.join(probs)}</span>" if probs else ""))

    def msg(self, text, ok=None):
        col = C_OK if ok is True else C_ERR if ok is False else C_DIM
        self.status.setText(f"<span style='color:{col}'>{time.strftime('%H:%M:%S')}  {text}</span>")
        log = getattr(self.tele.host, "logmsg", None) if self.tele.host else None
        if log:
            log(f"{self.k['title']}: {text}", "ok" if ok else "err" if ok is False else "info")

    # ---- actions
    def new(self):
        self.data, self.path = default(self.kind), ""
        self.refresh()
        self.msg("defaults of the document" + (f" (checksum 0x{CONFIG_DEFAULT_CRC:04X})" if self.kind == "config"
                                               else ""), True)

    def example(self):
        self.data = from_values(self.kind, SETTINGS_EXAMPLE, self.data)
        self.refresh()
        self.msg("document example for DAC_OG_data entered", True)

    def load(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, f"Load {self.k['title']}", self.path or "",
                                                     "MD files (*.json *.bin *.hex *.txt);;All files (*)")
        if not p:
            return
        try:
            self.data, self.path = load_file(self.kind, p), p
        except Exception as e:
            return self.msg(f"cannot load {os.path.basename(p)}: {e}", False)
        self.refresh()
        probs = check(self.kind, self.data)
        self.msg(f"loaded {os.path.basename(p)}" + (f" — {'; '.join(probs)}" if probs else ""), not probs)

    def save_as(self):
        name = self.path or f"{self.k['title']}_{time.strftime('%Y%m%d_%H%M')}.json"
        p, _ = QtWidgets.QFileDialog.getSaveFileName(self, f"Save {self.k['title']}", name,
                                                     "JSON with values (*.json);;Raw bytes (*.bin)")
        if not p:
            return
        try:
            save_file(self.kind, self.data, p)
            self.path = p
            self.refresh()
            self.msg(f"saved {p}", True)
        except OSError as e:
            self.msg(f"cannot save: {e}", False)

    # ---- BOS
    def _ctx(self):
        """(station, can window, sender, line, demo)"""
        h = self.tele.host
        st = getattr(h, "bos", None)
        c = getattr(h, "can_win", None)
        ctl = st.data["ctl"] if st else {}
        line = ctl.get("line", "BOS CAN1")
        try:
            import canable_analyzer as ca
            line = ca.resolve_line(line, c.line_names() if c is not None else [])
        except Exception:
            pass
        return st, c, ctl.get("sender", bp.SENDER_DEFAULT), line, bool(st and st._is_demo())

    def _busy(self, st):
        if st is not None and st.running:
            self.msg("the BOS Test Station is running a plan - wait until it ends", False)
            return True
        if self.waiting:
            self.msg("still waiting for the previous answer", False)
            return True
        return False

    def send_bos(self):
        st, c, sender, line, demo = self._ctx()
        if self._busy(st):
            return
        probs = check(self.kind, self.data)
        if probs and QtWidgets.QMessageBox.question(
                self, self.k["title"], "Send anyway?\n\n" + "\n".join(probs)) != QtWidgets.QMessageBox.Yes:
            return
        frames = md_frames(self.k["md"], self.data, sender)
        if demo:
            st.sim.md_array(self.k["md"], bytes(self.data))
            return self.msg(f"sent to the simulated BOS: {len(frames)} packets → КВ+ (demo)", True)
        if c is None:
            return self.msg("CAN tab not available", False)
        for cid, pl in frames:
            if not c.send_frame(c.FRAME(0, cid, True, False, None, pl, bus=line)):
                return self.msg(f"not sent - line '{line}' not connected (CAN tab)", False)
        self.waiting = ("ack", sender)
        self.timer.start(1000)
        self.msg(f"{len(frames)} packets sent on {line} (ID 0x{frames[0][0]:08X} … 0x{frames[-1][0]:08X}), "
                 f"waiting for КВ …")

    def read_bos(self):
        st, c, sender, line, demo = self._ctx()
        if self._busy(st):
            return
        if demo:
            raw = st.sim.md_read(self.k["md"])
            if raw is None:
                return self.msg(f"simulated BOS: no {self.k['title']} yet", False)
            return self.got_array(raw, "simulated BOS")
        if c is None:
            return self.msg("CAN tab not available", False)
        cid, pl = bp.command(0x11, sender, bp.p_req_tm(self.k["tm"]), True)
        if not c.send_frame(c.FRAME(0, cid, True, False, None, pl, bus=line)):
            return self.msg(f"not sent - line '{line}' not connected (CAN tab)", False)
        self.waiting = ("read", sender)
        self.timer.start(4000)
        self.msg(f"REQ_TM {self.k['iok']} sent on {line}, waiting for the array …")

    def flash(self):
        st, c, sender, line, demo = self._ctx()
        if self._busy(st):
            return
        if QtWidgets.QMessageBox.question(self, "MD_CONFIG", "Write the CONFIG the BOS uses now into its flash "
                                          "(SAVE_CONFIG_NEW)?\nExecuted only in standby.") != QtWidgets.QMessageBox.Yes:
            return
        if demo:
            ok = st.sim.handle(0x0E, bytes(8), st._sim_map())
            return self.msg("SAVE_CONFIG_NEW → " + ("КВ+ (demo)" if ok else "КВ- (demo: not in standby?)"), ok)
        cid, pl = bp.command(0x0E, sender, b"", True)
        if c is None or not c.send_frame(c.FRAME(0, cid, True, False, None, pl, bus=line)):
            return self.msg(f"not sent - line '{line}' not connected (CAN tab)", False)
        self.waiting = ("save", sender)
        self.timer.start(1000)
        self.msg("SAVE_CONFIG_NEW sent, waiting for КВ …")

    def got_array(self, raw, src):
        self.data = bytearray(raw[:self.k["len"]])
        self.path = ""
        self.refresh()
        probs = check(self.kind, self.data)
        self.msg(f"{self.k['iok']} read from the {src}" + (f" — {'; '.join(probs)}" if probs else ""), not probs)

    def on_frames(self, frames):
        w = self.waiting
        if not w or w[0] == "read":
            return
        for fr in frames:
            a = bp.parse_ack(fr.can_id, fr.ext, fr.data, w[1])
            if a is None:
                continue
            want = 0x0E if w[0] == "save" else self.k["md"]
            if a["code"] != want and not (w[0] == "ack" and a["array"]):
                continue
            self.timer.stop()
            self.waiting = None
            if a["negative"]:
                self.msg(f"КВ- from the BOS ({'unknown' if a['unknown'] else 'not accepted - standby only / sender?'})",
                         False)
            else:
                self.msg(f"КВ+ via CAN{2 if a['can2'] else 1}" + (" — saved in flash" if w[0] == "save" else
                                                                   " — the BOS uses the new values"), True)
            return

    def on_array(self, t, data):
        if self.waiting and self.waiting[0] == "read" and t == self.k["tm"] and len(data) >= 8 + self.k["len"]:
            self.timer.stop()
            self.waiting = None
            self.got_array(data[8:8 + self.k["len"]], "BOS")

    def _timeout(self):
        w, self.waiting = self.waiting, None
        if w:
            self.msg({"read": f"no {self.k['iok']} within 4 s", "ack": "no КВ within 1 s",
                      "save": "no КВ within 1 s"}[w[0]], False)
