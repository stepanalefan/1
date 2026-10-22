"""
1-Wire temperature sensors (DS18B20 / DS18S20 / DS1822) on the bench Arduino, pin A3.

The Arduino is the one on the Bench tab (same USB link as the multiplexer); see the sketch:
  OW:CONV  -> starts a conversion on all sensors, strong pull-up for parasitic power
  OW:READ  -> one line per sensor "OW:DEV:<ROM 8 bytes>:<scratchpad 9 bytes>:<1 ext / 0 parasitic>",
              then "OW:END:<n>"
CRCs, family, resolution and temperature are worked out here from the raw bytes.

Files next to the program / in the bench log folder:
* onewire_names.txt              "28FF4A1B9316035E=Cold plate"  (names edited in the table)
* temperature_log_YYYY-MM-DD.csv one row per sensor per poll
"""
from __future__ import annotations

import csv
import os
import sys
import time

from PyQt5 import QtCore, QtGui, QtWidgets

FAMILIES = {0x28: "DS18B20", 0x22: "DS1822", 0x10: "DS18S20", 0x42: "DS28EA00", 0x3B: "MAX31850"}
CONV_WAIT_MS = 800          # DS18B20 at 12 bit needs up to 750 ms
C_OK, C_WARN, C_ERR, C_DIM = "#4fd18b", "#f0b03e", "#ff6b6b", "#8a96a3"


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


NAMES_FILE = os.path.join(_app_dir(), "onewire_names.txt")


def crc8(data) -> int:
    """Dallas/Maxim 1-Wire CRC-8 (x^8 + x^5 + x^4 + 1, reflected)."""
    crc = 0
    for b in data:
        for _ in range(8):
            mix = (crc ^ b) & 1
            crc >>= 1
            if mix:
                crc ^= 0x8C
            b >>= 1
    return crc


def decode(rom_hex: str, sp_hex: str, ext: str) -> dict:
    rom, sp = bytes.fromhex(rom_hex), bytes.fromhex(sp_hex)
    fam = rom[0]
    d = {
        "rom": rom_hex.upper(),
        "family": fam,
        "model": FAMILIES.get(fam, f"family 0x{fam:02X}"),
        "serial": rom[6:0:-1].hex().upper(),                 # 48-bit serial, printed MSB first
        "rom_crc": rom[7],
        "rom_crc_ok": crc8(rom[:7]) == rom[7] and any(rom),
        "sp": sp.hex(" ").upper(),
        "sp_crc": sp[8],
        "sp_crc_ok": crc8(sp[:8]) == sp[8] and sp != b"\xff" * 9 and any(sp),
        "power": "external" if ext.strip() == "1" else "parasitic",
        "res": None,
        "temp": None,
        "status": "OK",
    }
    raw = int.from_bytes(sp[0:2], "little", signed=True)
    if fam in (0x28, 0x22, 0x42):
        res = 9 + (sp[4] >> 5 & 3)
        raw &= ~((1 << (12 - res)) - 1)                      # undefined low bits at < 12 bit
        d["res"], d["temp"] = res, raw / 16.0
    elif fam == 0x10:                                        # DS18S20: 0.5 degC + COUNT_REMAIN
        t = raw / 2.0
        if sp[7]:
            t = (raw >> 1) - 0.25 + (sp[7] - sp[6]) / sp[7]
        d["res"], d["temp"] = 12, t
    elif fam == 0x3B:                                        # MAX31850 thermocouple
        d["res"], d["temp"] = 14, (raw >> 2) / 4.0
    if sp == b"\xff" * 9:
        d["status"], d["temp"] = "no response", None
    elif not d["rom_crc_ok"]:
        d["status"] = "ROM CRC error"
    elif not d["sp_crc_ok"]:
        d["status"] = "data CRC error"
    elif d["temp"] is None:
        d["status"] = "unknown sensor type"
    elif fam in (0x28, 0x22) and sp[0:2] == b"\x50\x05":
        d["status"] = "85 °C power-on value: conversion did not run (parasitic power / pull-up?)"
    return d


