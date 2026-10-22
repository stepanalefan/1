# -*- coding: utf-8 -*-
"""
dashboard.py
============
"Main" tab of fsw_vsa_monitor.py: everything important on one screen.

* Device status: FSW, HMP4040, SMW200A, 34405A, Arduino, CAN adapter.
* Test sequences: steps that drive all devices (HMP outputs / master, SMW signal, CAN
  command from the CAN library, FSW run/stop/single, voltage scan start/stop, pause,
  repeat ... end, log message). Device steps wait until the instrument has executed
  them; any error stops the sequence. Stored in dashboard.json next to the program.
* Constellation + MER / Power / Carrier frequency error (mirror of the VSA tab).
* Voltage plot of selected Bench channels.
* Telemetry watch list: any decoded telemetry variable, picked from the Telemetry layouts.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import sys
import time

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

try:
    import telemetry
except Exception:  # pragma: no cover
    telemetry = None


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


STORE_FILE = os.path.join(_app_dir(), "dashboard.json")


class TelemetryCsv:
    """telemetry_log_YYYY-MM-DD.csv next to the program: one row per watched value update.
    Long format, so adding or removing watch rows never breaks the file's columns."""
    HEADER = ["Time", "Unit", "Array", "Designation", "Byte", "Value", "Meaning", "Warning"]

    def __init__(self):
        self.f = None
        self.day = None
        self.last_flush = 0.0

    def write(self, ts, unit, arr, f, value, meaning, warn):
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        if day != self.day:
            self.close()
            path = os.path.join(_app_dir(), f"telemetry_log_{day}.csv")
            new = not os.path.isfile(path)
            self.f = open(path, "a", newline="", encoding="utf-8")
            self.w = csv.writer(self.f)
            if new:
                self.w.writerow(self.HEADER)
            self.day = day
        stamp = time.strftime("%H:%M:%S", time.localtime(ts)) + f".{int(ts * 1000) % 1000:03d}"
        self.w.writerow([stamp, unit, arr, f.name, f.byte, value, meaning, int(bool(warn))])
        if ts - self.last_flush > 1.0:
            self.f.flush()
            self.last_flush = ts

    def close(self):
        if self.f:
            self.f.close()
            self.f = None
            self.day = None
C_OK, C_OFF, C_ERR, C_DIM = "#4fd18b", "#6e7681", "#ff6b6b", "#8a96a3"
STEP_COLORS = {"fsw_set": "#58a6ff", "hmp": "#f0b03e", "smw": "#d2a8ff", "can": "#79c0ff", "wait": "#8a96a3",
               "fsw": "#58a6ff", "scan": "#4fd18b", "repeat": "#d2a8ff", "end": "#d2a8ff", "log": "#c9d1d9"}
SMW_KEYS = [  # key, label
    ("freq", "Frequency, Hz"), ("level", "Level, dBm"), ("format", "Modulation"), ("srate", "Symbol rate, Hz"),
    ("filter", "Filter"), ("fparam", "Filter alpha / BT"), ("dm", "Baseband DM on"),
    ("mod_all", "Modulation on"), ("rf", "RF output on"),
]


def parse_si(text: str) -> float:
    t = text.strip().replace(" ", "").replace(",", ".")
    for u in ("sym/s", "sps", "hz", "dbm", "db", "s"):
        if t.lower().endswith(u) and len(t) > len(u):
            t = t[:-len(u)]
            break
    pre = {"T": 1e12, "G": 1e9, "M": 1e6, "k": 1e3, "K": 1e3, "m": 1e-3, "u": 1e-6}
    if t and t[-1] in pre:
        return float(t[:-1]) * pre[t[-1]]
    return float(t)


def fsw_params(host, app: str):
    """(key, label, kind, choices) of the FSW settings a sequence step can set.
    kind: hz (SI number), f (float), i (int), b (on/off), c (choice)."""
    hm = sys.modules[type(host).__module__]
    if app == "vsa":
        return [
            ("center", "Center frequency, Hz", "hz", None),
            ("ref_level", "Ref level, dBm", "f", None),
            ("att_auto", "Attenuation auto", "b", None),
            ("att", "Attenuation, dB", "f", None),
            ("preamp", "Preamplifier", "b", None),
            ("modulation", "Modulation", "c", [(m, m) for m in hm.MODULATIONS]),
            ("symbol_rate", "Symbol rate, sym/s", "hz", None),
            ("prat", "Oversampling (sample rate = SR × N)", "c", [(o, int(o)) for o in hm.OVERSAMPLING]),
            ("tfil_state", "Tx filter on", "b", None),
            ("tfil_name", "Tx filter type", "c", [(t, t) for t in hm.TX_FILTERS]),
            ("tfil_alpha", "Tx filter alpha / BT", "f", None),
            ("mfil_auto", "Meas filter auto (matched)", "b", None),
            ("rlen_auto", "Capture length auto", "b", None),
            ("capture_len", "Capture length, symbols", "i", None),
            ("result_len", "Result length, symbols", "i", None),
            ("sweep_count", "Statistic count", "i", None),
            ("equalizer", "Equalizer", "b", None),
            ("signal_type", "Signal type", "c", list(hm.SIGNAL_TYPES)),
            ("burst", "Burst search", "b", None),
            ("pattern", "Pattern search", "b", None),
            ("trig_source", "Trigger source", "c", [(t, t) for t in hm.TRIGGERS]),
        ]
    return [
        ("center", "Center frequency, Hz", "hz", None),
        ("span", "Span, Hz", "hz", None),
        ("ref_level", "Ref level, dBm", "f", None),
        ("range", "Display range, dB", "f", None),
        ("att_auto", "Attenuation auto", "b", None),
        ("att", "Attenuation, dB", "f", None),
        ("preamp", "Preamplifier", "b", None),
        ("rbw_auto", "RBW auto", "b", None),
        ("rbw", "RBW, Hz", "hz", None),
        ("vbw_auto", "VBW auto", "b", None),
        ("vbw", "VBW, Hz", "hz", None),
        ("swt_auto", "Sweep time auto", "b", None),
        ("swt", "Sweep time, s", "hz", None),
        ("points", "Sweep points", "i", None),
        ("detector", "Detector", "c", list(hm.DETECTORS)),
        ("trace_mode", "Trace mode", "c", list(hm.TRACE_MODES)),
        ("count", "Sweep / average count", "i", None),
    ]


def parse_channels(text: str) -> list[int]:
    out = []
    for tok in re.split(r"[,;\s]+", text.strip()):
        if not tok:
            continue
        if "-" in tok:
            a, b = tok.split("-", 1)
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(tok))
    return [c for c in dict.fromkeys(out) if 0 <= c < 176]


def fmt_hz(v):
    if v is None:
        return "—"
    for e, p in ((9, "G"), (6, "M"), (3, "k")):
        if abs(v) >= 10 ** e:
            return f"{v / 10 ** e:.6g} {p}Hz"
    return f"{v:g} Hz"


def check_steps(steps) -> str:
    depth = 0
    for i, s in enumerate(steps):
        if s["op"] == "repeat":
            depth += 1
        elif s["op"] == "end":
            depth -= 1
            if depth < 0:
                return f"step {i + 1}: 'End repeat' without 'Repeat'"
    return f"{depth} 'Repeat' without 'End repeat'" if depth else ""


