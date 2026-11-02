# -*- coding: utf-8 -*-
"""
n6700_supply.py
===============
Keysight N6701C modular power system (here with two N6775A modules: 60 V / 5 A / 300 W each).

Used as the "N6701C" tab of fsw_vsa_monitor.py, or standalone:  python n6700_supply.py [--demo]

* Per output: voltage, current limit, output on/off, over-voltage protection, over-current
  protection (output switches off when the current limit is reached), measured V / I / P,
  mode CV / CC / OFF / PROT.
* All outputs ON / OFF, clear protection, plot of current / voltage / power, daily log
  n6700_log_YYYY-MM-DD.csv.
* Connection: LAN (IP -> raw SCPI socket 5025, or a VISA string TCPIP0::...::inst0::INSTR /
  hislip0), USB / GPIB (VISA string) - needs Keysight IO Libraries for USB.

SCPI (Keysight N6700 family; channel list "(@1,2)"):
    SOUR:VOLT v,(@ch)  SOUR:CURR i,(@ch)  SOUR:VOLT:PROT v,(@ch)  CURR:PROT:STAT ON|OFF,(@ch)
    OUTP:STAT ON|OFF,(@list)  OUTP:PROT:CLE (@list)  MEAS:VOLT? (@list)  MEAS:CURR? (@list)
    STAT:OPER:COND? (@list)  bit 0 CV, 1 CC, 2 OFF
    STAT:QUES:COND? (@list)  bit 0 OV, 1 OC, 2 PF, 3 CP+, 4 OT, 5 CP-, ... 9 INH, 11 PROT
    SYST:CHAN:COUN?  SYST:CHAN:MOD? (@list)  SOUR:VOLT? MAX,(@ch)  SOUR:CURR? MAX,(@ch)
Every setting change is followed by SYST:ERR?; errors are reported next to the channel.
"""

from __future__ import annotations

import csv
import os
import sys
import time

import numpy as np
from PyQt5 import QtCore, QtWidgets
import pyqtgraph as pg

import bench_monitor as bm

N67_MAX_CH = 4
MODULES = {  # model: (V max, A max, W)  - fallback when the instrument does not answer MAX queries
    "N6775A": (60.0, 5.0, 300.0), "N6773A": (20.0, 15.0, 300.0), "N6774A": (35.0, 8.5, 300.0),
    "N6776A": (100.0, 3.0, 300.0), "N6754A": (60.0, 20.0, 300.0),
}
OPER_CV, OPER_CC, OPER_OFF = 1, 2, 4
QUES_OV, QUES_OC, QUES_PF, QUES_CPP, QUES_OT, QUES_INH, QUES_PROT = 1, 2, 4, 8, 16, 512, 2048
QUES_TRIP = QUES_OV | QUES_OC | QUES_PF | QUES_OT | QUES_PROT     # protection that switched the output off
COLORS = ["#f0b03e", "#79c0ff", "#4fd18b", "#d2a8ff"]


def ques_text(q: int) -> str:
    names = [(QUES_OV, "OV"), (QUES_OC, "OC"), (QUES_PF, "PF"), (QUES_OT, "OT"), (QUES_INH, "INH"),
             (QUES_PROT, "PROT"), (QUES_CPP, "CP")]
    return " ".join(n for b, n in names if q & b)


def mode_text(on: bool, oper: int, ques: int) -> str:
    if ques & QUES_TRIP:
        return "PROT " + ques_text(ques & QUES_TRIP)
    if not on or oper & OPER_OFF:
        return "OFF"
    if oper & OPER_CC:
        return "CC"
    if oper & OPER_CV:
        return "CV"
    return "ON"