def load_names() -> dict[str, str]:
    names = {}
    try:
        with open(NAMES_FILE, encoding="utf-8") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k.strip() and v.strip():
                    names[k.strip().upper()] = v.strip()
    except OSError:
        pass
    return names


def save_names(names: dict[str, str]):
    with open(NAMES_FILE, "w", encoding="utf-8") as f:
        for k, v in sorted(names.items()):
            f.write(f"{k}={v}\n")


class TempCsv:
    """temperature_log_YYYY-MM-DD.csv, one row per sensor per poll."""
    HEADER = ["Time", "ROM", "Name", "Temperature_C", "ROM_CRC_OK", "Data_CRC_OK", "Power", "Status",
              "Scratchpad"]

    def __init__(self, folder: str):
        self.folder = folder
        self.f = None
        self.day = None

    def write_poll(self, ts: float, rows: list[tuple]):
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        if day != self.day:
            self.close()
            path = os.path.join(self.folder, f"temperature_log_{day}.csv")
            new = not os.path.isfile(path)
            self.f = open(path, "a", newline="", encoding="utf-8")
            self.w = csv.writer(self.f)
            if new:
                self.w.writerow(self.HEADER)
            self.day = day
        stamp = time.strftime("%H:%M:%S", time.localtime(ts))
        for r in rows:
            self.w.writerow([stamp, *r])
        self.f.flush()

    def close(self):
        if self.f:
            self.f.close()
            self.f = None
            self.day = None


