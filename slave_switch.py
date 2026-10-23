"""
Switch outputs on the slave Arduino (pins A0, A1) + BOS watchdog.

The slave is reached through the master Arduino on the Bench tab (see the sketches):
  SW:SET:<i>:<0|1>, SW:PULSE:<i>:<ms>, SW:STATE?  ->  "SW:STATE:<one 0/1 per output>"
Pulses are timed by the slave itself, so a pulse ends even if the PC stops talking.

BOS watchdog: if no data from BOS arrives for longer than the set time, an output is switched
(pulse or stay on). "Data from BOS" means:
  CAN - any received extended frame whose ID prefix is BOS (0x1A46....)
  FSW - a VSA (K70) result with MER at or above a threshold (the demodulator locked on a signal)
The silence is counted from the last BOS data or from the moment the watchdog was armed /
last triggered, whichever is later, so it fires at most once per period.

Files:
* switch.json                       names, watchdog settings (next to the program)
* switch_log_YYYY-MM-DD.csv         every switching action with its reason (bench log folder)
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
import time

from PyQt5 import QtCore, QtGui, QtWidgets

PINS = ("A0", "A1")                 # must match SW_PINS in the slave sketch
BOS_PREFIX = 0x1A46                 # 29-bit ID = prefix(16) | segment(8) | type(8)
C_OK, C_OFF, C_ERR, C_DIM, C_WARN = "#4fd18b", "#6e7681", "#ff6b6b", "#8a96a3", "#f0b03e"
SOURCES = [("either", "CAN or FSW (either one silent)"),
           ("both", "CAN and FSW (both silent)"),
           ("can", "CAN only"),
           ("fsw", "FSW only")]
DEFAULTS = {
    "names": [f"Switch {p}" for p in PINS],
    "exclusive": False,
    "manual_pulse_ms": 500,
    "wd_enabled": False,
    "wd_source": "either",
    "wd_minutes": 60.0,
    "wd_mer_min": 10.0,
    "wd_output": 0,
    "wd_action": "pulse",           # "pulse" or "on"
    "wd_pulse_ms": 1000,
    "wd_max": 0,                    # 0 = unlimited
}


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CFG_FILE = os.path.join(_app_dir(), "switch.json")


def fmt_age(sec) -> str:
    if sec is None:
        return "never"
    sec = int(sec)
    if sec < 60:
        return f"{sec} s ago"
    if sec < 3600:
        return f"{sec // 60} min ago"
    return f"{sec // 3600} h {sec % 3600 // 60:02d} min ago"


def fmt_left(sec) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


class SwitchCsv:
    HEADER = ["Time", "Output", "Pin", "Name", "Action", "State", "Reason"]

    def __init__(self, folder_fn):
        self.folder_fn = folder_fn

    def write(self, out: int, action: str, state: int, reason: str, names):
        ts = time.time()
        path = os.path.join(self.folder_fn(), f"switch_log_{time.strftime('%Y-%m-%d', time.localtime(ts))}.csv")
        new = not os.path.isfile(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(self.HEADER)
            w.writerow([time.strftime("%H:%M:%S", time.localtime(ts)), out, PINS[out], names[out],
                        action, state, reason])


class SwitchController(QtCore.QObject):
    """Owns the output state, the settings and the watchdog. `panel` goes on the Main tab."""
    _reply = QtCore.pyqtSignal(object, str, object)     # states or None, error text, callback
    changed = QtCore.pyqtSignal()

    def __init__(self, bench, log_fn, parent=None):
        super().__init__(parent)
        self.bench = bench
        self.log = log_fn
        self.cfg = dict(DEFAULTS)
        self._load()
        self.state: list | None = None         # None = unknown (slave not read yet)
        self.slave_ok: bool | None = None
        self.error = ""
        self.last_can = None                   # wall time of the last BOS data
        self.last_fsw = None
        self.armed_at = time.time()
        self.triggers = 0
        self.wd_busy = False
        self.retry_at = 0.0
        self._next_refresh = 0.0
        self.csv = SwitchCsv(lambda: bench.log_folder)
        self._reply.connect(self._on_reply)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)
        self.panel = SwitchPanel(self)

    # ------------------------------------------------------------- settings
    def _load(self):
        try:
            with open(CFG_FILE, encoding="utf-8") as f:
                data = json.load(f)
            for k in DEFAULTS:
                if k in data:
                    self.cfg[k] = data[k]
        except (OSError, ValueError):
            pass
        names = list(self.cfg["names"])[:len(PINS)]
        self.cfg["names"] = names + DEFAULTS["names"][len(names):]

    def save(self):
        try:
            tmp = CFG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=1, ensure_ascii=False)
            os.replace(tmp, CFG_FILE)
        except OSError as e:
            self.log(f"Cannot save {CFG_FILE}: {e}", "err")

    def set_watchdog(self, on: bool):
        self.cfg["wd_enabled"] = on
        self.armed_at = time.time()
        self.triggers = 0
        self.retry_at = 0.0
        self.save()
        self.log(f"BOS watchdog {'armed' if on else 'off'}"
                 + (f": {self.wd_rule_text()}" if on else ""), "ok" if on else "info")
        self.changed.emit()

    def wd_rule_text(self) -> str:
        c = self.cfg
        src = dict(SOURCES)[c["wd_source"]]
        act = f"pulse {c['wd_pulse_ms']} ms" if c["wd_action"] == "pulse" else "switch ON"
        return (f"no BOS data ({src}) for {c['wd_minutes']:g} min -> {act} on "
                f"{PINS[c['wd_output']]} '{c['names'][c['wd_output']]}'")

    # ------------------------------------------------------------ data hooks
    def on_frames(self, frames):
        """Listener for the CAN tab (canable_analyzer.SnifferWindow.frame_listeners)."""
        for f in frames:
            if f.ext and not f.tx and (f.can_id >> 16) == BOS_PREFIX:
                self.last_can = time.time()
                return

    def on_vsa(self, r: dict):
        """Called with every FSW VSA result."""
        mer = r.get("mer")
        if mer is not None and math.isfinite(mer) and mer >= self.cfg["wd_mer_min"]:
            self.last_fsw = time.time()

    # -------------------------------------------------------------- commands
    def _dev(self):
        scan = self.bench.scan
        return scan.dev if scan.dev is not None and hasattr(scan.dev, "slave") else None

    def command(self, cmds: list[str], done=None):
        """Send slave commands in order (one worker job). done(ok: bool) runs in the GUI thread."""
        if self._dev() is None:
            self.error = "Arduino not connected (Bench tab)"
            self.changed.emit()
            if done:
                done(False)
            return
        scan = self.bench.scan

        def job():
            try:
                lines = []
                for c in cmds:
                    lines = scan.dev.slave(c)
                st = lines[-1].split(":")[2] if lines and lines[-1].startswith("SW:STATE:") else ""
                self._reply.emit([ch == "1" for ch in st], "", done)
            except Exception as e:
                self._reply.emit(None, f"{type(e).__name__}: {e}", done)
        scan.submit(job)

    def _on_reply(self, states, err, done):
        if states is not None:
            self.state, self.slave_ok, self.error = states, True, ""
        else:
            self.slave_ok, self.error = False, err
            self._next_refresh = time.monotonic() + 60       # don't block the master every few seconds
        self.changed.emit()
        if done:
            done(states is not None)

    def _prefix_exclusive(self, i: int) -> list[str]:
        if not self.cfg["exclusive"]:
            return []
        self._excl_off = [j for j in range(len(PINS)) if j != i and self.state and self.state[j]]
        return [f"SW:SET:{j}:0" for j in range(len(PINS)) if j != i]

    def set_output(self, i: int, on: bool, reason="manual", done=None):
        def fin(ok):
            self._logged(ok, i, "on" if on else "off", int(on), reason)
            if done:
                done(ok)
        self.command((self._prefix_exclusive(i) if on else []) + [f"SW:SET:{i}:{int(on)}"], fin)

    def pulse(self, i: int, ms: int, reason="manual", done=None):
        def fin(ok):
            self._logged(ok, i, f"pulse {ms} ms", 1, reason)
            if ok:
                QtCore.QTimer.singleShot(ms + 300, self.refresh)    # show the end of the pulse
            if done:
                done(ok)
        self.command(self._prefix_exclusive(i) + [f"SW:PULSE:{i}:{ms}"], fin)

    def _logged(self, ok, i, action, state, reason):
        name = self.cfg["names"][i]
        if ok and state:
            for j in getattr(self, "_excl_off", []):         # turned off because only one may be on
                self._logged(True, j, "off", 0, f"only one output on: {PINS[i]} switched on")
            self._excl_off = []
        if ok:
            self.log(f"Switch {PINS[i]} '{name}': {action} ({reason})", "warn" if reason != "manual" else "ok")
            try:
                self.csv.write(i, action, state, reason, self.cfg["names"])
            except OSError as e:
                self.log(f"Switch log not written: {e}", "err")
        else:
            self.log(f"Switch {PINS[i]} '{name}': {action} FAILED - {self.error}", "err")

    def refresh(self):
        self.command(["SW:STATE?"])

    # --------------------------------------------------------------- watchdog
    def silence(self, now=None):
        """Seconds without BOS data per source, counted from arming at the earliest."""
        now = now or time.time()
        can = now - max(self.last_can or 0, self.armed_at)
        fsw = now - max(self.last_fsw or 0, self.armed_at)
        return can, fsw

    def wd_remaining(self, now=None):
        can, fsw = self.silence(now)
        lim = self.cfg["wd_minutes"] * 60
        src = self.cfg["wd_source"]
        if src == "can":
            return lim - can
        if src == "fsw":
            return lim - fsw
        if src == "both":
            return lim - min(can, fsw)
        return lim - max(can, fsw)                # either

    def wd_stopped(self) -> bool:
        return bool(self.cfg["wd_max"]) and self.triggers >= self.cfg["wd_max"]

    def _tick(self):
        now = time.time()
        dev = self._dev()
        if dev is None:
            self.state, self.slave_ok = None, None
        elif time.monotonic() >= self._next_refresh:
            self._next_refresh = time.monotonic() + (10 if self.slave_ok else 60)
            self.refresh()
        c = self.cfg
        if c["wd_enabled"] and not self.wd_stopped() and not self.wd_busy and now >= self.retry_at \
                and self.wd_remaining(now) <= 0:
            can, fsw = self.silence(now)
            parts = []
            if c["wd_source"] in ("either", "both", "can"):
                parts.append(f"CAN {can / 60:.0f} min")
            if c["wd_source"] in ("either", "both", "fsw"):
                parts.append(f"FSW {fsw / 60:.0f} min")
            reason = f"watchdog: no BOS data ({', '.join(parts)})"
            self.wd_busy = True
            i = c["wd_output"]

            def done(ok):
                self.wd_busy = False
                if ok:
                    self.triggers += 1
                    self.armed_at = time.time()          # next trigger only after another full period
                    if self.wd_stopped():
                        self.log(f"BOS watchdog stopped after {self.triggers} trigger(s)", "warn")
                else:
                    self.retry_at = time.time() + 60
                self.changed.emit()
            self.log(f"BOS watchdog triggered - {reason}", "warn")
            if c["wd_action"] == "pulse":
                self.pulse(i, int(c["wd_pulse_ms"]), reason, done)
            else:
                cmds = self._prefix_exclusive(i) + [f"SW:SET:{i}:1"]
                self.command(cmds, lambda ok: (self._logged(ok, i, "on", 1, reason), done(ok)))
        self.changed.emit()

    def shutdown(self):
        self.timer.stop()


class SwitchSettings(QtWidgets.QDialog):
    def __init__(self, ctl: SwitchController, parent=None):
        super().__init__(parent)
        self.ctl = ctl
        c = ctl.cfg
        self.setWindowTitle("Switch outputs and BOS watchdog")
        v = QtWidgets.QVBoxLayout(self)

        g = QtWidgets.QGroupBox("Outputs (slave Arduino)")
        f = QtWidgets.QFormLayout(g)
        self.names = []
        for i, pin in enumerate(PINS):
            e = QtWidgets.QLineEdit(c["names"][i])
            self.names.append(e)
            f.addRow(f"Name of {pin}", e)
        self.excl = QtWidgets.QCheckBox("Only one output on at a time (switching one on turns the others off)")
        self.excl.setChecked(c["exclusive"])
        f.addRow(self.excl)
        self.mpulse = QtWidgets.QSpinBox()
        self.mpulse.setRange(10, 600000)
        self.mpulse.setSuffix(" ms")
        self.mpulse.setValue(int(c["manual_pulse_ms"]))
        f.addRow("Pulse button length", self.mpulse)
        v.addWidget(g)

        g = QtWidgets.QGroupBox("BOS watchdog")
        f = QtWidgets.QFormLayout(g)
        self.src = QtWidgets.QComboBox()
        for k, text in SOURCES:
            self.src.addItem(text, k)
        self.src.setCurrentIndex([k for k, _ in SOURCES].index(c["wd_source"]))
        f.addRow("Trigger when", self.src)
        self.minutes = QtWidgets.QDoubleSpinBox()
        self.minutes.setRange(0.5, 10080)
        self.minutes.setDecimals(1)
        self.minutes.setSuffix(" min")
        self.minutes.setValue(float(c["wd_minutes"]))
        f.addRow("…has no BOS data for", self.minutes)
        self.mer = QtWidgets.QDoubleSpinBox()
        self.mer.setRange(-50, 100)
        self.mer.setDecimals(1)
        self.mer.setSuffix(" dB")
        self.mer.setValue(float(c["wd_mer_min"]))
        self.mer.setToolTip("An FSW VSA result counts as BOS data when its MER is at least this value")
        f.addRow("FSW counts BOS data at MER ≥", self.mer)
        self.out = QtWidgets.QComboBox()
        for i, pin in enumerate(PINS):
            self.out.addItem(f"{pin}  ({c['names'][i]})", i)
        self.out.setCurrentIndex(int(c["wd_output"]))
        f.addRow("Then switch output", self.out)
        self.action = QtWidgets.QComboBox()
        self.action.addItem("Pulse (on for a time, then off)", "pulse")
        self.action.addItem("Switch ON and leave it on", "on")
        self.action.setCurrentIndex(0 if c["wd_action"] == "pulse" else 1)
        f.addRow("Action", self.action)
        self.wpulse = QtWidgets.QSpinBox()
        self.wpulse.setRange(10, 600000)
        self.wpulse.setSuffix(" ms")
        self.wpulse.setValue(int(c["wd_pulse_ms"]))
        f.addRow("Pulse length", self.wpulse)
        self.maxn = QtWidgets.QSpinBox()
        self.maxn.setRange(0, 1000)
        self.maxn.setSpecialValueText("unlimited")
        self.maxn.setValue(int(c["wd_max"]))
        self.maxn.setToolTip("After this many triggers in a row the watchdog stops until it is re-armed")
        f.addRow("Max. triggers", self.maxn)
        v.addWidget(g)
        for e in self.names:
            e.textChanged.connect(self._names_to_combo)
        self.action.currentIndexChanged.connect(lambda i: self.wpulse.setEnabled(i == 0))
        self.wpulse.setEnabled(self.action.currentIndex() == 0)

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _names_to_combo(self):
        for i, e in enumerate(self.names):
            self.out.setItemText(i, f"{PINS[i]}  ({e.text().strip() or PINS[i]})")

    def accept(self):
        c = self.ctl.cfg
        c["names"] = [e.text().strip() or f"Switch {PINS[i]}" for i, e in enumerate(self.names)]
        c["exclusive"] = self.excl.isChecked()
        c["manual_pulse_ms"] = self.mpulse.value()
        rule_before = {k: c[k] for k in c if k.startswith("wd_")}
        c["wd_source"] = self.src.currentData()
        c["wd_minutes"] = self.minutes.value()
        c["wd_mer_min"] = self.mer.value()
        c["wd_output"] = self.out.currentData()
        c["wd_action"] = self.action.currentData()
        c["wd_pulse_ms"] = self.wpulse.value()
        c["wd_max"] = self.maxn.value()
        self.ctl.save()
        if c["wd_enabled"] and rule_before != {k: c[k] for k in c if k.startswith("wd_")}:
            self.ctl.log(f"BOS watchdog rule changed: {self.ctl.wd_rule_text()}")
        self.ctl.changed.emit()
        super().accept()


class SwitchPanel(QtWidgets.QGroupBox):
    """Main-tab group: one row per output + the watchdog line."""

    def __init__(self, ctl: SwitchController):
        super().__init__("Switch (slave Arduino)")
        self.ctl = ctl
        g = QtWidgets.QGridLayout(self)
        g.setVerticalSpacing(3)
        self.rows = []
        for i, pin in enumerate(PINS):
            led = QtWidgets.QLabel("●")
            name = QtWidgets.QLabel()
            pl = QtWidgets.QLabel(pin)
            pl.setStyleSheet(f"color:{C_DIM};")
            on_b = QtWidgets.QPushButton("ON")
            off_b = QtWidgets.QPushButton("OFF")
            pul_b = QtWidgets.QPushButton("Pulse")
            for b in (on_b, off_b, pul_b):
                b.setFixedWidth(68)
            on_b.clicked.connect(lambda _, i=i: ctl.set_output(i, True))
            off_b.clicked.connect(lambda _, i=i: ctl.set_output(i, False))
            pul_b.clicked.connect(lambda _, i=i: ctl.pulse(i, int(ctl.cfg["manual_pulse_ms"])))
            for c, w in enumerate((led, name, pl, on_b, off_b, pul_b)):
                g.addWidget(w, i, c)
            self.rows.append((led, name, pul_b, (on_b, off_b, pul_b)))
        g.setColumnStretch(1, 1)
        r = len(PINS)
        self.wd_chk = QtWidgets.QCheckBox("BOS watchdog")
        self.wd_chk.setToolTip("Switch an output automatically when no BOS data arrives for too long")
        self.wd_chk.toggled.connect(ctl.set_watchdog)
        self.wd_lbl = QtWidgets.QLabel()
        self.wd_lbl.setWordWrap(True)
        set_b = QtWidgets.QPushButton("Settings…")
        set_b.clicked.connect(lambda: SwitchSettings(ctl, self).exec_())
        g.addWidget(self.wd_chk, r, 0, 1, 2)
        g.addWidget(set_b, r, 4, 1, 2)
        g.addWidget(self.wd_lbl, r + 1, 0, 1, 6)
        self.wd_chk.blockSignals(True)
        self.wd_chk.setChecked(ctl.cfg["wd_enabled"])
        self.wd_chk.blockSignals(False)
        ctl.changed.connect(self.update_view)
        self.update_view()

    def update_view(self):
        ctl, c = self.ctl, self.ctl.cfg
        known = ctl.state is not None and ctl.slave_ok
        for i, (led, name, pul_b, buttons) in enumerate(self.rows):
            on = known and i < len(ctl.state) and ctl.state[i]
            led.setStyleSheet(f"color:{(C_OK if on else C_OFF) if known else C_OFF};font-size:16px;")
            state = ("ON" if on else "off") if known else ("no answer" if ctl.slave_ok is False else "—")
            name.setText(f"<b>{c['names'][i]}</b>  <span style='color:{C_ERR if ctl.slave_ok is False else C_DIM}'>"
                         f"{state}</span>")
            name.setToolTip(ctl.error or "")
            pul_b.setToolTip(f"On for {c['manual_pulse_ms']} ms (set in Settings…)")
            for b in buttons:
                b.setEnabled(ctl._dev() is not None)
        now = time.time()
        last = f"BOS data: CAN {fmt_age(now - ctl.last_can if ctl.last_can else None)}, " \
               f"FSW {fmt_age(now - ctl.last_fsw if ctl.last_fsw else None)}"
        if not c["wd_enabled"]:
            text, color = f"Off.  {last}", C_DIM
        elif ctl.wd_stopped():
            text, color = f"Stopped after {ctl.triggers} trigger(s) - untick and tick to re-arm.  {last}", C_ERR
        else:
            left = ctl.wd_remaining(now)
            n = f"  ·  triggered {ctl.triggers}×" if ctl.triggers else ""
            out = PINS[c["wd_output"]]
            if ctl.wd_busy:
                text, color = f"Switching {out}…  {last}", C_WARN
            elif left <= 0:
                text, color = f"Due now (retrying, {ctl.error or 'waiting'})  {last}", C_ERR
            else:
                text = f"Armed: {out} '{c['names'][c['wd_output']]}' in {fmt_left(left)}{n}.  {last}"
                color = C_WARN if left < 300 else C_DIM
        self.wd_lbl.setText(text)
        self.wd_lbl.setStyleSheet(f"color:{color};")
        self.wd_lbl.setToolTip(ctl.wd_rule_text())
        if self.wd_chk.isChecked() != c["wd_enabled"]:
            self.wd_chk.blockSignals(True)
            self.wd_chk.setChecked(c["wd_enabled"])
            self.wd_chk.blockSignals(False)