# --------------------------------------------------------------------------- #
#  Drivers                                                                     #
# --------------------------------------------------------------------------- #
class N6700:
    def __init__(self, addr: str):
        a = addr.strip()
        if "::" not in a and not a.upper().startswith("COM") and ":" not in a:
            a = f"{a}:5025"                                   # plain IP -> raw SCPI socket
        self.link = bm.open_link(a)
        try:
            self.idn = self.link.query("*IDN?")
            if "N67" not in self.idn.upper():
                raise IOError(f"{self.link.name} is not a Keysight N67xx (*IDN? = {self.idn!r})")
            self.link.write("*CLS")
            n = int(float(self.link.query("SYST:CHAN:COUN?")))
            self.channels = list(range(1, min(n, N67_MAX_CH) + 1))
            mods = [m.strip().strip('"') for m in self.link.query(f"SYST:CHAN:MOD? {self._lst()}").split(",")]
            self.models = {ch: (mods[k] if k < len(mods) else "?") for k, ch in enumerate(self.channels)}
            self.limits = {}
            for ch in self.channels:
                fb = MODULES.get(self.models[ch], (60.0, 5.0, 300.0))
                try:
                    vmax = float(self.link.query(f"SOUR:VOLT? MAX,(@{ch})"))
                    imax = float(self.link.query(f"SOUR:CURR? MAX,(@{ch})"))
                    self.limits[ch] = (vmax, imax, fb[2])
                except Exception:
                    self.limits[ch] = fb
            self.errors()
        except Exception:
            self.link.close()
            raise
        model = self.idn.split(",")[1].strip() if "," in self.idn else self.idn
        self.info = f"{model} · " + ", ".join(f"{ch}: {self.models[ch]}" for ch in self.channels) + \
                    f" · {self.link.name}"

    def _lst(self, chans=None) -> str:
        return "(@" + ",".join(str(c) for c in (chans or self.channels)) + ")"

    def errors(self) -> list[str]:
        out = []
        for _ in range(20):
            e = self.link.query("SYST:ERR?")
            if e.lstrip("+").startswith("0"):
                break
            out.append(e)
        return out

    def _floats(self, cmd) -> list[float]:
        return [float(x) for x in self.link.query(cmd).split(",")]

    def read_channel(self, ch):
        """(Vset, Iset, on, OVP, OCP on)"""
        v = float(self.link.query(f"SOUR:VOLT? (@{ch})"))
        i = float(self.link.query(f"SOUR:CURR? (@{ch})"))
        on = int(float(self.link.query(f"OUTP:STAT? (@{ch})"))) == 1
        ovp = float(self.link.query(f"SOUR:VOLT:PROT? (@{ch})"))
        ocp = int(float(self.link.query(f"SOUR:CURR:PROT:STAT? (@{ch})"))) == 1
        return v, i, on, ovp, ocp

    def set_channel(self, ch, v, i, on, ovp=None, ocp=None) -> list[str]:
        errs = []
        cmds = [f"SOUR:VOLT {v:.6g},(@{ch})", f"SOUR:CURR {i:.6g},(@{ch})"]
        if ovp is not None:
            cmds.append(f"SOUR:VOLT:PROT {ovp:.6g},(@{ch})")
        if ocp is not None:
            cmds.append(f"SOUR:CURR:PROT:STAT {'ON' if ocp else 'OFF'},(@{ch})")
        cmds.append(f"OUTP:STAT {'ON' if on else 'OFF'},(@{ch})")
        for c in cmds:
            self.link.write(c)
            errs += [f"{c}  →  {e}" for e in self.errors()]
        return errs

    def set_all(self, on: bool, chans=None) -> list[str]:
        c = f"OUTP:STAT {'ON' if on else 'OFF'},{self._lst(chans)}"
        self.link.write(c)
        return [f"{c}  →  {e}" for e in self.errors()]

    def clear_protection(self) -> list[str]:
        c = f"OUTP:PROT:CLE {self._lst()}"
        self.link.write(c)
        return [f"{c}  →  {e}" for e in self.errors()]

    def measure(self):
        """({ch: (V, I)}, {ch: (oper, ques)})"""
        lst = self._lst()
        v = self._floats(f"MEAS:VOLT? {lst}")
        i = self._floats(f"MEAS:CURR? {lst}")
        op = [int(x) for x in self._floats(f"STAT:OPER:COND? {lst}")]
        qu = [int(x) for x in self._floats(f"STAT:QUES:COND? {lst}")]
        meas = {ch: (v[k], i[k]) for k, ch in enumerate(self.channels)}
        st = {ch: (op[k], qu[k]) for k, ch in enumerate(self.channels)}
        return meas, st

    def close(self):
        self.link.close()


