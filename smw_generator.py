# -*- coding: utf-8 -*-
"""
smw_generator.py
================
R&S SMW200A vector signal generator tab for fsw_vsa_monitor.py.

SCPI (checked against R&S's official RsSmw driver):
    SOUR1:FREQ:CW, SOUR1:POW:LEV:IMM:AMPL, OUTP1:STAT, SOUR1:MOD:ALL:STAT
    Custom digital modulation:  SOUR1:BB:DM:STAT / FORM / SRAT / FILT:TYPE /
        FILT:PAR:RCOS (alpha) / FILT:PAR:COS:ROLL / FILT:PAR:GAUS (BT) / SOUR / PRBS:LENG
"""

from __future__ import annotations

import math
import queue
import re
import time

from PyQt5 import QtCore, QtWidgets

try:
    import pyvisa
except ImportError:  # pragma: no cover
    pyvisa = None
import visa_rm      # one shared VISA ResourceManager (installed VISA first)
# label, SCPI mnemonic (long form; the instrument answers with the short form)
FORMATS = [("BPSK", "BPSK"), ("QPSK", "QPSK"), ("OQPSK", "OQPSk"), ("pi/4-QPSK", "P4QPsk"),
           ("pi/4-DQPSK", "P4DQpsk"), ("8PSK", "PSK8"), ("16QAM", "QAM16"), ("32QAM", "QAM32"),
           ("64QAM", "QAM64"), ("128QAM", "QAM128"), ("256QAM", "QAM256"), ("512QAM", "QAM512"),
           ("1024QAM", "QAM1024"), ("16APSK", "APSK16"), ("32APSK", "APSK32"), ("MSK", "MSK")]
FILTERS = [("Root raised cosine", "RCOSine", "FILT:PAR:RCOS", "Alpha"),
           ("Raised cosine", "COSine", "FILT:PAR:COS:ROLL", "Roll-off"),
           ("Gauss", "GAUSs", "FILT:PAR:GAUS", "BT"),
           ("Rectangle", "RECTangle", None, "")]
SOURCES = [("PRBS", "PRBS"), ("All 0", "ZERO"), ("All 1", "ONE")]
PRBS_LENGTHS = [9, 11, 15, 16, 20, 21, 23]
PROTOCOLS = ["HiSLIP (hislip0)", "VXI-11 (INSTR)", "Raw socket (5025)", "Demo (no instrument)"]
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def short(mnemonic: str) -> str:
    """SCPI short form: the leading upper-case letters / digits ('OQPSk' -> 'OQPS')."""
    m = re.match(r"[A-Z0-9]+", mnemonic)
    return m.group(0) if m else mnemonic.upper()


def from_short(resp: str, table, col=1):
    r = resp.strip().strip("'\"").upper()
    for row in table:
        if r in (short(row[col]), row[col].upper()):
            return row[col]
    return None


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


def fmt_si(v, unit="") -> str:
    if v is None or not math.isfinite(v):
        return "—"
    if v == 0:
        return f"0 {unit}".strip()
    e = max(-6, min(12, int(math.floor(math.log10(abs(v)) / 3) * 3)))
    p = {12: "T", 9: "G", 6: "M", 3: "k", 0: "", -3: "m", -6: "u"}[e]
    return f"{v / 10 ** e:.10g} {p}{unit}".strip()