class Token:
    """Completion flag for a job handed to a device worker thread."""

    def __init__(self, what, timeout=None):
        self.what, self.done, self.error, self.msg = what, False, None, ""
        self.deadline = time.monotonic() + timeout if timeout else None


def run_in(worker_submit, fn, what) -> Token:
    tok = Token(what)

    def job():
        try:
            fn()
        except Exception as e:
            tok.error = f"{type(e).__name__}: {e}"
        finally:
            tok.done = True
    worker_submit(job)
    return tok


# --------------------------------------------------------------------------- #
#  Step editor dialog                                                          #
# --------------------------------------------------------------------------- #
class StepDialog(QtWidgets.QDialog):
    def __init__(self, parent, step: dict, can_cmds: list, host=None):
        super().__init__(parent)
        self.step = dict(step)
        op = step["op"]
        self.setWindowTitle({"hmp": "HMP4040 outputs", "smw": "SMW200A signal", "can": "CAN command",
                             "wait": "Pause", "fsw": "FSW", "fsw_set": "FSW settings", "scan": "Voltage scan", "repeat": "Repeat",
                             "log": "Log message"}.get(op, op))
        f = QtWidgets.QFormLayout(self)
        self.w = {}
        if op == "hmp":
            grid = QtWidgets.QGridLayout()
            for c, h in enumerate(("Channel", "Set", "Voltage, V", "Current, A", "Output")):
                grid.addWidget(QtWidgets.QLabel(f"<b>{h}</b>"), 0, c)
            chs = step.get("ch", {})
            for ch in (1, 2, 3, 4):
                p = chs.get(str(ch))
                use = QtWidgets.QCheckBox()
                use.setChecked(p is not None)
                use.setToolTip("Unchecked = leave this channel as it is")
                v = QtWidgets.QDoubleSpinBox()
                v.setRange(0, 32)
                v.setDecimals(3)
                v.setValue((p or {}).get("v", 5.0))
                i = QtWidgets.QDoubleSpinBox()
                i.setRange(0, 10)
                i.setDecimals(3)
                i.setValue((p or {}).get("i", 1.0))
                on = QtWidgets.QCheckBox("on")
                on.setChecked((p or {}).get("on", True))
                for c, wdg in enumerate((QtWidgets.QLabel(f"OUT {ch}"), use, v, i, on)):
                    grid.addWidget(wdg, ch, c)
                self.w[ch] = (use, v, i, on)
            f.addRow(grid)
            self.master = QtWidgets.QComboBox()
            self.master.addItems(["leave as is", "ON", "OFF"])
            m = step.get("master")
            self.master.setCurrentIndex(0 if m is None else (1 if m else 2))
            f.addRow("Master output", self.master)
        elif op == "smw":
            for key, label in SMW_KEYS:
                use = QtWidgets.QCheckBox(label)
                use.setChecked(key in step)
                if key == "format":
                    wdg = QtWidgets.QComboBox()
                    for lbl, mn in _smw_formats():
                        wdg.addItem(lbl, mn)
                    i = wdg.findData(step.get(key))
                    wdg.setCurrentIndex(max(0, i))
                elif key == "filter":
                    wdg = QtWidgets.QComboBox()
                    for lbl, mn in _smw_filters():
                        wdg.addItem(lbl, mn)
                    i = wdg.findData(step.get(key))
                    wdg.setCurrentIndex(max(0, i))
                elif key in ("dm", "mod_all", "rf"):
                    wdg = QtWidgets.QComboBox()
                    wdg.addItems(["ON", "OFF"])
                    wdg.setCurrentIndex(0 if step.get(key, True) else 1)
                else:
                    wdg = QtWidgets.QLineEdit("" if step.get(key) is None else f"{step[key]:.10g}")
                    wdg.setPlaceholderText({"freq": "2.4e9", "level": "-30", "srate": "10e6",
                                            "fparam": "0.35"}[key])
                f.addRow(use, wdg)
                self.w[key] = (use, wdg)
        elif op == "can":
            self.cmd = QtWidgets.QComboBox()
            for c in can_cmds:
                self.cmd.addItem(f"{c.label()}   {c.id_str()}: {c.data_str()}", c.uid)
            i = self.cmd.findData(step.get("cmd"))
            self.cmd.setCurrentIndex(max(0, i))
            f.addRow("Command (CAN library)", self.cmd)
            if not can_cmds:
                f.addRow(QtWidgets.QLabel("The CAN command library is empty - add commands on the CAN tab."))
        elif op == "wait":
            self.ms = QtWidgets.QSpinBox()
            self.ms.setRange(1, 86_400_000)
            self.ms.setSuffix(" ms")
            self.ms.setValue(step.get("ms", 1000))
            f.addRow("Pause", self.ms)
        elif op == "fsw_set":
            self.app = QtWidgets.QComboBox()
            self.app.addItem("VSA (K70)", "vsa")
            self.app.addItem("Spectrum", "spec")
            f.addRow("FSW application", self.app)
            self.pages = QtWidgets.QStackedWidget()
            self.fields = {}
            for app in ("vsa", "spec"):
                pw = QtWidgets.QWidget()
                pf = QtWidgets.QFormLayout(pw)
                pf.setContentsMargins(0, 0, 0, 0)
                cur = step.get("params", {}) if step.get("app", "vsa") == app else {}
                for key, label, kind, choices in fsw_params(host, app):
                    use = QtWidgets.QCheckBox(label)
                    use.setChecked(key in cur)
                    v = cur.get(key)
                    if kind == "b":
                        wdg = QtWidgets.QComboBox()
                        wdg.addItems(["ON", "OFF"])
                        wdg.setCurrentIndex(0 if v is None or v else 1)
                    elif kind == "c":
                        wdg = QtWidgets.QComboBox()
                        for lbl, data in choices:
                            wdg.addItem(lbl, data)
                        wdg.setCurrentIndex(max(0, wdg.findData(v)))
                    elif kind == "i":
                        wdg = QtWidgets.QSpinBox()
                        wdg.setRange(0, 100_000_000)
                        wdg.setValue(int(v) if v is not None else 0)
                    else:
                        wdg = QtWidgets.QLineEdit("" if v is None else f"{v:.10g}")
                        wdg.setPlaceholderText("e.g. 2.4G, 10M, 100k" if kind == "hz" else "")
                    pf.addRow(use, wdg)
                    self.fields[(app, key)] = (use, wdg, kind)
                sc = QtWidgets.QScrollArea()
                sc.setWidget(pw)
                sc.setWidgetResizable(True)
                sc.setMinimumSize(460, 420)
                self.pages.addWidget(sc)
            self.app.currentIndexChanged.connect(self.pages.setCurrentIndex)
            self.app.setCurrentIndex(0 if step.get("app", "vsa") == "vsa" else 1)
            f.addRow(self.pages)
            f.addRow(QtWidgets.QLabel("Only ticked parameters are sent; the rest stay as they are."))
        elif op in ("fsw", "scan"):
            self.action = QtWidgets.QComboBox()
            acts = (["run", "stop", "single", "autolevel"] if op == "fsw" else ["start", "stop"])
            self.action.addItems(acts)
            self.action.setCurrentText(step.get("action", acts[0]))
            f.addRow("Action", self.action)
        elif op == "repeat":
            self.count = QtWidgets.QSpinBox()
            self.count.setRange(0, 1_000_000)
            self.count.setSpecialValueText("forever")
            self.count.setValue(step.get("count", 3))
            f.addRow("Repeat count", self.count)
        elif op == "log":
            self.text = QtWidgets.QLineEdit(step.get("text", ""))
            f.addRow("Message", self.text)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def accept(self):
        s, op = {"op": self.step["op"]}, self.step["op"]
        try:
            if op == "hmp":
                s["ch"] = {str(ch): {"v": v.value(), "i": i.value(), "on": on.isChecked()}
                           for ch, (use, v, i, on) in self.w.items() if use.isChecked()}
                m = self.master.currentIndex()
                s["master"] = None if m == 0 else (m == 1)
                if not s["ch"] and s["master"] is None:
                    raise ValueError("nothing to do: select a channel or the master output")
            elif op == "smw":
                for key, (use, wdg) in self.w.items():
                    if not use.isChecked():
                        continue
                    if key in ("format", "filter"):
                        s[key] = wdg.currentData()
                    elif key in ("dm", "mod_all", "rf"):
                        s[key] = wdg.currentIndex() == 0
                    else:
                        s[key] = float(wdg.text().replace(",", "."))
                if len(s) == 1:
                    raise ValueError("select at least one parameter")
            elif op == "can":
                if self.cmd.currentData() is None:
                    raise ValueError("no CAN command selected")
                s["cmd"] = self.cmd.currentData()
            elif op == "wait":
                s["ms"] = self.ms.value()
            elif op == "fsw_set":
                app = self.app.currentData()
                s["app"], s["params"] = app, {}
                for (a, key), (use, wdg, kind) in self.fields.items():
                    if a != app or not use.isChecked():
                        continue
                    if kind == "b":
                        s["params"][key] = wdg.currentIndex() == 0
                    elif kind == "c":
                        s["params"][key] = wdg.currentData()
                    elif kind == "i":
                        s["params"][key] = wdg.value()
                    elif kind == "hz":
                        s["params"][key] = parse_si(wdg.text())
                    else:
                        s["params"][key] = float(wdg.text().replace(",", "."))
                if not s["params"]:
                    raise ValueError("select at least one parameter")
            elif op in ("fsw", "scan"):
                s["action"] = self.action.currentText()
            elif op == "repeat":
                s["count"] = self.count.value()
            elif op == "log":
                s["text"] = self.text.text()
            else:
                s = self.step
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "Step", str(e))
            return
        self.step = s
        super().accept()