class DemoN6700:
    """Two N6775A with resistive loads (10 Ω on 1, 25 Ω on 2): CV / CC, OCP trip, OVP."""
    channels = [1, 2]

    def __init__(self):
        self.models = {1: "N6775A", 2: "N6775A"}
        self.limits = {1: MODULES["N6775A"], 2: MODULES["N6775A"]}
        self.idn = "Keysight Technologies,N6701C,DEMO,simulated"
        self.info = "N6701C · 1: N6775A, 2: N6775A · demo"
        self.load = {1: 10.0, 2: 25.0}
        self.st = {ch: {"v": 0.0, "i": 1.0, "on": False, "ovp": 66.0, "ocp": False, "trip": 0} for ch in (1, 2)}
        self.rng = np.random.default_rng()

    def errors(self):
        return []

    def read_channel(self, ch):
        s = self.st[ch]
        return s["v"], s["i"], s["on"], s["ovp"], s["ocp"]

    def set_channel(self, ch, v, i, on, ovp=None, ocp=None):
        s = self.st[ch]
        s.update(v=v, i=i, on=on)
        if ovp is not None:
            s["ovp"] = ovp
        if ocp is not None:
            s["ocp"] = ocp
        if on:
            s["trip"] = 0 if not s["trip"] else s["trip"]
        return []

    def set_all(self, on, chans=None):
        for ch in chans or self.channels:
            if not (on and self.st[ch]["trip"]):
                self.st[ch]["on"] = on
        return []

    def clear_protection(self):
        for s in self.st.values():
            s["trip"] = 0
        return []

    def measure(self):
        meas, st = {}, {}
        for ch, s in self.st.items():
            if s["v"] > s["ovp"]:
                s["trip"], s["on"] = QUES_OV, False
            if not s["on"] or s["trip"]:
                meas[ch] = (0.0, 0.0)
                st[ch] = (OPER_OFF, s["trip"])
                continue
            i_cv = s["v"] / self.load[ch]
            if i_cv > s["i"]:                                # current limit -> CC
                if s["ocp"]:
                    s["trip"], s["on"] = QUES_OC, False
                    meas[ch], st[ch] = (0.0, 0.0), (OPER_OFF, QUES_OC)
                    continue
                i, v, op = s["i"], s["i"] * self.load[ch], OPER_CC
            else:
                i, v, op = i_cv, s["v"], OPER_CV
            meas[ch] = (v * (1 + self.rng.normal(0, 3e-4)), i * (1 + self.rng.normal(0, 1e-3)))
            st[ch] = (op, 0)
        return meas, st

    def close(self):
        pass


# --------------------------------------------------------------------------- #
#  Worker                                                                      #
# --------------------------------------------------------------------------- #
class N67Worker(bm._Worker):
    telemetry = QtCore.pyqtSignal(object, object)        # {ch: (V, I)}, {ch: (oper, ques)}
    state = QtCore.pyqtSignal(object)                    # {ch: (Vset, Iset, on, ovp, ocp)}
    connected = QtCore.pyqtSignal(bool, str)
    cleared = QtCore.pyqtSignal()                        # protection cleared: old status is stale

    TEL_INTERVAL = 0.5

    def __init__(self):
        super().__init__()
        self._next_tel = 0.0

    def connect_dev(self, demo: bool, addr: str):
        self.disconnect_dev()
        self.dev = DemoN6700() if demo else N6700(addr)
        self.connected.emit(True, self.dev.info)
        self.read_state()

    def disconnect_dev(self):
        if self.dev is not None:
            self.dev.close()
            self.dev = None
            self.connected.emit(False, "")

    def read_state(self):
        if self.dev:
            self.state.emit({ch: self.dev.read_channel(ch) for ch in self.dev.channels})

    def set_channel(self, ch, v, i, on, ovp=None, ocp=None):
        errs = self.dev.set_channel(ch, v, i, on, ovp, ocp)
        for e in errs:
            self.log.emit(f"N6701C OUT{ch}: {e}", "err")
        self.log.emit(f"N6701C OUT{ch}: {v:.3f} V, {i:.3f} A, output {'ON' if on else 'OFF'}"
                      + (f", OVP {ovp:g} V" if ovp is not None else "")
                      + (f", OCP {'on' if ocp else 'off'}" if ocp is not None else ""), "warn" if errs else "ok")
        self.state.emit({ch: self.dev.read_channel(ch)})
        self._next_tel = 0.0
        if errs:
            raise RuntimeError("; ".join(errs))

    def set_all(self, on, reason=""):
        errs = self.dev.set_all(on)
        for e in errs:
            self.log.emit(f"N6701C: {e}", "err")
        self.log.emit(f"N6701C all outputs {'ON' if on else 'OFF'}" + (f" ({reason})" if reason else ""),
                      "warn" if errs else "ok")
        self.read_state()
        self._next_tel = 0.0

    def clear_protection(self):
        errs = self.dev.clear_protection()
        self.cleared.emit()
        self.log.emit("N6701C protection cleared" + (f": {'; '.join(errs)}" if errs else ""), "warn" if errs else "ok")
        self._next_tel = 0.0

    def step(self) -> bool:
        if self.dev is None:
            return False
        now = time.monotonic()
        if now < self._next_tel:
            return False
        meas, st = self.dev.measure()
        self.telemetry.emit(meas, st)
        self._next_tel = now + self.TEL_INTERVAL
        return True

    def on_error(self, e):
        self.log.emit(f"N6701C communication error: {type(e).__name__}: {e}", "err")
        try:
            self.dev.close()
        except Exception:
            pass
        self.dev = None
        self.connected.emit(False, "error")