# --------------------------------------------------------------------------- #
#  Drivers                                                                     #
# --------------------------------------------------------------------------- #
class SmwDriver:
    def __init__(self, resource: str, timeout_ms: int = 10000):
        if pyvisa is None:
            raise RuntimeError("pyvisa is not installed:  pip install pyvisa pyvisa-py")
        self.inst = visa_rm.open_resource(resource)
        self.inst.timeout = timeout_ms
        if resource.upper().endswith("::SOCKET"):
            self.inst.read_termination = "\n"
            self.inst.write_termination = "\n"
        self.idn = self.q("*IDN?")
        self.write("*CLS")

    def write(self, c):
        self.inst.write(c)

    def q(self, c) -> str:
        return self.inst.query(c).strip()

    def errors(self) -> list[str]:
        out = []
        for _ in range(20):
            e = self.q("SYST:ERR?")
            if e.lstrip("+").startswith("0"):
                break
            out.append(e)
        return out

    def _f(self, c):
        try:
            m = _NUM.search(self.q(c))
            return float(m.group(0)) if m else None
        except Exception:
            return None

    def _b(self, c):
        try:
            return self.q(c).upper() in ("1", "ON")
        except Exception:
            return None

    def _s(self, c):
        try:
            return self.q(c)
        except Exception:
            return None

    def read_settings(self) -> dict:
        s = {
            "freq": self._f("SOUR1:FREQ:CW?"),
            "level": self._f("SOUR1:POW:LEV:IMM:AMPL?"),
            "rf": self._b("OUTP1:STAT?"),
            "mod_all": self._b("SOUR1:MOD:ALL:STAT?"),
            "dm": self._b("SOUR1:BB:DM:STAT?"),
            "format": None, "srate": self._f("SOUR1:BB:DM:SRAT?"),
            "filter": None, "fparam": None, "source": None, "prbs": None,
        }
        fm = self._s("SOUR1:BB:DM:FORM?")
        s["format"] = from_short(fm, FORMATS) if fm else None
        ft = self._s("SOUR1:BB:DM:FILT:TYPE?")
        s["filter"] = from_short(ft, FILTERS) if ft else None
        for _, mn, pcmd, _ in FILTERS:
            if mn == s["filter"] and pcmd:
                s["fparam"] = self._f(f"SOUR1:BB:DM:{pcmd}?")
        src = self._s("SOUR1:BB:DM:SOUR?")
        s["source"] = from_short(src, SOURCES) if src else None
        p = self._s("SOUR1:BB:DM:PRBS:LENG?")
        m = re.search(r"\d+", p or "")
        s["prbs"] = int(m.group(0)) if m else None
        self.errors()                              # options that are missing just give errors
        return s

    def apply(self, s: dict, prev: dict | None = None):
        """Send the keys of `s` whose value differs from `prev`. Order matters:
        modulation settings first, RF output last."""
        prev = prev or {}

        def ch(k):
            return k in s and s[k] is not None and s[k] != prev.get(k)
        cmds = []
        if ch("freq"):
            cmds.append(f"SOUR1:FREQ:CW {s['freq']:.10g}")
        if ch("level"):
            cmds.append(f"SOUR1:POW:LEV:IMM:AMPL {s['level']:.3f}")
        if ch("format"):
            cmds.append(f"SOUR1:BB:DM:FORM {s['format'].upper()}")
        if ch("srate"):
            cmds.append(f"SOUR1:BB:DM:SRAT {s['srate']:.10g}")
        flt = s.get("filter") or prev.get("filter")
        if ch("filter"):
            cmds.append(f"SOUR1:BB:DM:FILT:TYPE {s['filter'].upper()}")
        if ch("fparam") or (ch("filter") and s.get("fparam") is not None):
            pcmd = next((p for _, mn, p, _ in FILTERS if mn == flt), None)
            if pcmd:
                cmds.append(f"SOUR1:BB:DM:{pcmd} {s['fparam']:.4g}")
        if ch("source"):
            cmds.append(f"SOUR1:BB:DM:SOUR {s['source'].upper()}")
        if ch("prbs"):
            cmds.append(f"SOUR1:BB:DM:PRBS:LENG PN{int(s['prbs'])}")
        if ch("dm"):
            cmds.append(f"SOUR1:BB:DM:STAT {'ON' if s['dm'] else 'OFF'}")
        if ch("mod_all"):
            cmds.append(f"SOUR1:MOD:ALL:STAT {'ON' if s['mod_all'] else 'OFF'}")
        if ch("rf"):
            cmds.append(f"OUTP1:STAT {'ON' if s['rf'] else 'OFF'}")
        errors = []
        for c in cmds:
            self.write(c)
            errors += [f"{c}  →  {e}" for e in self.errors()]
        self.q("*OPC?")
        return cmds, errors

    def close(self):
        try:
            self.inst.close()
        except Exception:
            pass