class TempTab(QtWidgets.QWidget):
    """1-Wire sensor table. `readings` (rom -> dict) is read by the Main tab."""
    COLS = ["Name", "Temperature", "Status", "Model", "Unique ID (ROM)", "ROM CRC", "Scratchpad",
            "Data CRC", "Resolution", "Power", "Min", "Max", "Updated"]
    _result = QtCore.pyqtSignal(object, float)     # list of decoded dicts, time
    _conv = QtCore.pyqtSignal(str)
    _failed = QtCore.pyqtSignal(str)

    def __init__(self, bench, parent=None):
        super().__init__(parent)
        self.bench = bench
        self.names = load_names()
        self.readings: dict[str, dict] = {}
        self.rows: dict[str, int] = {}
        self.csv: TempCsv | None = None
        self.busy_since = None
        self._result.connect(self._on_result)
        self._conv.connect(self._on_conv)
        self._failed.connect(self._on_failed)
        self._build()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.poll)
        self._apply_interval()
        self._on_log_toggled(self.log_chk.isChecked())

    # ------------------------------------------------------------------ UI
    def _build(self):
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        top = QtWidgets.QHBoxLayout()
        self.poll_btn = QtWidgets.QPushButton("⟳ Poll now")
        self.poll_btn.clicked.connect(self.poll)
        self.auto_chk = QtWidgets.QCheckBox("Poll every")
        self.auto_chk.setChecked(True)
        self.auto_chk.toggled.connect(self._apply_interval)
        self.interval = QtWidgets.QSpinBox()
        self.interval.setRange(2, 3600)
        self.interval.setValue(5)
        self.interval.setSuffix(" s")
        self.interval.valueChanged.connect(self._apply_interval)
        self.log_chk = QtWidgets.QCheckBox("Write daily log (temperature_log_*.csv)")
        self.log_chk.setChecked(True)
        self.log_chk.toggled.connect(self._on_log_toggled)
        forget = QtWidgets.QPushButton("Forget missing")
        forget.setToolTip("Remove sensors that were not found in the last poll from the table")
        forget.clicked.connect(self.forget_missing)
        for w in (self.poll_btn, self.auto_chk, self.interval, self.log_chk, forget):
            top.addWidget(w)
        top.addStretch(1)
        v.addLayout(top)
        self.status = QtWidgets.QLabel("Connect the Arduino on the Bench tab (sensors on pin A3).")
        self.status.setStyleSheet(f"color:{C_DIM};")
        v.addWidget(self.status)

        self.table = QtWidgets.QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.DoubleClicked |
                                   QtWidgets.QAbstractItemView.EditKeyPressed)
        self.table.setToolTip("Double-click a name to rename the sensor (saved to onewire_names.txt)")
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(0, QtWidgets.QHeaderView.Interactive)
        self.table.setColumnWidth(0, 160)
        hdr.setStretchLastSection(True)
        self.table.itemChanged.connect(self._on_item_changed)
        v.addWidget(self.table, 1)
        note = QtWidgets.QLabel("Unique ID = 64-bit ROM in bus order: family code, 48-bit serial, CRC.  "
                                "Sensors are polled through the Bench Arduino; voltage scanning keeps running.")
        note.setStyleSheet(f"color:{C_DIM};font-size:11px;")
        v.addWidget(note)

    def _apply_interval(self, *_):
        self.timer.stop()
        if self.auto_chk.isChecked():
            self.timer.start(self.interval.value() * 1000)

    def _on_log_toggled(self, on):
        if self.csv:
            self.csv.close()
        self.csv = TempCsv(self.bench.log_folder) if on else None

    def folder_changed(self):
        self._on_log_toggled(self.log_chk.isChecked())

    # ---------------------------------------------------------------- polling
    def poll(self):
        if self.busy_since and time.monotonic() - self.busy_since < 10:
            return
        scan = self.bench.scan
        if scan.dev is None or not hasattr(scan.dev, "ow"):
            self.status.setText("Arduino not connected - connect it on the Bench tab.")
            return
        self.busy_since = time.monotonic()
        self.poll_btn.setEnabled(False)

        def conv():
            try:
                self._conv.emit(scan.dev.ow("OW:CONV")[-1])
            except Exception as e:
                self._failed.emit(f"{type(e).__name__}: {e}")
        scan.submit(conv)

    def _on_conv(self, answer):
        if answer != "OW:CONV:OK":
            self._on_result([], time.time())       # nothing on the bus: everything is missing
            self.status.setText("No 1-Wire sensor answered (check wiring, pull-up 4.7 kΩ to +5 V on A3).")
            return
        QtCore.QTimer.singleShot(CONV_WAIT_MS, self._read)

    def _read(self):
        scan = self.bench.scan
        if scan.dev is None:
            self._failed.emit("Arduino disconnected")
            return

        def read():
            try:
                found = []
                for line in scan.dev.ow("OW:READ"):
                    p = line.split(":")
                    if p[:2] == ["OW", "DEV"] and len(p) == 5 and len(p[2]) == 16 and len(p[3]) == 18:
                        found.append(decode(p[2], p[3], p[4]))
                self._result.emit(found, time.time())
            except Exception as e:
                self._failed.emit(f"{type(e).__name__}: {e}")
        scan.submit(read)

    def _on_failed(self, msg):
        self.busy_since = None
        self.poll_btn.setEnabled(True)
        self.status.setText(f"1-Wire poll failed: {msg}")
        self.status.setStyleSheet(f"color:{C_ERR};")

    def _on_result(self, found: list, ts: float):
        self.busy_since = None
        self.poll_btn.setEnabled(True)
        seen = set()
        for d in found:
            rom = d["rom"]
            seen.add(rom)
            old = self.readings.get(rom, {})
            d["name"] = self.names.get(rom, "")
            d["ts"] = ts
            d["missing"] = False
            ok = d["status"] == "OK"
            d["valid"] = ok
            t = d["temp"] if ok else None
            lo = [x for x in (old.get("tmin"), t) if x is not None]
            hi = [x for x in (old.get("tmax"), t) if x is not None]
            d["tmin"], d["tmax"] = (min(lo) if lo else None), (max(hi) if hi else None)
            self.readings[rom] = d
        for rom, d in self.readings.items():
            if rom not in seen:
                d["missing"], d["valid"] = True, False
        for rom in self.readings:
            self._show(rom)
        bad = sum(1 for r in seen if not self.readings[r]["valid"])
        miss = sum(1 for d in self.readings.values() if d["missing"])
        self.status.setText(f"{time.strftime('%H:%M:%S', time.localtime(ts))}: {len(seen)} sensor(s) found"
                            + (f", {bad} with errors" if bad else "") + (f", {miss} missing" if miss else ""))
        self.status.setStyleSheet(f"color:{C_ERR if bad or miss else C_DIM};")
        if self.csv and self.readings:
            rows = []
            for rom, d in sorted(self.readings.items()):
                if d["missing"]:
                    rows.append((rom, self.names.get(rom, ""), "", "", "", "", "missing", ""))
                else:
                    rows.append((rom, d["name"], "" if not d["valid"] else f"{d['temp']:.4f}",
                                 int(d["rom_crc_ok"]), int(d["sp_crc_ok"]), d["power"], d["status"],
                                 d["sp"].replace(" ", "")))
            try:
                self.csv.write_poll(ts, rows)
            except OSError as e:
                self.status.setText(f"Temperature log stopped: {e}")
                self.log_chk.setChecked(False)

    # ------------------------------------------------------------------ table
    def _show(self, rom):
        d = self.readings[rom]
        self.table.blockSignals(True)          # new rows / text updates are not user edits
        if rom not in self.rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.rows[rom] = r
            for c in range(len(self.COLS)):
                it = QtWidgets.QTableWidgetItem("")
                if c != 0:
                    it.setFlags(QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled)
                self.table.setItem(r, c, it)
        r = self.rows[rom]
        fmt = lambda x: "—" if x is None else f"{x:.2f} °C"
        stale = d["missing"]
        cells = [
            self.names.get(rom, ""),
            "—" if stale or not d["valid"] else f"{d['temp']:.2f} °C",
            "not found in last poll" if stale else d["status"],
            d["model"],
            " ".join(rom[i:i + 2] for i in range(0, 16, 2)),
            f"{d['rom_crc']:02X} " + ("✓" if d["rom_crc_ok"] else "✗"),
            d["sp"],
            f"{d['sp_crc']:02X} " + ("✓" if d["sp_crc_ok"] else "✗"),
            f"{d['res']} bit" if d["res"] else "—",
            d["power"],
            fmt(d.get("tmin")),
            fmt(d.get("tmax")),
            time.strftime("%H:%M:%S", time.localtime(d["ts"])),
        ]
        for c, text in enumerate(cells):
            it = self.table.item(r, c)
            it.setText(text)
        self.table.item(r, 4).setToolTip(f"Family 0x{d['family']:02X} ({d['model']}), serial {d['serial']}, "
                                         f"CRC 0x{d['rom_crc']:02X}")
        self.table.item(r, 0).setToolTip(rom)
        color = QtGui.QColor(C_DIM if stale else (C_OK if d["valid"] else C_ERR))
        self.table.item(r, 2).setForeground(color)
        self.table.item(r, 1).setForeground(QtGui.QColor(C_DIM) if stale else QtGui.QBrush())
        for c in (5, 7):
            ok = d["rom_crc_ok"] if c == 5 else d["sp_crc_ok"]
            self.table.item(r, c).setForeground(QtGui.QColor(C_OK if ok else C_ERR))
        f = self.table.item(r, 1).font()
        f.setBold(True)
        self.table.item(r, 1).setFont(f)
        self.table.blockSignals(False)

    def _on_item_changed(self, it):
        if it.column() != 0:
            return
        rom = next((k for k, r in self.rows.items() if r == it.row()), None)
        if rom is None:
            return
        name = it.text().strip()
        if name:
            self.names[rom] = name
        else:
            self.names.pop(rom, None)
        if rom in self.readings:
            self.readings[rom]["name"] = name
        try:
            save_names(self.names)
        except OSError as e:
            self.status.setText(f"Could not save {NAMES_FILE}: {e}")

    def forget_missing(self):
        for rom in [k for k, d in self.readings.items() if d["missing"]]:
            del self.readings[rom]
        self.table.setRowCount(0)
        self.rows.clear()
        for rom in sorted(self.readings):
            self._show(rom)

    def shutdown(self):
        self.timer.stop()
        if self.csv:
            self.csv.close()