class N67Csv:
    """n6700_log_YYYY-MM-DD.csv: one row per interval with all channels."""

    def __init__(self, folder: str, channels):
        self.folder, self.channels, self.f, self.day = folder, list(channels), None, None
        self.header = ["Time"] + [f"CH{c}_{k}" for c in self.channels
                                  for k in ("On", "Vset_V", "Iset_A", "V_V", "I_A", "P_W", "Mode")]

    def write(self, t, state, meas, status):
        day = time.strftime("%Y-%m-%d", time.localtime(t))
        if day != self.day:
            self.close()
            path = os.path.join(self.folder, f"n6700_log_{day}.csv")
            new = not os.path.isfile(path)
            self.f = open(path, "a", newline="", encoding="utf-8")
            self.w = csv.writer(self.f)
            if new:
                self.w.writerow(self.header)
            self.day = day
        row = [time.strftime("%H:%M:%S", time.localtime(t))]
        for c in self.channels:
            vs, is_, on = state.get(c, (float("nan"), float("nan"), False, 0, False))[:3]
            v, i = meas.get(c, (float("nan"), float("nan")))
            op, q = status.get(c, (0, 0))
            row += [int(on), f"{vs:.4f}", f"{is_:.4f}", f"{v:.4f}", f"{i:.4f}", f"{v * i:.3f}", mode_text(on, op, q)]
        self.w.writerow(row)
        self.f.flush()

    def close(self):
        if self.f:
            self.f.close()
            self.f = None
            self.day = None