class DemoSmw:
    idn = "Rohde&Schwarz,SMW200A,DEMO,simulated"

    def __init__(self):
        self.s = {"freq": 2.4e9, "level": -30.0, "rf": False, "mod_all": True, "dm": False,
                  "format": "QPSK", "srate": 10e6, "filter": "RCOSine", "fparam": 0.35,
                  "source": "PRBS", "prbs": 9}

    def read_settings(self):
        return dict(self.s)

    def apply(self, s, prev=None):
        cmds = []
        for k, v in s.items():
            if v is not None and v != self.s.get(k):
                self.s[k] = v
                cmds.append(f"{k} = {v}")
        time.sleep(0.05)
        return cmds, []

    def close(self):
        pass


# --------------------------------------------------------------------------- #
#  Worker                                                                      #
# --------------------------------------------------------------------------- #
class SmwWorker(QtCore.QThread):
    log = QtCore.pyqtSignal(str, str)
    connected = QtCore.pyqtSignal(bool, str)
    state = QtCore.pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.jobs: queue.Queue = queue.Queue()
        self.dev = None
        self._quit = False
        self.last: dict = {}

    def submit(self, fn):
        self.jobs.put(fn)

    def run(self):
        while not self._quit:
            try:
                fn = self.jobs.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                fn()
            except Exception as e:
                self.log.emit(f"SMW200A: {type(e).__name__}: {e}", "err")

    def shutdown(self):
        self._quit = True
        self.wait(3000)
        if self.dev:
            self.dev.close()

    # jobs
    def connect_dev(self, demo, resource):
        if self.dev:
            self.dev.close()
        try:
            self.dev = DemoSmw() if demo else SmwDriver(resource)
        except Exception as e:
            self.dev = None
            self.connected.emit(False, f"{type(e).__name__}: {e}")
            return
        self.connected.emit(True, self.dev.idn)
        self.read()

    def disconnect_dev(self):
        if self.dev:
            self.dev.close()
        self.dev = None
        self.connected.emit(False, "")

    def read(self):
        self.last = self.dev.read_settings()
        self.state.emit(dict(self.last))

    def apply(self, s, prev=None, label="Applied"):
        """prev=None: compare with the last read state (only changes are sent)."""
        if self.dev is None:
            raise RuntimeError("SMW200A not connected")
        cmds, errors = self.dev.apply(s, self.last if prev is None else prev)
        for e in errors:
            self.log.emit(f"SMW200A {e}", "err")
        if cmds:
            self.log.emit(f"SMW200A {label}: " + "; ".join(cmds), "warn" if errors else "ok")
        self.read()
        if errors:
            raise RuntimeError(f"{len(errors)} SCPI error(s)")


