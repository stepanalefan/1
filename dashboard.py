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
C_OK, C_OFF, C_ERR, C_DIM = "#4fd18b", "#6e7681", "#ff6b6b", "#8a96a3"
STEP_COLORS = {"hmp": "#f0b03e", "smw": "#d2a8ff", "can": "#79c0ff", "wait": "#8a96a3",
               "fsw": "#58a6ff", "scan": "#4fd18b", "repeat": "#d2a8ff", "end": "#d2a8ff", "log": "#c9d1d9"}
SMW_KEYS = [  # key, label
    ("freq", "Frequency, Hz"), ("level", "Level, dBm"), ("format", "Modulation"), ("srate", "Symbol rate, Hz"),
    ("filter", "Filter"), ("fparam", "Filter alpha / BT"), ("dm", "Baseband DM on"),
    ("mod_all", "Modulation on"), ("rf", "RF output on"),
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

    def __init__(self, what):
        self.what, self.done, self.error = what, False, None


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
    def __init__(self, parent, step: dict, can_cmds: list):
        super().__init__(parent)
        self.step = dict(step)
        op = step["op"]
        self.setWindowTitle({"hmp": "HMP4040 outputs", "smw": "SMW200A signal", "can": "CAN command",
                             "wait": "Pause", "fsw": "FSW", "scan": "Voltage scan", "repeat": "Repeat",
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
        elif op in ("fsw", "scan"):
            self.action = QtWidgets.QComboBox()
            acts = (["run", "stop", "single"] if op == "fsw" else ["start", "stop"])
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
                                        ("+ Pause", "wait"), ("+ FSW", "fsw"), ("+ Scan", "scan"),
                                        ("+ Repeat", "repeat"), ("+ Log", "log"))):
            b = QtWidgets.QPushButton(text)
            b.setStyleSheet(f"color:{STEP_COLORS[op]};")
            b.clicked.connect(lambda _, op=op: self.add_step(op))
            add.addWidget(b, k // 4, k % 4)
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

    # ------------------------------------------------------------- helpers
    def log(self, text, level="info"):
        self.host.logmsg(text, level)

    def save(self):
        self.data["watch"] = [[u, t, f.byte, f.name] for u, t, f in self.watch_rows]
        self.data["volt_channels"] = self.volt_ch.text()
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
            return f"〰 FSW   {s['action']}"
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
                    "fsw": {"action": "run"}, "scan": {"action": "start"}, "repeat": {"count": 3},
                    "log": {"text": ""}, "can": {}}
        dlg = StepDialog(self, {"op": op, **defaults[op]}, self._can_cmds())
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
        dlg = StepDialog(self, self.steps[rows[0]], self._can_cmds())
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
                self.runner.start(20)
                return
            tok, self.pending = self.pending, None
            if tok.error:
                return self._fail(f"{tok.what}: {tok.error}")
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
                h.on_single()
            else:
                h.run_btn.setChecked(s["action"] == "run")
        elif op == "scan":
            b = getattr(h, "bench", None)
            if not b or not b._scan_ok:
                raise RuntimeError("multiplexer / 34405A not connected")
            b.run_btn.setChecked(s["action"] == "start")
        return None

    def shutdown(self):
        if self.running:
            self.stop("stopped (application closed)", "warn")