def _smw_formats():
    try:
        import smw_generator
        return smw_generator.FORMATS
    except Exception:
        return [("QPSK", "QPSK")]


def _smw_filters():
    try:
        import smw_generator
        return [(lbl, mn) for lbl, mn, _, _ in smw_generator.FILTERS]
    except Exception:
        return [("Root raised cosine", "RCOSine")]


# --------------------------------------------------------------------------- #
#  Dashboard                                                                   #
# --------------------------------------------------------------------------- #
class Dashboard(QtWidgets.QWidget):
    DEVICES = ["FSW", "HMP4040", "SMW200A", "34405A", "Arduino", "CAN"]

    def __init__(self, host):
        super().__init__()
        self.host = host
        self.data = {"scripts": {"Sequence 1": []}, "watch": [], "volt_channels": "0-7"}
        self.load_error = ""
        try:
            with open(STORE_FILE, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except FileNotFoundError:
            pass
        except Exception as e:
            self.load_error = f"cannot read {STORE_FILE}: {e}"
        if not self.data["scripts"]:
            self.data["scripts"] = {"Sequence 1": []}
        self.running = False
        self.pc, self.stack, self.pending, self.run_steps = 0, [], None, []
        self.wait_until = 0.0
        self.watch_rows: list = []            # (unit, type, Field)
        self.tlog: TelemetryCsv | None = None
        self._tlog_last: dict = {}             # (unit, type, Field) -> last logged value
        self.volt_curves = {}
        self._range = None

        self._build()
        self.timer = QtCore.QTimer(self, interval=500, timeout=self._tick)
        self.timer.start()
        self.runner = QtCore.QTimer(self, singleShot=True, timeout=self._step)
        if self.load_error:
            QtCore.QTimer.singleShot(0, lambda: self.log(self.load_error, "err"))

    # ---------------------------------------------------------------- build
    def _build(self):
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        root.addWidget(split)

        # --- left: status + sequences
        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        g = QtWidgets.QGroupBox("Devices")
        grid = QtWidgets.QGridLayout(g)
        grid.setVerticalSpacing(2)
        self.status = {}
        for r, name in enumerate(self.DEVICES):
            led = QtWidgets.QLabel("●")
            led.setStyleSheet(f"color:{C_OFF};font-size:16px;")
            nm = QtWidgets.QLabel(f"<b>{name}</b>")
            info = QtWidgets.QLabel("—")
            info.setStyleSheet(f"color:{C_DIM};")
            act = QtWidgets.QLabel("")
            act.setStyleSheet("font-weight:700;")
            grid.addWidget(led, r, 0)
            grid.addWidget(nm, r, 1)
            grid.addWidget(act, r, 2)
            grid.addWidget(info, r, 3)
            self.status[name] = (led, info, act)
        grid.setColumnStretch(3, 1)
        lv.addWidget(g)

        g = QtWidgets.QGroupBox("HMP4040 outputs")
        hg = QtWidgets.QGridLayout(g)
        hg.setVerticalSpacing(2)
        for col, text in enumerate(("", "Ch", "Set", "Measured", "Power", "")):
            lb = QtWidgets.QLabel(text)
            lb.setStyleSheet(f"color:{C_DIM};")
            hg.addWidget(lb, 0, col)
        mono = QtGui.QFont("Consolas")
        mono.setStyleHint(QtGui.QFont.Monospace)
        self.hmp_rows = {}
        for r, ch in enumerate((1, 2, 3, 4), start=1):
            led = QtWidgets.QLabel("●")
            led.setStyleSheet(f"color:{C_OFF};font-size:16px;")
            cells = [QtWidgets.QLabel("—") for _ in range(3)]
            for c in cells:
                c.setFont(mono)
            mode = QtWidgets.QLabel("")
            mode.setStyleSheet("font-weight:700;")
            hg.addWidget(led, r, 0)
            hg.addWidget(QtWidgets.QLabel(f"<b>CH{ch}</b>"), r, 1)
            for k, c in enumerate(cells):
                hg.addWidget(c, r, 2 + k)
            hg.addWidget(mode, r, 5)
            self.hmp_rows[ch] = (led, *cells, mode)
        hg.setColumnStretch(3, 1)
        lv.addWidget(g)

        self.temp_box = QtWidgets.QGroupBox("Temperatures (1-Wire)")
        self.temp_grid = QtWidgets.QGridLayout(self.temp_box)
        self.temp_grid.setVerticalSpacing(2)
        self.temp_rows: dict = {}          # rom -> (led, name, value)
        self.temp_box.hide()               # shown once a sensor has been seen
        lv.addWidget(self.temp_box)

        g = QtWidgets.QGroupBox("Test sequence")
        sv = QtWidgets.QVBoxLayout(g)
        top = QtWidgets.QHBoxLayout()
        self.seq_cb = QtWidgets.QComboBox()
        self.seq_cb.addItems(list(self.data["scripts"]))
        self.seq_cb.currentIndexChanged.connect(lambda *_: self.reload())
        top.addWidget(self.seq_cb, 1)
        for text, fn in (("New", self.new_seq), ("Rename", self.rename_seq), ("Delete", self.delete_seq)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            top.addWidget(b)
        sv.addLayout(top)
        self.list = QtWidgets.QListWidget()
        self.list.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.list.itemDoubleClicked.connect(lambda *_: self.edit_step())
        self.list.setToolTip("Double-click to edit a step")
        sv.addWidget(self.list, 1)
        add = QtWidgets.QGridLayout()
        for k, (text, op) in enumerate((("+ HMP", "hmp"), ("+ SMW", "smw"), ("+ CAN", "can"),
                                        ("+ Pause", "wait"), ("+ FSW", "fsw"), ("+ FSW settings", "fsw_set"),
                                        ("+ Scan", "scan"), ("+ Repeat", "repeat"), ("+ Log", "log"))):
            b = QtWidgets.QPushButton(text)
            b.setStyleSheet(f"color:{STEP_COLORS[op]};")
            b.clicked.connect(lambda _, op=op: self.add_step(op))
            add.addWidget(b, k // 3, k % 3)
        sv.addLayout(add)
        ed = QtWidgets.QHBoxLayout()
        for text, fn in (("▲", lambda: self.move(-1)), ("▼", lambda: self.move(1)), ("Copy", self.duplicate),
                         ("Edit", self.edit_step), ("Delete", self.delete_steps)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            ed.addWidget(b)
        sv.addLayout(ed)
        run = QtWidgets.QHBoxLayout()
        self.run_btn = QtWidgets.QPushButton("▶ Run sequence")
        self.run_btn.setCheckable(True)
        self.run_btn.clicked.connect(self.toggle_run)
        self.loop_chk = QtWidgets.QCheckBox("Loop")
        self.seq_status = QtWidgets.QLabel("")
        self.seq_status.setStyleSheet(f"color:{C_DIM};")
        run.addWidget(self.run_btn)
        run.addWidget(self.loop_chk)
        run.addWidget(self.seq_status, 1)
        sv.addLayout(run)
        lv.addWidget(g, 1)
        left.setMinimumWidth(430)
        split.addWidget(left)

        # --- right: constellation / voltages / telemetry
        right = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        top_w = QtWidgets.QWidget()
        th = QtWidgets.QHBoxLayout(top_w)
        th.setContentsMargins(0, 0, 0, 0)
        self.const = pg.PlotWidget(title="Constellation (FSW VSA)")
        self.const.setAspectLocked(True)
        self.const.showGrid(x=True, y=True, alpha=0.25)
        for ax in ("left", "bottom"):
            self.const.getAxis(ax).enableAutoSIPrefix(False)
        self.const.getPlotItem().setMenuEnabled(False)
        self.const_pts = pg.ScatterPlotItem(size=3, pen=None, symbol="s", brush=pg.mkBrush(255, 196, 0, 150))
        self.const.addItem(self.const_pts)
        self.const.setRange(xRange=(-1.5, 1.5), yRange=(-1.5, 1.5))
        th.addWidget(self.const, 1)
        tiles = QtWidgets.QVBoxLayout()
        self.tiles = {}
        for key, title, color in (("mer", "MER RMS", "#4fd18b"), ("power", "Power", "#58a6ff"),
                                  ("cfe", "Carrier Freq Error", "#f0883e")):
            box = QtWidgets.QFrame()
            box.setStyleSheet("QFrame{background:#181c22;border:1px solid #2a3038;border-radius:8px;}")
            bl = QtWidgets.QVBoxLayout(box)
            bl.setContentsMargins(10, 4, 10, 4)
            t = QtWidgets.QLabel(title)
            t.setStyleSheet("color:#8a96a3;font-size:11px;font-weight:600;border:0;")
            v = QtWidgets.QLabel("—")
            v.setStyleSheet(f"color:{color};font-size:22px;font-weight:700;border:0;")
            bl.addWidget(t)
            bl.addWidget(v)
            tiles.addWidget(box)
            self.tiles[key] = v
        tiles.addStretch(1)
        tw = QtWidgets.QWidget()
        tw.setLayout(tiles)
        tw.setFixedWidth(210)
        th.addWidget(tw)
        right.addWidget(top_w)

        vw = QtWidgets.QWidget()
        vv = QtWidgets.QVBoxLayout(vw)
        vv.setContentsMargins(0, 0, 0, 0)
        vb = QtWidgets.QHBoxLayout()
        vb.addWidget(QtWidgets.QLabel("Voltage channels"))
        self.volt_ch = QtWidgets.QLineEdit(self.data.get("volt_channels", "0-7"))
        self.volt_ch.setToolTip("Bench channels to plot, e.g. 0-7, 144, 160-163")
        self.volt_ch.editingFinished.connect(self._volt_channels_changed)
        vb.addWidget(self.volt_ch, 1)
        self.volt_win = QtWidgets.QSpinBox()
        self.volt_win.setRange(10, 86400)
        self.volt_win.setValue(120)
        self.volt_win.setPrefix("last ")
        self.volt_win.setSuffix(" s")
        vb.addWidget(self.volt_win)
        vv.addLayout(vb)
        self.volt = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self.volt.showGrid(x=True, y=True, alpha=0.25)
        self.volt.setLabel("left", "Voltage", units="V")
        self.volt.getPlotItem().setMenuEnabled(False)
        self.volt.getPlotItem().setAutoVisible(y=True)
        self.volt.addLegend(offset=(5, 5), labelTextSize="8pt")
        vv.addWidget(self.volt, 1)
        right.addWidget(vw)

        ww = QtWidgets.QWidget()
        wv = QtWidgets.QVBoxLayout(ww)
        wv.setContentsMargins(0, 0, 0, 0)
        pick = QtWidgets.QHBoxLayout()
        pick.addWidget(QtWidgets.QLabel("Telemetry watch"))
        self.w_unit = QtWidgets.QComboBox()
        self.w_arr = QtWidgets.QComboBox()
        self.w_field = QtWidgets.QComboBox()
        self.w_field.setMinimumWidth(180)
        self.w_unit.currentIndexChanged.connect(self._fill_arrays)
        self.w_arr.currentIndexChanged.connect(self._fill_fields)
        add_b = QtWidgets.QPushButton("+ Add")
        add_b.clicked.connect(self.add_watch)
        rm_b = QtWidgets.QPushButton("Remove")
        rm_b.clicked.connect(self.remove_watch)
        for wdg in (self.w_unit, self.w_arr, self.w_field):
            pick.addWidget(wdg)
        pick.addWidget(add_b)
        pick.addWidget(rm_b)
        pick.addStretch(1)
        self.tlog_chk = QtWidgets.QCheckBox("Log to CSV")
        self.tlog_chk.setToolTip("telemetry_log_YYYY-MM-DD.csv next to the program:\n"
                                 "one row per update of each watched value")
        self.tlog_chg = QtWidgets.QCheckBox("only changes")
        self.tlog_chg.setToolTip("Write a row only when a value differs from the last logged one")
        self.tlog_chk.setChecked(self.data.get("tlog", False))
        self.tlog_chg.setChecked(self.data.get("tlog_changes", True))
        self.tlog_chk.toggled.connect(self._on_tlog_toggled)
        self.tlog_chg.toggled.connect(lambda *_: self.save())
        pick.addWidget(self.tlog_chk)
        pick.addWidget(self.tlog_chg)
        wv.addLayout(pick)
        self.watch = QtWidgets.QTableWidget(0, 5)
        self.watch.setHorizontalHeaderLabels(["Source", "Designation", "Value", "Meaning", "Updated"])
        self.watch.verticalHeader().hide()
        self.watch.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.watch.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        for c, w in enumerate((130, 160, 150, 250)):
            self.watch.setColumnWidth(c, w)
        self.watch.horizontalHeader().setStretchLastSection(True)
        wv.addWidget(self.watch, 1)
        right.addWidget(ww)
        right.setSizes([380, 260, 200])
        split.addWidget(right)
        split.setSizes([470, 900])

        self._fill_units()
        for unit, t, byte, name in self.data.get("watch", []):
            f = self._find_field(unit, t, byte, name)
            if f is not None:
                self._add_watch_row(unit, t, f)
        self.reload()
        self._on_tlog_toggled(self.tlog_chk.isChecked(), save=False)

    # ------------------------------------------------------------- helpers
    def log(self, text, level="info"):
        self.host.logmsg(text, level)

    def save(self):
        self.data["watch"] = [[u, t, f.byte, f.name] for u, t, f in self.watch_rows]
        self.data["volt_channels"] = self.volt_ch.text()
        self.data["tlog"] = self.tlog_chk.isChecked()
        self.data["tlog_changes"] = self.tlog_chg.isChecked()
        try:
            tmp = STORE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1, ensure_ascii=False)
            os.replace(tmp, STORE_FILE)
        except OSError as e:
            self.log(f"cannot save {STORE_FILE}: {e}", "err")

    # -------------------------------------------------------------- status
    def _set_status(self, name, state, info="", act=""):
        led, inf, a = self.status[name]
        led.setStyleSheet(f"color:{ {'ok': C_OK, 'off': C_OFF, 'err': C_ERR}[state] };font-size:16px;")
        inf.setText(info)
        a.setText(act)

    def _tick(self):
        h = self.host
        idn = h.idn_label.text().split("\n")[0] if h.connected else "not connected"
        self._set_status("FSW", "ok" if h.connected else "off", idn,
                         "measuring" if h.connected and h.run_btn.isChecked() else "")
        b = getattr(h, "bench", None)
        if b:
            m = b._hmp_ok and b._hmp_master
            self._set_status("HMP4040", "ok" if b._hmp_ok else "off", b.hmp_status.text(),
                             "MASTER ON" if m else "")
            for name in ("34405A", "Arduino"):
                self._set_status(name, "ok" if b._scan_ok else "off", b.scan_status.text(),
                                 "scanning" if b._scan_ok and b.run_btn.isChecked() else "")
            self._refresh_hmp(b)
        tt = getattr(h, "temps", None)
        if tt:
            self._refresh_temps(tt)
        smw = getattr(h, "smw", None)
        if smw:
            rf = smw.is_connected and smw.rf_btn.isChecked()
            self._set_status("SMW200A", "ok" if smw.is_connected else "off",
                             smw.idn or "not connected", "RF ON" if rf else "")
        c = getattr(h, "can_win", None)
        if c:
            ok = c.worker is not None and c.worker.backend is not None
            self._set_status("CAN", "ok" if ok else "off", c.status.text().lstrip("● "),
                             f"{c.n_rx:,} frames" if ok else "")
        self._refresh_volt()

    def _refresh_temps(self, tt):
        """Name + temperature of every 1-Wire sensor; red = error, grey = not found in the last poll."""
        if list(self.temp_rows) != sorted(tt.readings):
            for w in (x for row in self.temp_rows.values() for x in row):
                w.deleteLater()
            self.temp_rows = {}
            mono = QtGui.QFont("Consolas")
            mono.setStyleHint(QtGui.QFont.Monospace)
            for k, rom in enumerate(sorted(tt.readings)):
                r, c0 = k // 2, (k % 2) * 4            # two sensors per row
                led = QtWidgets.QLabel("●")
                name = QtWidgets.QLabel()
                val = QtWidgets.QLabel()
                val.setFont(mono)
                val.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                self.temp_grid.addWidget(led, r, c0)
                self.temp_grid.addWidget(name, r, c0 + 1)
                self.temp_grid.addWidget(val, r, c0 + 2)
                self.temp_rows[rom] = (led, name, val)
            self.temp_grid.setColumnStretch(1, 1)
            self.temp_grid.setColumnStretch(5, 1)
            self.temp_grid.setColumnMinimumWidth(3, 14)
            self.temp_box.setVisible(bool(self.temp_rows))
        for rom, (led, name, val) in self.temp_rows.items():
            d = tt.readings[rom]
            name.setText(f"<b>{tt.names.get(rom) or 'sensor ' + rom[-4:]}</b>")
            name.setToolTip(f"ROM {rom}\n{d['status'] if not d['missing'] else 'not found in last poll'}")
            if d["missing"]:
                color, text = C_OFF, "missing"
            elif not d["valid"]:
                color, text = C_ERR, "error"
            else:
                color, text = C_OK, f"{d['temp']:7.2f} °C"
            led.setStyleSheet(f"color:{color};font-size:16px;")
            val.setText(text)
            val.setStyleSheet("" if d["valid"] else f"color:{color};")

    def _refresh_hmp(self, b):
        """Per-channel HMP4040 state: LED green = output delivering (channel on + master on)."""
        for ch, (led, set_l, meas_l, pwr_l, mode_l) in self.hmp_rows.items():
            st = b.hmp_last_state.get(ch) if b._hmp_ok else None
            if st is None:
                led.setStyleSheet(f"color:{C_OFF};font-size:16px;")
                for lb in (set_l, meas_l, pwr_l):
                    lb.setText("—")
                mode_l.setText("")
                continue
            vset, iset, on = st
            live = on and b._hmp_master
            v, i = b.hmp_last_meas.get(ch, (0.0, 0.0))
            color = C_OK if live else ("#f0b03e" if on else C_OFF)
            led.setStyleSheet(f"color:{color};font-size:16px;")
            set_l.setText(f"{vset:6.3f} V {iset:6.3f} A")
            meas_l.setText(f"{v:6.3f} V {i:6.3f} A" if live else "—")
            pwr_l.setText(f"{v * i:6.2f} W" if live else "—")
            if live and iset > 0 and i >= 0.98 * iset and v < 0.98 * vset:   # current limit reached
                mode_l.setText("CC")
                mode_l.setStyleSheet(f"font-weight:700;color:{C_ERR};")
            else:
                mode_l.setText("ON" if live else ("armed" if on else "OFF"))
                mode_l.setStyleSheet("font-weight:700;" + (f"color:{C_OK};" if live else f"color:{C_DIM};"))

    # ------------------------------------------------------- constellation
    def update_vsa(self, r: dict):
        i, q = r["i"], r["q"]
        if i.size > 8000:
            step = int(math.ceil(i.size / 8000))
            i, q = i[::step], q[::step]
        self.const_pts.setData(x=i, y=q)
        if i.size:
            m = float(np.percentile(np.maximum(np.abs(i), np.abs(q)), 99.9)) * 1.15 or 1.0
            if self._range is None or not 0.85 < m / self._range < 1.15:
                self._range = m
                self.const.setRange(xRange=(-m, m), yRange=(-m, m), padding=0)
        for k, fmt in (("mer", "{:.2f} dB"), ("power", "{:.2f} dBm"), ("cfe", "{:+.1f} Hz")):
            v = r.get(k)
            self.tiles[k].setText(fmt.format(v) if v is not None and math.isfinite(v) else "—")

    # ------------------------------------------------------------ voltages
    def _volt_channels_changed(self):
        try:
            parse_channels(self.volt_ch.text())
            self.volt_ch.setStyleSheet("")
        except ValueError:
            self.volt_ch.setStyleSheet("border-color:#f85149")
            return
        for c in self.volt_curves.values():
            self.volt.removeItem(c)
        self.volt_curves = {}
        self.volt.plotItem.legend.clear()
        self.save()
        self._refresh_volt()

    def _refresh_volt(self):
        b = getattr(self.host, "bench", None)
        if not b:
            return
        try:
            chans = parse_channels(self.volt_ch.text())
        except ValueError:
            return
        for ch in chans:
            if ch not in self.volt_curves:
                color = pg.intColor(ch, hues=22, values=8, minValue=140, maxValue=255)
                self.volt_curves[ch] = self.volt.plot([], [], pen=pg.mkPen(color, width=1.5),
                                                      name=f"{ch} {b.names.get(ch, '')}")
            ts, vs = b.hist[ch]
            if ts:
                self.volt_curves[ch].setData(np.fromiter(ts, float, len(ts)), np.fromiter(vs, float, len(vs)))
        now = time.time()
        self.volt.setXRange(now - self.volt_win.value(), now, padding=0)

    # ----------------------------------------------------------- telemetry
    def _layouts(self):
        if telemetry is None:
            return {}
        return telemetry.TelemetryTab.LAYOUTS

    def _fill_units(self):
        self.w_unit.clear()
        for unit, _, arrays in (telemetry.UNITS if telemetry else []):
            if any((unit, t) in self._layouts() for t, _, _ in arrays):
                self.w_unit.addItem(unit, unit)

    def _fill_arrays(self, *_):
        self.w_arr.clear()
        unit = self.w_unit.currentData()
        for u, _, arrays in (telemetry.UNITS if telemetry else []):
            if u == unit:
                for t, name, _ in arrays:
                    if (unit, t) in self._layouts():
                        self.w_arr.addItem(f"{name} (0x{t:02X})", t)

    def _fill_fields(self, *_):
        self.w_field.clear()
        key = (self.w_unit.currentData(), self.w_arr.currentData())
        for f in self._layouts().get(key, ([], ""))[0]:
            if f.kind != "res":
                self.w_field.addItem(f"{f.name}  [byte {f.pos()[0]}]", (f.byte, f.name))

    def _find_field(self, unit, t, byte, name):
        for f in self._layouts().get((unit, t), ([], ""))[0]:
            if f.byte == byte and f.name == name:
                return f
        return None

    def _add_watch_row(self, unit, t, f):
        r = self.watch.rowCount()
        self.watch.insertRow(r)
        arr = next((n for u, _, arrays in telemetry.UNITS if u == unit for tt, n, _ in arrays if tt == t), "?")
        for c, text in enumerate((f"{unit} · {arr}", f.name, "—", "", "")):
            it = QtWidgets.QTableWidgetItem(text)
            if c == 1:
                it.setToolTip(f.desc)
            self.watch.setItem(r, c, it)
        self.watch_rows.append((unit, t, f))

    def add_watch(self):
        unit, t, sel = self.w_unit.currentData(), self.w_arr.currentData(), self.w_field.currentData()
        if sel is None:
            return
        f = self._find_field(unit, t, *sel)
        if f is None or any(u == unit and tt == t and ff is f for u, tt, ff in self.watch_rows):
            return
        self._add_watch_row(unit, t, f)
        self.save()

    def remove_watch(self):
        rows = sorted({i.row() for i in self.watch.selectionModel().selectedRows()}, reverse=True)
        for r in rows:
            self.watch.removeRow(r)
            del self.watch_rows[r]
        self.save()

    def _on_tlog_toggled(self, on, save=True):
        if self.tlog:
            self.tlog.close()
        self.tlog = TelemetryCsv() if on else None
        self._tlog_last.clear()
        if save:
            self.save()
            self.log("Telemetry watch logging " + ("ON (telemetry_log_*.csv)" if on else "OFF"))

    def on_array(self, unit, t, data, ts):
        """Listener for TelemetryTab.array_listeners."""
        stamp = time.strftime("%H:%M:%S", time.localtime(ts))
        for r, (u, tt, f) in enumerate(self.watch_rows):
            if u != unit or tt != t:
                continue
            v = f.raw(data)
            if v is None:
                continue
            text, warn = f.meaning(v)
            warn = warn or (f.name, v) in telemetry.WARN_ENUM
            self.watch.item(r, 2).setText(f.value_str(v))
            mi = self.watch.item(r, 3)
            mi.setText(text)
            mi.setForeground(QtGui.QColor(C_ERR) if warn else QtGui.QBrush())
            self.watch.item(r, 4).setText(stamp)
            if self.tlog:
                key = (u, tt, f)
                if self.tlog_chg.isChecked() and self._tlog_last.get(key) == v:
                    continue
                self._tlog_last[key] = v
                try:
                    self.tlog.write(ts, u, self.watch.item(r, 0).text().split(" · ", 1)[-1],
                                    f, f.value_str(v), text, warn)
                except OSError as e:
                    self.log(f"Telemetry log stopped: {e}", "err")
                    self.tlog_chk.setChecked(False)

    # ----------------------------------------------------------- sequences
    @property
    def steps(self) -> list:
        return self.data["scripts"].setdefault(self.seq_cb.currentText(), [])

    def _can_cmds(self):
        c = getattr(self.host, "can_win", None)
        return list(c.store.commands) if c else []

    def step_text(self, s) -> str:
        op = s["op"]
        if op == "hmp":
            parts = [f"OUT{ch} {p['v']:g} V / {p['i']:g} A {'on' if p['on'] else 'off'}"
                     for ch, p in sorted(s.get("ch", {}).items())]
            if s.get("master") is not None:
                parts.append(f"MASTER {'ON' if s['master'] else 'OFF'}")
            return "⚡ HMP   " + ", ".join(parts)
        if op == "smw":
            parts = []
            for key, label in SMW_KEYS:
                if key not in s:
                    continue
                v = s[key]
                if key in ("freq", "srate"):
                    parts.append(f"{label.split(',')[0]} {fmt_hz(v)}")
                elif key in ("dm", "mod_all", "rf"):
                    parts.append(f"{label.replace(' on', '')} {'ON' if v else 'OFF'}")
                elif key == "level":
                    parts.append(f"{v:g} dBm")
                elif key == "fparam":
                    parts.append(f"α/BT {v:g}")
                else:
                    parts.append(str(v))
            return "📡 SMW   " + ", ".join(parts)
        if op == "can":
            c = next((c for c in self._can_cmds() if c.uid == s.get("cmd")), None)
            return ("✉ CAN   " + (f"{c.label()}  {c.id_str()}: {c.data_str()}" if c else "<missing command>"))
        if op == "wait":
            return f"⏱ Pause {s['ms']} ms"
        if op == "fsw":
            return "〰 FSW   " + {"single": "single measurement (wait for result)",
                                  "autolevel": "auto level"}.get(s["action"], s["action"])
        if op == "fsw_set":
            parts = []
            for key, label, kind, _ in fsw_params(self.host, s["app"]):
                if key not in s["params"]:
                    continue
                v = s["params"][key]
                name = label.split(",")[0].split(" (")[0]
                if kind == "hz":
                    parts.append(f"{name} {fmt_hz(v) if 'Hz' in label or 'sym' in label else f'{v:g}'}")
                elif kind == "b":
                    parts.append(f"{name} {'ON' if v else 'OFF'}")
                elif key == "prat":
                    sr = s["params"].get("symbol_rate")
                    parts.append(f"OVS ×{v}" + (f" (sample rate {fmt_hz(sr * v)})" if sr else ""))
                else:
                    parts.append(f"{name} {v:g}" if isinstance(v, float) else f"{name} {v}")
            return f"〰 FSW {'VSA' if s['app'] == 'vsa' else 'Spectrum'}   " + ", ".join(parts)
        if op == "scan":
            return f"⎍ Voltage scan {s['action']}"
        if op == "repeat":
            return f"⟳ Repeat {'forever' if s['count'] == 0 else str(s['count']) + ' ×'}"
        if op == "end":
            return "⟲ End repeat"
        return f"✎ Log   {s.get('text', '')}"

    def reload(self, select=()):
        self.list.clear()
        depth = 0
        for i, s in enumerate(self.steps):
            if s["op"] == "end":
                depth = max(0, depth - 1)
            it = QtWidgets.QListWidgetItem("    " * depth + self.step_text(s))
            missing = s["op"] == "can" and "<missing" in it.text()
            it.setForeground(QtGui.QColor(C_ERR if missing else STEP_COLORS.get(s["op"], "#c9d1d9")))
            self.list.addItem(it)
            if i in select:
                it.setSelected(True)
            if s["op"] == "repeat":
                depth += 1
        if not self.running:
            err = check_steps(self.steps)
            self.seq_status.setText(err or f"{len(self.steps)} steps")
            self.seq_status.setStyleSheet(f"color:{C_ERR if err else C_DIM};")

    def _sel(self):
        return sorted(self.list.row(i) for i in self.list.selectedItems())

    def add_step(self, op):
        if self.running:
            return
        if op == "repeat":
            rows = self._sel()
            if rows:
                dlg = StepDialog(self, {"op": "repeat", "count": 3}, [])
                if dlg.exec_() != QtWidgets.QDialog.Accepted:
                    return
                self.steps.insert(rows[-1] + 1, {"op": "end"})
                self.steps.insert(rows[0], dlg.step)
                self.save()
                self.reload({rows[0]})
                return
        defaults = {"hmp": {"ch": {"1": {"v": 16.0, "i": 1.0, "on": True}}, "master": True},
                    "smw": {"freq": 2.4e9, "level": -30.0, "rf": True}, "wait": {"ms": 1000},
                    "fsw": {"action": "run"}, "scan": {"action": "start"},
                    "fsw_set": {"app": "vsa", "params": {}}, "repeat": {"count": 3},
                    "log": {"text": ""}, "can": {}}
        dlg = StepDialog(self, {"op": op, **defaults[op]}, self._can_cmds(), self.host)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        rows = self._sel()
        pos = rows[-1] + 1 if rows else len(self.steps)
        new = [dlg.step] + ([{"op": "end"}] if op == "repeat" else [])
        self.steps[pos:pos] = new
        self.save()
        self.reload({pos})

    def edit_step(self):
        rows = self._sel()
        if self.running or len(rows) != 1 or self.steps[rows[0]]["op"] == "end":
            return
        dlg = StepDialog(self, self.steps[rows[0]], self._can_cmds(), self.host)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            self.steps[rows[0]] = dlg.step
            self.save()
            self.reload({rows[0]})

    def delete_steps(self):
        rows = self._sel()
        if self.running or not rows:
            return
        for r in reversed(rows):
            del self.steps[r]
        self.save()
        self.reload()

    def duplicate(self):
        rows = self._sel()
        if self.running or not rows:
            return
        pos = rows[-1] + 1
        self.steps[pos:pos] = [json.loads(json.dumps(self.steps[r])) for r in rows]
        self.save()
        self.reload(set(range(pos, pos + len(rows))))

    def move(self, d):
        rows = self._sel()
        if self.running or not rows:
            return
        st, a, b = self.steps, rows[0], rows[-1]
        if d < 0 and a > 0:
            st.insert(b, st.pop(a - 1))
        elif d > 0 and b < len(st) - 1:
            st.insert(a, st.pop(b + 1))
        else:
            return
        self.save()
        self.reload({r + d for r in rows})

    def new_seq(self):
        n = len(self.data["scripts"]) + 1
        name, ok = QtWidgets.QInputDialog.getText(self, "New sequence", "Name:", text=f"Sequence {n}")
        name = name.strip()
        if ok and name and name not in self.data["scripts"]:
            self.data["scripts"][name] = []
            self.seq_cb.addItem(name)
            self.seq_cb.setCurrentText(name)
            self.save()

    def rename_seq(self):
        old = self.seq_cb.currentText()
        name, ok = QtWidgets.QInputDialog.getText(self, "Rename sequence", "Name:", text=old)
        name = name.strip()
        if ok and name and name != old and name not in self.data["scripts"]:
            self.data["scripts"] = {(name if k == old else k): v for k, v in self.data["scripts"].items()}
            self.seq_cb.setItemText(self.seq_cb.currentIndex(), name)
            self.save()

    def delete_seq(self):
        if self.running:
            return
        name = self.seq_cb.currentText()
        if QtWidgets.QMessageBox.question(self, "Delete", f"Delete sequence '{name}'?") != QtWidgets.QMessageBox.Yes:
            return
        del self.data["scripts"][name]
        if not self.data["scripts"]:
            self.data["scripts"]["Sequence 1"] = []
        self.seq_cb.blockSignals(True)
        self.seq_cb.clear()
        self.seq_cb.addItems(list(self.data["scripts"]))
        self.seq_cb.blockSignals(False)
        self.save()
        self.reload()

    # -------------------------------------------------------------- runner
    def toggle_run(self):
        if self.running:
            self.stop("stopped by user", "warn")
            return
        err = check_steps(self.steps) or ("sequence is empty" if not self.steps else "")
        if err:
            self.log(f"sequence '{self.seq_cb.currentText()}': {err}", "err")
            self.run_btn.setChecked(False)
            return
        self.running = True
        self.run_steps = json.loads(json.dumps(self.steps))
        self.pc, self.stack, self.pending, self.wait_until = 0, [], None, 0.0
        self.run_btn.setText("■ Stop")
        self.run_btn.setChecked(True)
        self.seq_cb.setEnabled(False)
        self.log(f"sequence '{self.seq_cb.currentText()}' started", "ok")
        self.runner.start(0)

    def stop(self, why="", level="info"):
        self.runner.stop()
        if not self.running:
            return
        self.running = False
        self.run_btn.setChecked(False)
        self.run_btn.setText("▶ Run sequence")
        self.seq_cb.setEnabled(True)
        self._mark(-1)
        self.log(f"sequence '{self.seq_cb.currentText()}' {why}", level)
        self.reload()

    def _mark(self, pc):
        for i in range(self.list.count()):
            self.list.item(i).setBackground(QtGui.QColor("#1f6f43") if i == pc else QtGui.QBrush())
        if 0 <= pc < self.list.count():
            self.list.scrollToItem(self.list.item(pc))

    def _fail(self, msg):
        self.stop(f"ABORTED at step {self.pc + 1}: {msg}", "err")

    def _step(self):
        if not self.running:
            return
        if self.pending is not None:                 # device step still executing
            if not self.pending.done:
                if self.pending.deadline and time.monotonic() > self.pending.deadline:
                    self._await_meas = None
                    return self._fail(f"{self.pending.what}: timeout")
                self.runner.start(20)
                return
            tok, self.pending = self.pending, None
            if tok.error:
                return self._fail(f"{tok.what}: {tok.error}")
            msg = tok.msg or getattr(getattr(tok, "msg_from", None), "msg", "")
            if msg:
                self.log(f"[sequence] {msg}", "ok")
            if tok.what.startswith("FSW") and self.host.connected:
                self.host.on_read()                  # show what the FSW now uses
            self.pc += 1
        if time.monotonic() < self.wait_until:
            left = self.wait_until - time.monotonic()
            self.seq_status.setText(f"step {self.pc}/{len(self.run_steps)}: pause, {left:.1f} s left")
            self.runner.start(min(100, int(left * 1000) + 1))
            return
        for _ in range(200):                         # quick steps per tick
            if self.pc >= len(self.run_steps):
                if self.loop_chk.isChecked():
                    self.pc, self.stack = 0, []
                    self.runner.start(1)
                    return
                return self.stop("finished", "ok")
            s = self.run_steps[self.pc]
            self._mark(self.pc)
            self.seq_status.setText(f"running: step {self.pc + 1}/{len(self.run_steps)}")
            self.seq_status.setStyleSheet(f"color:{C_OK};")
            try:
                result = self._exec(s)
            except Exception as e:
                return self._fail(f"{type(e).__name__}: {e}")
            if isinstance(result, Token):
                self.pending = result
                self.runner.start(20)
                return
            if s["op"] == "wait":
                self.pc += 1
                self.wait_until = time.monotonic() + s["ms"] / 1000
                self.runner.start(min(100, s["ms"]))
                return
            if s["op"] not in ("repeat", "end"):
                self.pc += 1
        self.runner.start(1)

    def _exec(self, s):
        op, h = s["op"], self.host
        if op == "repeat":
            self.stack.append([self.pc + 1, s["count"]])
            self.pc += 1
        elif op == "end":
            top = self.stack[-1]
            if top[1] == 0:
                self.pc = top[0]
            elif top[1] > 1:
                top[1] -= 1
                self.pc = top[0]
            else:
                self.stack.pop()
                self.pc += 1
        elif op == "log":
            self.log(f"[sequence] {s.get('text', '')}", "info")
        elif op == "hmp":
            b = getattr(h, "bench", None)
            if not b or not b._hmp_ok:
                raise RuntimeError("HMP4040 not connected")
            w = b.hmp

            def job():
                for ch, p in sorted(s.get("ch", {}).items()):
                    w.set_channel(int(ch), p["v"], p["i"], p["on"])
                if s.get("master") is not None:
                    w.set_master(s["master"])
            return run_in(w.submit, job, "HMP4040")
        elif op == "smw":
            smw = getattr(h, "smw", None)
            if not smw or not smw.is_connected:
                raise RuntimeError("SMW200A not connected")
            params = {k: s[k] for k, _ in SMW_KEYS if k in s}
            return run_in(smw.worker.submit, lambda: smw.worker.apply(params, prev={}, label="sequence"),
                          "SMW200A")
        elif op == "can":
            c = getattr(h, "can_win", None)
            cmd = c.store.by_uid(s.get("cmd")) if c else None
            if cmd is None:
                raise RuntimeError("CAN command not found in the library")
            if not c.send_frame(cmd.to_frame(c.FRAME)):
                raise RuntimeError("CAN send failed (adapter not connected?)")
            self.log(f"[sequence] CAN sent {cmd.label()} {cmd.id_str()}: {cmd.data_str()}")
        elif op == "fsw":
            if not h.connected:
                raise RuntimeError("FSW not connected")
            if s["action"] == "single":
                h.run_btn.setChecked(False)
                tok = Token("FSW single measurement", timeout=max(30.0, h.timeout_spin.value() * 2))
                self._await_meas = (tok, time.time())
                h.on_single()
                return tok
            if s["action"] == "autolevel":
                app = h.app()
                return run_in(lambda job: h.worker.submit("seq", job),
                              lambda: h.worker.dev.auto_level(app), "FSW auto level")
            h.run_btn.setChecked(s["action"] == "run")
        elif op == "fsw_set":
            if not h.connected:
                raise RuntimeError("FSW not connected")
            app, params = s["app"], dict(s["params"])
            h.fsw_tabs.setCurrentIndex(0 if app == "vsa" else 1)     # GUI follows the FSW channel
            known = h.last_read if app == "vsa" else h.last_read_spec
            prev = {k: v for k, v in known.items() if k not in params}   # dependencies only
            box = Token(f"FSW {app.upper()} settings")

            def job():
                dev = h.worker.dev
                fn = dev.apply_settings if app == "vsa" else dev.apply_spec_settings
                cmds, errors, notes = fn(params, prev)
                if errors:
                    raise RuntimeError("; ".join(errors))
                box.msg = "FSW: " + ("; ".join(cmds) or "nothing to change") + \
                          ("  (" + "; ".join(notes) + ")" if notes else "")
            tok = run_in(lambda j: h.worker.submit("seq", j), job, box.what)
            tok.msg_from = box
            return tok
        elif op == "scan":
            b = getattr(h, "bench", None)
            if not b or not b._scan_ok:
                raise RuntimeError("multiplexer / 34405A not connected")
            b.run_btn.setChecked(s["action"] == "start")
        return None

    def on_measurement(self, r: dict):
        """Called by the main window for every drawn FSW result (completes 'single' steps)."""
        aw = getattr(self, "_await_meas", None)
        if aw and r.get("mode") == "sync" and r.get("t", 0) >= aw[1]:
            aw[0].done = True
            self._await_meas = None

    def shutdown(self):
        if self.tlog:
            self.tlog.close()
        if self.running:
            self.stop("stopped (application closed)", "warn")