# --------------------------------------------------------------------------- #
#  Tab                                                                         #
# --------------------------------------------------------------------------- #
class SmwTab(QtWidgets.QWidget):
    log = QtCore.pyqtSignal(str, str)

    def __init__(self, vsa_settings=None):
        """vsa_settings: callable returning the FSW VSA settings dict (for 'Copy from VSA')."""
        super().__init__()
        self.vsa_settings = vsa_settings
        self.is_connected = False
        self.idn = ""
        self.worker = SmwWorker()
        self.worker.log.connect(self.log)
        self.worker.connected.connect(self._on_connected)
        self.worker.state.connect(self._on_state)
        self.worker.start()
        self._build()
        self._set_enabled(False)

    def _build(self):
        root = QtWidgets.QHBoxLayout(self)
        left = QtWidgets.QVBoxLayout()
        g = QtWidgets.QGroupBox("Connection")
        f = QtWidgets.QFormLayout(g)
        self.addr = QtWidgets.QLineEdit("192.168.0.20")
        self.addr.setToolTip("IP / hostname or a full VISA resource string")
        self.proto = QtWidgets.QComboBox()
        self.proto.addItems(PROTOCOLS)
        self.conn_btn = QtWidgets.QPushButton("Connect")
        self.conn_btn.clicked.connect(self.on_connect)
        self.status = QtWidgets.QLabel("not connected")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#8a96a3;")
        f.addRow("Address", self.addr)
        f.addRow("Protocol", self.proto)
        f.addRow(self.conn_btn)
        f.addRow(self.status)
        left.addWidget(g)

        self.g_rf = g = QtWidgets.QGroupBox("RF")
        f = QtWidgets.QFormLayout(g)
        self.freq = QtWidgets.QLineEdit()
        self.freq.setPlaceholderText("e.g. 2.4 GHz")
        self.level = QtWidgets.QDoubleSpinBox()
        self.level.setRange(-145, 30)
        self.level.setDecimals(2)
        self.level.setSuffix(" dBm")
        self.mod_all = QtWidgets.QCheckBox("Modulation on (MOD:ALL)")
        self.rf_btn = QtWidgets.QPushButton("RF OUTPUT: OFF")
        self.rf_btn.setCheckable(True)
        self.rf_btn.setMinimumHeight(40)
        self.rf_btn.clicked.connect(self.on_rf)
        f.addRow("Frequency", self.freq)
        f.addRow("Level", self.level)
        f.addRow(self.mod_all)
        f.addRow(self.rf_btn)
        left.addWidget(g)

        self.g_dm = g = QtWidgets.QGroupBox("Custom digital modulation (BB:DM)")
        f = QtWidgets.QFormLayout(g)
        self.dm = QtWidgets.QCheckBox("Baseband DM on")
        self.fmt = QtWidgets.QComboBox()
        for lbl, mn in FORMATS:
            self.fmt.addItem(lbl, mn)
        self.srate = QtWidgets.QLineEdit()
        self.srate.setPlaceholderText("e.g. 10M")
        self.flt = QtWidgets.QComboBox()
        for lbl, mn, _, _ in FILTERS:
            self.flt.addItem(lbl, mn)
        self.flt.currentIndexChanged.connect(self._filter_changed)
        self.fparam = QtWidgets.QDoubleSpinBox()
        self.fparam.setRange(0.0, 1.0)
        self.fparam.setDecimals(3)
        self.fparam.setSingleStep(0.05)
        self.fparam_lbl = QtWidgets.QLabel("Alpha")
        self.src = QtWidgets.QComboBox()
        for lbl, mn in SOURCES:
            self.src.addItem(lbl, mn)
        self.prbs = QtWidgets.QComboBox()
        for n in PRBS_LENGTHS:
            self.prbs.addItem(f"PRBS {n}", n)
        f.addRow(self.dm)
        f.addRow("Modulation", self.fmt)
        f.addRow("Symbol rate", self.srate)
        f.addRow("Filter", self.flt)
        f.addRow(self.fparam_lbl, self.fparam)
        f.addRow("Data", self.src)
        f.addRow("PRBS", self.prbs)
        col2 = QtWidgets.QVBoxLayout()
        col2.addWidget(g)

        btns = QtWidgets.QHBoxLayout()
        self.apply_btn = QtWidgets.QPushButton("Apply ▶ SMW")
        self.apply_btn.setStyleSheet("font-weight:700;")
        self.apply_btn.clicked.connect(self.on_apply)
        self.read_btn = QtWidgets.QPushButton("Read ◀ SMW")
        self.read_btn.clicked.connect(lambda: self.worker.submit(self.worker.read))
        self.copy_btn = QtWidgets.QPushButton("Copy from FSW VSA")
        self.copy_btn.setToolTip("Frequency, modulation, symbol rate and filter alpha from the VSA settings")
        self.copy_btn.clicked.connect(self.on_copy_vsa)
        for b in (self.apply_btn, self.read_btn):
            btns.addWidget(b)
        left.addLayout(btns)
        left.addWidget(self.copy_btn)
        left.addStretch(1)
        col2.addStretch(1)
        for lay in (left, col2):
            lw = QtWidgets.QWidget()
            lw.setLayout(lay)
            lw.setMaximumWidth(420)
            root.addWidget(lw)

        info = QtWidgets.QLabel(
            "<b>SMW200A</b><br><br>Apply sends only the settings that changed since the last read.<br>"
            "Settings are applied in this order: frequency, level, modulation, baseband on/off, "
            "modulation on/off, RF output.<br><br>The RF button switches the output immediately.<br><br>"
            "Main tab test sequences can set any of these parameters ('SMW' step).")
        info.setWordWrap(True)
        info.setAlignment(QtCore.Qt.AlignTop)
        info.setStyleSheet("color:#8a96a3;padding:12px;")
        root.addWidget(info, 1)

    def _filter_changed(self, *_):
        row = FILTERS[self.flt.currentIndex()]
        self.fparam_lbl.setText(row[3] or "—")
        self.fparam.setEnabled(row[2] is not None)

    def _set_enabled(self, on):
        for w in (self.g_rf, self.g_dm, self.apply_btn, self.read_btn, self.copy_btn):
            w.setEnabled(on)
        self.addr.setEnabled(not on)
        self.proto.setEnabled(not on)
        self.conn_btn.setText("Disconnect" if on else "Connect")

    def _resource(self):
        a = self.addr.text().strip()
        if "::" in a:
            return a
        return [f"TCPIP0::{a}::hislip0::INSTR", f"TCPIP0::{a}::inst0::INSTR",
                f"TCPIP0::{a}::5025::SOCKET"][min(self.proto.currentIndex(), 2)]

    def on_connect(self):
        if self.is_connected:
            self.worker.submit(self.worker.disconnect_dev)
            return
        demo = self.proto.currentIndex() == 3
        res = self._resource()
        self.status.setText("connecting…")
        self.worker.submit(lambda: self.worker.connect_dev(demo, res))

    def _on_connected(self, ok, info):
        self.is_connected = ok
        self.idn = info if ok else ""
        self._set_enabled(ok)
        self.status.setText(info or "not connected")
        if ok:
            self.log.emit(f"SMW200A connected: {info}", "ok")
        elif info:
            self.log.emit(f"SMW200A: {info}", "err")

    def _on_state(self, s):
        if s.get("freq") is not None:
            self.freq.setText(fmt_si(s["freq"], "Hz"))
        if s.get("level") is not None:
            self.level.setValue(s["level"])
        if s.get("srate") is not None:
            self.srate.setText(fmt_si(s["srate"], "Hz"))
        for combo, key in ((self.fmt, "format"), (self.flt, "filter"), (self.src, "source"), (self.prbs, "prbs")):
            i = combo.findData(s.get(key))
            if i >= 0:
                combo.setCurrentIndex(i)
        if s.get("fparam") is not None:
            self.fparam.setValue(s["fparam"])
        for cb, key in ((self.dm, "dm"), (self.mod_all, "mod_all")):
            if s.get(key) is not None:
                cb.setChecked(s[key])
        self._show_rf(bool(s.get("rf")))

    def _show_rf(self, on):
        self.rf_btn.setChecked(on)
        self.rf_btn.setText(f"RF OUTPUT: {'ON' if on else 'OFF'}")
        self.rf_btn.setStyleSheet("background:#8b1d1d;border-color:#f85149;font-weight:700;"
                                  if on else "font-weight:700;")

    def collect(self) -> dict:
        return {"freq": parse_si(self.freq.text()), "level": self.level.value(),
                "mod_all": self.mod_all.isChecked(), "dm": self.dm.isChecked(),
                "format": self.fmt.currentData(), "srate": parse_si(self.srate.text()),
                "filter": self.flt.currentData(), "fparam": self.fparam.value(),
                "source": self.src.currentData(), "prbs": self.prbs.currentData()}

    def on_apply(self):
        try:
            s = self.collect()
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "SMW200A", f"Could not parse a field: {e}")
            return
        self.worker.submit(lambda: self.worker.apply(s))

    def on_rf(self, on):
        self._show_rf(not on)       # show the real state once the instrument confirms
        self.worker.submit(lambda: self.worker.apply({"rf": on}, label="RF output"))

    def on_copy_vsa(self):
        v = self.vsa_settings() if self.vsa_settings else {}
        if not v:
            self.log.emit("Read the FSW VSA settings first", "warn")
            return
        if v.get("center"):
            self.freq.setText(fmt_si(v["center"], "Hz"))
        if v.get("symbol_rate"):
            self.srate.setText(fmt_si(v["symbol_rate"], "Hz"))
        i = self.fmt.findText(v.get("modulation", ""))
        if i >= 0:
            self.fmt.setCurrentIndex(i)
        if v.get("tfil_alpha") is not None:
            self.flt.setCurrentIndex(0)
            self.fparam.setValue(v["tfil_alpha"])
        self.dm.setChecked(True)
        self.log.emit("SMW fields taken from the VSA settings - press Apply", "info")

    def shutdown(self):
        self.worker.shutdown()