# --------------------------------------------------------------------------- #
#  GUI                                                                         #
# --------------------------------------------------------------------------- #
class ChannelBox(QtWidgets.QGroupBox):
    apply = QtCore.pyqtSignal(int, float, float, bool, float, bool)

    def __init__(self, ch: int):
        super().__init__(f"OUT {ch}")
        self.ch = ch
        f = QtWidgets.QFormLayout(self)
        self.model = QtWidgets.QLabel("—")
        self.model.setStyleSheet("color:#8a96a3;")
        self.on = QtWidgets.QCheckBox("Output on")
        self.v = bm._spin(0, 60.0, 3, " V", 5.0, 0.1)
        self.v.setPrefix("U  ")
        self.i = bm._spin(0, 5.0, 3, " A", 0.5, 0.05)
        self.i.setPrefix("I ≤ ")
        self.ovp = bm._spin(0, 66.0, 2, " V", 66.0, 1.0)
        self.ovp.setPrefix("OVP ")
        self.ovp.setToolTip("Over-voltage protection: output off above this voltage")
        self.ocp = QtWidgets.QCheckBox("OCP: off when current limit is reached")
        self.ocp.setToolTip("Over-current protection: the output switches off instead of regulating at the limit (CC)")
        self.meas = QtWidgets.QLabel("—")
        self.meas.setStyleSheet("font-size:15px;font-weight:700;color:#4fd18b;")
        self.mode = QtWidgets.QLabel("")
        self.mode.setStyleSheet("font-weight:700;")
        self.set_btn = QtWidgets.QPushButton("Set")
        self.set_btn.setToolTip("Send voltage, current, OVP, OCP and output state of this channel")
        self.set_btn.clicked.connect(self._emit)
        for w in (self.model, self.on, self.v, self.i, self.ovp, self.ocp, self.set_btn, self.meas, self.mode):
            f.addRow(w)

    def _emit(self):
        self.apply.emit(self.ch, round(self.v.value(), 4), round(self.i.value(), 4), self.on.isChecked(),
                        round(self.ovp.value(), 3), self.ocp.isChecked())

    def set_limits(self, model, vmax, imax):
        self.model.setText(f"{model}  ({vmax:g} V / {imax:g} A)")
        self.v.setMaximum(vmax)
        self.i.setMaximum(imax)
        self.ovp.setMaximum(vmax * 1.1)

    def set_state(self, v, i, on, ovp, ocp):
        for w, val in ((self.v, v), (self.i, i), (self.ovp, ovp)):
            w.blockSignals(True)
            w.setValue(val)
            w.blockSignals(False)
        self.on.setChecked(on)
        self.ocp.setChecked(ocp)

    def set_meas(self, v, i, mode):
        self.meas.setText(f"{v:.3f} V   {i:.3f} A\n{v * i:.2f} W")
        color = "#ff6b6b" if mode.startswith("PROT") or mode == "CC" else ("#4fd18b" if mode == "CV" else "#8a96a3")
        self.mode.setText(mode)
        self.mode.setStyleSheet(f"font-weight:700;color:{color};")


class N6700Tab(QtCore.QObject):
    """N6701C tab: `settings_panel` (left) and `page`. Attributes read by the Main tab:
    ok, channels, last_state, last_meas, last_status, hist, comm_error_t, status_lbl, worker."""
    log = QtCore.pyqtSignal(str, str)

    def __init__(self, parent=None, folder_fn=None):
        super().__init__(parent)
        self.folder_fn = folder_fn or bm.script_dir
        self.ok = False
        self.channels: list[int] = []
        self.last_state, self.last_meas, self.last_status = {}, {}, {}
        self.hist = {}
        self.comm_error_t = 0.0
        self.csv: N67Csv | None = None
        self._next_log = 0.0
        self.worker = N67Worker()
        self.worker.log.connect(self.log)
        self.worker.connected.connect(self._on_connected)
        self.worker.state.connect(self._on_state)
        self.worker.telemetry.connect(self._on_tel)
        self.worker.cleared.connect(lambda: self.last_status.clear())
        self.settings_panel = self._build_settings()
        self.page = self._build_page()
        self._set_connected(False)
        self.worker.start()

    # ------------------------------------------------------------------ build
    def _build_settings(self):
        w = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(w)
        lv.setContentsMargins(4, 4, 4, 4)
        self.demo_chk = QtWidgets.QCheckBox("Simulated hardware (demo)")
        lv.addWidget(self.demo_chk)
        g = QtWidgets.QGroupBox("N6701C (LAN / USB / GPIB)")
        f = QtWidgets.QFormLayout(g)
        self.addr = QtWidgets.QLineEdit()
        self.addr.setPlaceholderText("192.168.1.30  or  USB0::0x2A8D::...::INSTR")
        self.addr.setToolTip("LAN: IP address (raw SCPI socket 5025) or TCPIP0::<ip>::inst0::INSTR / hislip0\n"
                             "USB / GPIB: VISA address (Keysight Connection Expert)")
        self.conn_btn = QtWidgets.QPushButton("Connect")
        self.conn_btn.clicked.connect(self.on_connect)
        self.status_lbl = QtWidgets.QLabel("not connected")
        self.status_lbl.setWordWrap(True)
        f.addRow("Address", self.addr)
        f.addRow(self.conn_btn)
        f.addRow(self.status_lbl)
        lv.addWidget(g)
        g = QtWidgets.QGroupBox("Logging")
        f = QtWidgets.QFormLayout(g)
        self.log_chk = QtWidgets.QCheckBox("Write daily N6701C log")
        self.log_chk.setToolTip("n6700_log_YYYY-MM-DD.csv in the log folder of the HMP4040 tab")
        self.log_chk.setChecked(True)
        self.log_chk.toggled.connect(lambda *_: self._reopen_log())
        self.log_int = bm._spin(0.5, 3600, 1, " s", 1.0)
        f.addRow(self.log_chk)
        f.addRow("Log every", self.log_int)
        lv.addWidget(g)
        lv.addStretch(1)
        return w

    def _build_page(self):
        page = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(page)
        root.setContentsMargins(4, 4, 4, 4)
        self.g_ctrl = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(self.g_ctrl)
        row.setContentsMargins(0, 0, 0, 0)
        self.boxes = {}
        for ch in range(1, N67_MAX_CH + 1):
            b = ChannelBox(ch)
            b.apply.connect(self.on_set)
            b.hide()
            self.boxes[ch] = b
            row.addWidget(b)
        side = QtWidgets.QVBoxLayout()
        on = QtWidgets.QPushButton("ALL OUTPUTS ON")
        off = QtWidgets.QPushButton("ALL OUTPUTS OFF")
        clr = QtWidgets.QPushButton("Clear protection")
        clr.setToolTip("Clears OV / OC / OT protection so the outputs can be switched on again")
        on.clicked.connect(lambda: self.worker.submit(lambda: self.worker.set_all(True)))
        off.clicked.connect(lambda: self.worker.submit(lambda: self.worker.set_all(False)))
        clr.clicked.connect(lambda: self.worker.submit(self.worker.clear_protection))
        for b in (on, off):
            b.setMinimumHeight(36)
            b.setStyleSheet("font-weight:700;")
        for b in (on, off, clr):
            side.addWidget(b)
        side.addStretch(1)
        row.addLayout(side)
        root.addWidget(self.g_ctrl)
        bar = QtWidgets.QHBoxLayout()
        self.plot_what = QtWidgets.QComboBox()
        self.plot_what.addItems(["Current, A", "Voltage, V", "Power, W"])
        self.follow = QtWidgets.QCheckBox("Follow")
        self.follow.setChecked(True)
        self.window_s = QtWidgets.QSpinBox()
        self.window_s.setRange(10, 86400)
        self.window_s.setValue(300)
        self.window_s.setPrefix("Window ")
        self.window_s.setSuffix(" s")
        clear = QtWidgets.QPushButton("Clear plot")
        clear.clicked.connect(self.on_clear)
        for w in (QtWidgets.QLabel("Plot"), self.plot_what, self.follow, self.window_s, clear):
            bar.addWidget(w)
        bar.addStretch(1)
        root.addLayout(bar)
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.getPlotItem().setMenuEnabled(False)
        self.plot.addLegend(offset=(5, 5))
        self.plot.getViewBox().sigRangeChangedManually.connect(lambda *_: self.follow.setChecked(False))
        self.curves = {}
        root.addWidget(self.plot, 1)
        self.timer = QtCore.QTimer(self, interval=500, timeout=self._redraw)
        self.timer.start()
        return page

    # ---------------------------------------------------------------- actions
    def _set_connected(self, on):
        self.ok = on
        self.conn_btn.setText("Disconnect" if on else "Connect")
        self.addr.setEnabled(not on)
        self.g_ctrl.setEnabled(on)
        if not on:
            for b in self.boxes.values():
                b.meas.setText("—")
                b.mode.setText("")

    def on_connect(self):
        if self.ok:
            self.worker.submit(self.worker.disconnect_dev)
            return
        demo, addr = self.demo_chk.isChecked(), self.addr.text().strip()
        if not demo and not addr:
            self.log.emit("Enter the N6701C address (IP or VISA string)", "warn")
            return
        self.status_lbl.setText("connecting…")
        self.conn_btn.setEnabled(False)

        def job():
            try:
                self.worker.connect_dev(demo, addr)
            except Exception as e:
                self.worker.connected.emit(False, f"{type(e).__name__}: {e}")
        self.worker.submit(job)

    def on_set(self, ch, v, i, on, ovp, ocp):
        def job():
            try:
                self.worker.set_channel(ch, v, i, on, ovp, ocp)
            except RuntimeError:
                pass                                         # already logged
        self.worker.submit(job)

    def on_clear(self):
        for lists in self.hist.values():
            for x in lists:
                x.clear()
        self.follow.setChecked(True)

    def _reopen_log(self):
        if self.csv:
            self.csv.close()
        self.csv = N67Csv(self.folder_fn(), self.channels) if self.log_chk.isChecked() and self.channels else None
        self._next_log = 0.0

    # --------------------------------------------------------------- callbacks
    def _on_connected(self, ok, info):
        self.conn_btn.setEnabled(True)
        self._set_connected(ok)
        if ok:
            dev = self.worker.dev
            self.channels = list(dev.channels)
            for ch, b in self.boxes.items():
                b.setVisible(ch in self.channels)
                if ch in self.channels:
                    vmax, imax, _ = dev.limits[ch]
                    b.set_limits(dev.models[ch], vmax, imax)
            for ch in self.channels:
                self.hist.setdefault(ch, ([], [], []))
                if ch not in self.curves:
                    self.curves[ch] = self.plot.plot([], [], pen=pg.mkPen(COLORS[ch - 1], width=1.5), name=f"OUT {ch}")
            self.status_lbl.setText(f"✓ {info}")
            self.log.emit(f"N6701C connected: {info}", "ok")
            self._reopen_log()
        else:
            if info == "error":
                self.comm_error_t = time.time()
            self.status_lbl.setText(info or "not connected")
            if info and info != "error":
                self.log.emit(f"N6701C: {info}", "err")
            self.last_meas, self.last_status = {}, {}

    def _on_state(self, st):
        self.last_state.update(st)
        for ch, vals in st.items():
            self.boxes[ch].set_state(*vals)

    def _on_tel(self, meas, status):
        self.last_meas.update(meas)
        self.last_status.update(status)
        now = time.time()
        for ch, (v, i) in meas.items():
            op, q = status.get(ch, (0, 0))
            on = self.last_state.get(ch, (0, 0, False))[2]
            if q & QUES_TRIP or op & OPER_OFF:
                on = on and not (op & OPER_OFF)
            self.boxes[ch].set_meas(v, i, mode_text(on, op, q))
            ts, vs, is_ = self.hist.setdefault(ch, ([], [], []))
            ts.append(now)
            vs.append(v)
            is_.append(i)
            if len(ts) > 200_000:
                for x in (ts, vs, is_):
                    del x[:50_000]
        if self.csv and now >= self._next_log:
            self._next_log = now + self.log_int.value()
            try:
                self.csv.write(now, self.last_state, self.last_meas, self.last_status)
            except OSError as e:
                self.log.emit(f"N6701C log stopped: {e}", "err")
                self.log_chk.setChecked(False)

    def is_cc(self, ch) -> bool:
        op, q = self.last_status.get(ch, (0, 0))
        return bool(op & OPER_CC) and not op & OPER_OFF

    def tripped(self, ch) -> int:
        return self.last_status.get(ch, (0, 0))[1] & QUES_TRIP

    def _redraw(self):
        k = self.plot_what.currentIndex()
        self.plot.setLabel("left", ("Current", "Voltage", "Power")[k], units=("A", "V", "W")[k])
        for ch, (ts, vs, is_) in self.hist.items():
            n = min(len(ts), len(vs), len(is_))
            v, i = np.asarray(vs[:n], float), np.asarray(is_[:n], float)
            y = i if k == 0 else v if k == 1 else v * i
            if ch in self.curves:
                self.curves[ch].setData(np.asarray(ts[:n], float), y)
        if self.follow.isChecked():
            now = time.time()
            self.plot.setXRange(now - self.window_s.value(), now, padding=0)

    def shutdown(self):
        self.timer.stop()
        self.worker.shutdown()
        if self.csv:
            self.csv.close()


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Keysight N6701C control")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    pg.setConfigOptions(background="#0f1216", foreground="#c9d1d9")
    win = QtWidgets.QMainWindow()
    tab = N6700Tab(win)
    tab.log.connect(lambda t, l: print(f"[{l}] {t}"))
    split = QtWidgets.QSplitter()
    split.addWidget(tab.settings_panel)
    split.addWidget(tab.page)
    win.setCentralWidget(split)
    win.setStyleSheet(bm.STYLE)
    win.resize(1400, 800)
    if args.demo:
        tab.demo_chk.setChecked(True)
    win.show()
    app.aboutToQuit.connect(tab.shutdown)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
