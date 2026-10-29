# -*- coding: utf-8 -*-
"""
mxg_generator.py
================
Keysight N5183B MXG X-Series microwave analog signal generator tab.

Connection through VISA: LAN (HiSLIP, VXI-11 or raw socket 5025), USB (USB-TMC, Keysight
vendor id 0x0957) or GPIB - any VISA resource string can be typed in.

SCPI: Keysight X-Series signal generator command set (same tree as PSG / MXG), long form:
    RF        :FREQuency[:CW], :POWer[:LEVel][:IMMediate][:AMPLitude], :OUTPut[:STATe],
              :OUTPut:MODulation[:STATe], :POWer:ALC[:STATe], :FREQuency:OFFSet, :POWer:OFFSet
    AM / FM / PM (option UNT)
              :AM[:STATe|:DEPTh|:SOURce|:INTernal:FREQuency|:INTernal:FUNCtion:SHAPe]
              :FM[:STATe|:DEViation|...], :PM[:STATe|:DEViation (rad)|...]
    Pulse (option UNU / UNW)
              :PULM:STATe, :PULM:SOURce, :PULM:SOURce:INTernal {SQUare|FRUN|TRIGgered|DOUBlet|GATEd|PTRain},
              :PULM:INTernal:FREQuency / PERiod / PWIDth / DELay
    Sweep     :FREQuency:MODE {CW|LIST}, :POWer:MODE {FIXed|LIST}, :LIST:TYPE STEP, :FREQuency:STARt/STOP,
              :POWer:STARt/STOP, :SWEep:POINts, :SWEep:DWELl, :SWEep:SPACing, :LIST:TRIGger:SOURce,
              :INITiate:CONTinuous, :INITiate[:IMMediate]
    LF out    :LFOutput:STATe, :LFOutput:AMPLitude
    System    *IDN?, *OPT?, *RST, *CLS, *TRG, *SAV/*RCL n, :SYSTem:PRESet, :SYSTem:ERRor?
Every change is followed by :SYSTem:ERRor?, so a command the instrument does not accept (e.g. a
missing option) is reported next to the setting that caused it. Anything else via the console.
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

_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
PROTOCOLS = [("LAN - HiSLIP", "hislip"), ("LAN - VXI-11", "vxi11"), ("LAN - socket 5025", "socket"),
             ("USB / GPIB / VISA address", "visa")]
SHAPES = [("Sine", "SINE"), ("Triangle", "TRIangle"), ("Square", "SQUare"), ("Ramp", "RAMP"), ("Noise", "NOISe")]
SRC_IE = [("Internal", "INTernal"), ("External", "EXTernal")]

# key, label, kind, SCPI, group, option needed (None / tuple), choices
#   kind: hz dbm db pct rad s v int enum bool
P = [
    # ----- RF
    ("freq", "Frequency", "hz", ":FREQuency:CW", "RF", None, None),
    ("power", "Power", "dbm", ":POWer:LEVel:IMMediate:AMPLitude", "RF", None, None),
    ("alc", "ALC", "bool", ":POWer:ALC:STATe", "RF", None, None),
    ("foffs", "Frequency offset", "hz", ":FREQuency:OFFSet", "RF", None, None),
    ("poffs", "Power offset", "db", ":POWer:OFFSet", "RF", None, None),
    # ----- AM
    ("am_src", "AM source", "enum", ":AM:SOURce", "AM", ("UNT",), SRC_IE),
    ("am_depth", "AM depth", "pct", ":AM:DEPTh", "AM", ("UNT",), None),
    ("am_f", "AM rate", "hz", ":AM:INTernal:FREQuency", "AM", ("UNT",), None),
    ("am_shape", "AM waveform", "enum", ":AM:INTernal:FUNCtion:SHAPe", "AM", ("UNT",), SHAPES),
    ("am_on", "AM on", "bool", ":AM:STATe", "AM", ("UNT",), None),
    # ----- FM
    ("fm_src", "FM source", "enum", ":FM:SOURce", "FM", ("UNT",), SRC_IE),
    ("fm_dev", "FM deviation", "hz", ":FM:DEViation", "FM", ("UNT",), None),
    ("fm_f", "FM rate", "hz", ":FM:INTernal:FREQuency", "FM", ("UNT",), None),
    ("fm_shape", "FM waveform", "enum", ":FM:INTernal:FUNCtion:SHAPe", "FM", ("UNT",), SHAPES),
    ("fm_on", "FM on", "bool", ":FM:STATe", "FM", ("UNT",), None),
    # ----- PM
    ("pm_src", "ΦM source", "enum", ":PM:SOURce", "ΦM", ("UNT",), SRC_IE),
    ("pm_dev", "ΦM deviation", "rad", ":PM:DEViation", "ΦM", ("UNT",), None),
    ("pm_f", "ΦM rate", "hz", ":PM:INTernal:FREQuency", "ΦM", ("UNT",), None),
    ("pm_shape", "ΦM waveform", "enum", ":PM:INTernal:FUNCtion:SHAPe", "ΦM", ("UNT",), SHAPES),
    ("pm_on", "ΦM on", "bool", ":PM:STATe", "ΦM", ("UNT",), None),
    # ----- Pulse
    ("pu_src", "Pulse source", "enum", ":PULM:SOURce", "Pulse", ("UNU", "UNW"), SRC_IE),
    ("pu_type", "Internal pulse type", "enum", ":PULM:SOURce:INTernal", "Pulse", ("UNU", "UNW"),
     [("Free run", "FRUN"), ("Square", "SQUare"), ("Triggered", "TRIGgered"), ("Doublet", "DOUBlet"),
      ("Gated", "GATEd"), ("Pulse train", "PTRain")]),
    ("pu_per", "Pulse period", "s", ":PULM:INTernal:PERiod", "Pulse", ("UNU", "UNW"), None),
    ("pu_width", "Pulse width", "s", ":PULM:INTernal:PWIDth", "Pulse", ("UNU", "UNW"), None),
    ("pu_delay", "Pulse delay", "s", ":PULM:INTernal:DELay", "Pulse", ("UNU", "UNW"), None),
    ("pu_rate", "Square-wave rate", "hz", ":PULM:INTernal:FREQuency", "Pulse", ("UNU", "UNW"), None),
    ("pu_on", "Pulse on", "bool", ":PULM:STATe", "Pulse", ("UNU", "UNW"), None),
    # ----- Sweep (step sweep)
    ("sw_fstart", "Start frequency", "hz", ":FREQuency:STARt", "Sweep", None, None),
    ("sw_fstop", "Stop frequency", "hz", ":FREQuency:STOP", "Sweep", None, None),
    ("sw_pstart", "Start power", "dbm", ":POWer:STARt", "Sweep", None, None),
    ("sw_pstop", "Stop power", "dbm", ":POWer:STOP", "Sweep", None, None),
    ("sw_points", "Points", "int", ":SWEep:POINts", "Sweep", None, (2, 65535)),
    ("sw_dwell", "Dwell per point", "s", ":SWEep:DWELl", "Sweep", None, None),
    ("sw_space", "Spacing", "enum", ":SWEep:SPACing", "Sweep", None,
     [("Linear", "LINear"), ("Logarithmic", "LOGarithmic")]),
    ("sw_trig", "Sweep trigger", "enum", ":LIST:TRIGger:SOURce", "Sweep", None,
     [("Immediate", "IMMediate"), ("Bus (*TRG)", "BUS"), ("External", "EXTernal"), ("Trigger key", "KEY")]),
    ("sw_cont", "Continuous", "bool", ":INITiate:CONTinuous", "Sweep", None, None),
    ("fmode", "Frequency sweep", "enum", ":FREQuency:MODE", "Sweep", None, [("Off (CW)", "CW"), ("On", "LIST")]),
    ("pmode", "Power sweep", "enum", ":POWer:MODE", "Sweep", None, [("Off (fixed)", "FIXed"), ("On", "LIST")]),
    # ----- LF output
    ("lf_ampl", "LF out amplitude", "v", ":LFOutput:AMPLitude", "LF out", None, None),
    ("lf_on", "LF out on", "bool", ":LFOutput:STATe", "LF out", None, None),
    # ----- outputs last: the signal is set up before it appears
    ("mod", "Modulation on", "bool", ":OUTPut:MODulation:STATe", "RF", None, None),
    ("rf", "RF output", "bool", ":OUTPut:STATe", "RF", None, None),
]
PARAMS = {p[0]: p for p in P}
GROUPS = ["RF", "AM", "FM", "ΦM", "Pulse", "Sweep", "LF out"]
UNITS = {"hz": "Hz", "dbm": "dBm", "db": "dB", "pct": "%", "rad": "rad", "s": "s", "v": "V"}


def short(mnemonic: str) -> str:
    m = re.match(r"[A-Z0-9]+", mnemonic)
    return m.group(0) if m else mnemonic.upper()


def match_choice(resp: str, choices):
    r = resp.strip().strip("'\"").upper()
    for _, val in choices:
        s, full = short(val), val.upper()
        if r in (s, full) or (len(r) >= min(3, len(s)) and full.startswith(r)) or r.startswith(s):
            return val
    return None


def parse_si(text: str) -> float:
    t = text.strip().replace(" ", "").replace(",", ".")
    for u in ("dbm", "db", "hz", "rad", "%", "v", "s"):
        if t.lower().endswith(u) and len(t) > len(u):
            t = t[:-len(u)]
            break
    pre = {"G": 1e9, "M": 1e6, "k": 1e3, "K": 1e3, "m": 1e-3, "u": 1e-6, "µ": 1e-6, "n": 1e-9}
    if t and t[-1] in pre:
        return float(t[:-1]) * pre[t[-1]]
    return float(t)


def fmt_si(v, unit="") -> str:
    if v is None or not math.isfinite(v):
        return "—"
    if v == 0:
        return f"0 {unit}".strip()
    e = max(-9, min(9, int(math.floor(math.log10(abs(v)) / 3) * 3)))
    p = {9: "G", 6: "M", 3: "k", 0: "", -3: "m", -6: "u", -9: "n"}[e]
    return f"{v / 10 ** e:.15g} {p}{unit}".strip()


def scpi_value(kind: str, v) -> str:
    if kind == "bool":
        return "ON" if v else "OFF"
    if kind == "enum":
        return str(v)
    if kind == "int":
        return str(int(v))
    suffix = {"hz": " Hz", "dbm": " dBm", "db": " dB", "s": " s", "v": " V"}.get(kind, "")
    return f"{float(v):.12g}{suffix}"


def describe(key, v) -> str:
    kind, choices = PARAMS[key][2], PARAMS[key][6]
    if kind == "enum":
        return next((lbl for lbl, val in choices if val == v), str(v))
    if kind == "bool":
        return "ON" if v else "OFF"
    if kind == "int":
        return str(int(v))
    if kind in ("dbm", "db", "pct", "rad"):
        return f"{v:g} {UNITS[kind]}"
    return fmt_si(v, UNITS.get(kind, ""))


def resource_for(proto: str, addr: str) -> str:
    a = addr.strip()
    if "::" in a or proto == "visa":
        return a
    return {"hislip": f"TCPIP0::{a}::hislip0::INSTR", "vxi11": f"TCPIP0::{a}::inst0::INSTR",
            "socket": f"TCPIP0::{a}::5025::SOCKET"}[proto]


def _rm():
    try:
        return pyvisa.ResourceManager()
    except Exception:
        return pyvisa.ResourceManager("@py")


def find_visa() -> list[tuple[str, bool]]:
    """VISA USB / GPIB resources, Keysight (vendor 0x0957 = 2391) first, marked True."""
    if pyvisa is None:
        return []
    try:
        res = [r for r in _rm().list_resources() if r.upper().startswith(("USB", "GPIB"))]
    except Exception:
        return []
    out = [(r, "0X0957" in r.upper() or "::2391::" in r) for r in res]
    return sorted(out, key=lambda x: not x[1])


def same(a, b) -> bool:
    """Unchanged? Numbers equal within 1e-14 relative (text <-> number round trips are not exact)."""
    if isinstance(a, float) or isinstance(b, float):
        try:
            return math.isclose(float(a), float(b), rel_tol=1e-14, abs_tol=1e-15)
        except (TypeError, ValueError):
            return False
    return a == b


# --------------------------------------------------------------------------- #
#  Drivers                                                                     #
# --------------------------------------------------------------------------- #
class MxgDriver:
    def __init__(self, resource: str, timeout_ms=5000):
        if pyvisa is None:
            raise RuntimeError("pyvisa is not installed:  pip install pyvisa pyvisa-py")
        self.inst = _rm().open_resource(resource)
        self.inst.timeout = timeout_ms
        self.inst.read_termination = "\n"
        self.inst.write_termination = "\n"
        self.addr = resource
        try:
            self.idn = self.q("*IDN?")
            try:
                self.options = {o.strip().upper() for o in self.q("*OPT?").strip('"').split(",") if o.strip()}
            except TimeoutError:
                self.options = None
            self.write("*CLS")
            self.write(":UNIT:POWer DBM")
            self.fmin, self.fmax = self._f(":FREQuency:CW? MIN"), self._f(":FREQuency:CW? MAX")
            self.pmin, self.pmax = self._f(":POWer? MIN"), self._f(":POWer? MAX")
            self.errors()
        except Exception:
            self.inst.close()
            raise

    def write(self, c):
        self.inst.write(c)

    def q(self, c) -> str:
        try:
            return self.inst.query(c).strip()
        except pyvisa.errors.VisaIOError as e:
            if e.error_code == pyvisa.constants.StatusCode.error_timeout:
                try:
                    self.inst.clear()
                except Exception:
                    pass
                raise TimeoutError(f"no reply to {c}")
            raise ConnectionError(f"VISA: {e}")

    def _f(self, c):
        try:
            m = _NUM.search(self.q(c))
            return float(m.group(0)) if m else None
        except TimeoutError:
            return None

    def errors(self) -> list[str]:
        out = []
        for _ in range(20):
            e = self.q(":SYSTem:ERRor?")
            if e.lstrip("+").startswith("0"):
                break
            out.append(e)
        return out

    def get(self, key):
        p = PARAMS[key]
        kind, choices = p[2], p[6]
        r = self.q(p[3] + "?")
        if kind == "enum":
            return match_choice(r, choices)
        if kind == "bool":
            return r.strip().upper() in ("1", "ON")
        m = _NUM.search(r)
        if not m:
            return None
        v = float(m.group(0))
        return int(v) if kind == "int" else v

    def set(self, key, v):
        p = PARAMS[key]
        self.write(f"{p[3]} {scpi_value(p[2], v)}")

    def raw(self, c):
        if "?" in c:
            return self.q(c)
        self.write(c)
        return ""

    def close(self):
        try:
            self.inst.close()
        except Exception:
            pass


class DemoMxg:
    idn = "Keysight Technologies,N5183B,DEMO,simulated"
    addr = "demo"
    options = {"520", "1E1", "UNT", "UNW"}
    fmin, fmax, pmin, pmax = 9e3, 20e9, -20.0, 20.0

    def __init__(self):
        self.s = {"freq": 1e9, "power": -20.0, "alc": True, "foffs": 0.0, "poffs": 0.0,
                  "am_src": "INTernal", "am_depth": 30.0, "am_f": 1e3, "am_shape": "SINE", "am_on": False,
                  "fm_src": "INTernal", "fm_dev": 1e3, "fm_f": 1e3, "fm_shape": "SINE", "fm_on": False,
                  "pm_src": "INTernal", "pm_dev": 0.1, "pm_f": 1e3, "pm_shape": "SINE", "pm_on": False,
                  "pu_src": "INTernal", "pu_type": "FRUN", "pu_per": 100e-6, "pu_width": 10e-6, "pu_delay": 0.0,
                  "pu_rate": 1e3, "pu_on": False, "sw_fstart": 1e9, "sw_fstop": 2e9, "sw_pstart": -20.0,
                  "sw_pstop": -10.0, "sw_points": 101, "sw_dwell": 2e-3, "sw_space": "LINear",
                  "sw_trig": "IMMediate", "sw_cont": False, "fmode": "CW", "pmode": "FIXed",
                  "lf_ampl": 0.5, "lf_on": False, "mod": True, "rf": False}
        self.log = []

    def get(self, key):
        time.sleep(0.001)
        return self.s.get(key)

    def set(self, key, v):
        if key == "freq" and not self.fmin <= v <= self.fmax:
            self._err = "-222,Data out of range"
            return
        self.s[key] = v
        self.log.append(f"{PARAMS[key][3]} {scpi_value(PARAMS[key][2], v)}")

    _err = None

    def errors(self):
        e, self._err = self._err, None
        return [e] if e else []

    def raw(self, c):
        self.log.append(c)
        return self.idn if c.strip().upper() == "*IDN?" else ("+0,\"No error\"" if "?" in c else "")

    def write(self, c):
        self.log.append(c)

    def close(self):
        pass


# --------------------------------------------------------------------------- #
#  Worker                                                                      #
# --------------------------------------------------------------------------- #
class MxgWorker(QtCore.QThread):
    log = QtCore.pyqtSignal(str, str)
    connected = QtCore.pyqtSignal(bool, str)
    state = QtCore.pyqtSignal(object)
    console = QtCore.pyqtSignal(str, str)

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
                self.log.emit(f"N5183B: {type(e).__name__}: {e}", "err")
                if isinstance(e, ConnectionError):
                    self._lost(e)

    def _lost(self, e):
        if self.dev is not None:
            self.dev.close()
        self.dev = None
        self.connected.emit(False, f"connection lost: {e}")

    def shutdown(self):
        self._quit = True
        self.wait(3000)
        if self.dev:
            self.dev.close()

    def connect_dev(self, demo, resource):
        if self.dev:
            self.dev.close()
        try:
            self.dev = DemoMxg() if demo else MxgDriver(resource)
        except Exception as e:
            self.dev = None
            self.connected.emit(False, f"{type(e).__name__}: {e}")
            return
        self.connected.emit(True, f"{self.dev.idn}  ({self.dev.addr})")
        self.read()

    def disconnect_dev(self):
        if self.dev:
            self.dev.close()
        self.dev = None
        self.connected.emit(False, "")

    def available(self, key) -> bool:
        opt = PARAMS[key][5]
        o = getattr(self.dev, "options", None)
        return opt is None or o is None or any(x in o for x in opt)

    def read(self, keys=None):
        if self.dev is None:
            return
        st, failed = dict(self.last), []
        for k in keys or PARAMS:
            if not self.available(k):
                continue
            try:
                v = self.dev.get(k)
            except TimeoutError:
                failed.append(k)
                continue
            if v is not None:
                st[k] = v
        self.dev.errors()
        self.last = st
        self.state.emit(dict(st))
        if failed:
            self.log.emit(f"N5183B: no reply for {', '.join(PARAMS[k][1] for k in failed)}", "warn")

    def apply(self, s: dict, prev=None, label="Applied"):
        if self.dev is None:
            raise RuntimeError("N5183B not connected")
        ref = self.last if prev is None else prev
        sent, errors = [], []
        for key in PARAMS:
            if key not in s or s[key] is None or same(ref.get(key), s[key]):
                continue
            self.dev.set(key, s[key])
            sent.append(f"{PARAMS[key][1]} {describe(key, s[key])}")
            for e in self.dev.errors():
                errors.append(f"{PARAMS[key][1]}: {e}")
        for e in errors:
            self.log.emit(f"N5183B {e}", "err")
        if sent:
            self.log.emit(f"N5183B {label}: " + "; ".join(sent), "warn" if errors else "ok")
        self.read()
        if errors:
            raise RuntimeError(f"{len(errors)} SCPI error(s): " + " | ".join(errors))

    def command(self, c, label=None, reread=False):
        if self.dev is None:
            raise RuntimeError("N5183B not connected")
        reply = self.dev.raw(c)
        errs = [] if c.strip().upper().startswith(":SYST") and "ERR" in c.upper() else self.dev.errors()
        self.console.emit(c, reply)
        if label:
            self.log.emit(f"N5183B {label}", "warn" if errs else "ok")
        for e in errs:
            self.log.emit(f"N5183B '{c}': {e}", "err")
        if reread:
            self.read()
        if errs:
            raise RuntimeError("; ".join(errs))
        return reply


# --------------------------------------------------------------------------- #
#  Field widgets (shared with the sequence step editor)                        #
# --------------------------------------------------------------------------- #
class Field:
    def __init__(self, key):
        self.key = key
        _, label, kind, _, _, _, choices = PARAMS[key]
        self.kind = kind
        if kind == "enum":
            self.w = QtWidgets.QComboBox()
            for lbl, val in choices:
                self.w.addItem(lbl, val)
        elif kind == "bool":
            self.w = QtWidgets.QCheckBox("on")
        elif kind == "int":
            self.w = QtWidgets.QSpinBox()
            lo, hi = choices or (0, 1_000_000)
            self.w.setRange(lo, hi)
        elif kind in ("dbm", "db", "pct", "rad"):
            self.w = QtWidgets.QDoubleSpinBox()
            self.w.setDecimals(2 if kind != "rad" else 3)
            self.w.setRange(*{"dbm": (-150, 35), "db": (-200, 200), "pct": (0, 100), "rad": (0, 100)}[kind])
            self.w.setSuffix(f" {UNITS[kind]}")
        else:
            self.w = QtWidgets.QLineEdit()
            self.w.setPlaceholderText({"hz": "e.g. 2.4G, 10M", "s": "e.g. 10u, 2m", "v": "e.g. 0.5"}.get(kind, ""))
            self.w.setToolTip(f"Unit {UNITS.get(kind, '')}; SI prefixes allowed (n u m k M G)")
        if isinstance(self.w, QtWidgets.QAbstractSpinBox):
            self.w.setKeyboardTracking(False)

    def get(self):
        if self.kind == "enum":
            return self.w.currentData()
        if self.kind == "bool":
            return self.w.isChecked()
        if self.kind in ("int", "dbm", "db", "pct", "rad"):
            return self.w.value()
        t = self.w.text().strip()
        return parse_si(t) if t and t != "—" else None

    def put(self, v):
        if v is None:
            return
        self.w.blockSignals(True)
        if self.kind == "enum":
            i = self.w.findData(v)
            if i >= 0:
                self.w.setCurrentIndex(i)
        elif self.kind == "bool":
            self.w.setChecked(bool(v))
        elif self.kind in ("int", "dbm", "db", "pct", "rad"):
            self.w.setValue(v)
        else:
            self.w.setText(fmt_si(v, UNITS.get(self.kind, "")))
        self.w.blockSignals(False)


# --------------------------------------------------------------------------- #
#  Tab                                                                         #
# --------------------------------------------------------------------------- #
class MxgTab(QtWidgets.QWidget):
    log = QtCore.pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.is_connected = False
        self.idn = ""
        self.fields: dict[str, Field] = {}
        self.dirty: set[str] = set()
        self.worker = MxgWorker()
        self.worker.log.connect(self.log)
        self.worker.connected.connect(self._on_connected)
        self.worker.state.connect(self._on_state)
        self.worker.console.connect(lambda c, r: self.con_out.appendPlainText(f"> {c}" + (f"\n{r}" if r else "")))
        self.worker.start()
        self._build()
        self._set_enabled(False)

    @property
    def rf_on(self) -> bool:
        return self.is_connected and bool(self.worker.last.get("rf"))

    # ------------------------------------------------------------------ UI
    def _build(self):
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        left = QtWidgets.QVBoxLayout()
        g = QtWidgets.QGroupBox("Connection")
        f = QtWidgets.QFormLayout(g)
        self.proto = QtWidgets.QComboBox()
        for lbl, key in PROTOCOLS:
            self.proto.addItem(lbl, key)
        self.proto.currentIndexChanged.connect(self._proto_changed)
        self.addr = QtWidgets.QComboBox()
        self.addr.setEditable(True)
        self.addr.setMinimumContentsLength(16)
        self.addr.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.find_btn = QtWidgets.QPushButton("⟳")
        self.find_btn.setFixedWidth(34)
        self.find_btn.setToolTip("List USB / GPIB instruments (★ = Keysight)")
        self.find_btn.clicked.connect(self.on_find)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.addr, 1)
        row.addWidget(self.find_btn)
        self.addr_w = QtWidgets.QWidget()
        self.addr_w.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        self.demo = QtWidgets.QCheckBox("Demo (no instrument)")
        self.conn_btn = QtWidgets.QPushButton("Connect")
        self.conn_btn.clicked.connect(self.on_connect)
        self.status = QtWidgets.QLabel("not connected")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#8a96a3;")
        f.addRow("Interface", self.proto)
        f.addRow("Address", self.addr_w)
        f.addRow(self.demo)
        f.addRow(self.conn_btn)
        f.addRow(self.status)
        left.addWidget(g)

        self.rf_btn = QtWidgets.QPushButton("RF OUTPUT: OFF")
        self.rf_btn.setCheckable(True)
        self.rf_btn.setMinimumHeight(40)
        self.rf_btn.clicked.connect(self.on_rf)
        self.mod_btn = QtWidgets.QPushButton("MOD: ON")
        self.mod_btn.setCheckable(True)
        self.mod_btn.setToolTip("Modulation master switch (:OUTPut:MODulation)")
        self.mod_btn.clicked.connect(self.on_mod)
        ob = QtWidgets.QHBoxLayout()
        ob.addWidget(self.rf_btn, 3)
        ob.addWidget(self.mod_btn, 1)
        left.addLayout(ob)

        self.g_sys = g = QtWidgets.QGroupBox("Instrument")
        grid = QtWidgets.QGridLayout(g)
        for k, (text, fn) in enumerate((
                ("Apply", self.on_apply), ("Read all", lambda: self.worker.submit(self.worker.read)),
                ("Single sweep", lambda: self._cmd(":INITiate:IMMediate", "single sweep started")),
                ("Trigger (*TRG)", lambda: self._cmd("*TRG", "trigger sent")),
                ("Preset", self.on_preset), ("Clear errors", lambda: self._cmd("*CLS", "error queue cleared")))):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            grid.addWidget(b, k // 2, k % 2)
            if text == "Apply":
                b.setStyleSheet("font-weight:700;")
                b.setToolTip("Send the fields that differ from the instrument's current settings")
        self.slot = QtWidgets.QSpinBox()
        self.slot.setRange(0, 99)
        sv = QtWidgets.QPushButton("Save state")
        sv.clicked.connect(lambda: self._cmd(f"*SAV {self.slot.value()}", f"state saved to register {self.slot.value()}"))
        rc = QtWidgets.QPushButton("Recall state")
        rc.clicked.connect(lambda: self._cmd(f"*RCL {self.slot.value()}", f"register {self.slot.value()} recalled",
                                             reread=True))
        grid.addWidget(QtWidgets.QLabel("Register"), 3, 0)
        grid.addWidget(self.slot, 3, 1)
        grid.addWidget(sv, 4, 0)
        grid.addWidget(rc, 4, 1)
        left.addWidget(g)

        self.g_con = g = QtWidgets.QGroupBox("SCPI console")
        cv = QtWidgets.QVBoxLayout(g)
        self.con_in = QtWidgets.QLineEdit()
        self.con_in.setPlaceholderText("e.g. :FREQuency:CW?   or   :ROSCillator:SOURce?")
        self.con_in.returnPressed.connect(self.on_console)
        self.con_out = QtWidgets.QPlainTextEdit()
        self.con_out.setReadOnly(True)
        self.con_out.setMaximumBlockCount(500)
        send = QtWidgets.QPushButton("Send")
        send.clicked.connect(self.on_console)
        r2 = QtWidgets.QHBoxLayout()
        r2.addWidget(self.con_in, 1)
        r2.addWidget(send)
        cv.addLayout(r2)
        cv.addWidget(self.con_out, 1)
        left.addWidget(g, 1)
        lw = QtWidgets.QWidget()
        lw.setLayout(left)
        lw.setMaximumWidth(400)
        root.addWidget(lw)

        # parameter groups in two scrollable columns
        cols = [("RF", "Sweep", "LF out"), ("AM", "FM", "ΦM", "Pulse")]
        self.groups = {}
        for names in cols:
            w = QtWidgets.QWidget()
            v = QtWidgets.QVBoxLayout(w)
            v.setContentsMargins(0, 0, 0, 0)
            for gname in names:
                g = QtWidgets.QGroupBox(gname)
                gl = QtWidgets.QGridLayout(g)
                gl.setVerticalSpacing(3)
                r = 0
                for key, p in PARAMS.items():
                    if p[4] != gname or key in ("rf", "mod"):
                        continue
                    fld = Field(key)
                    gl.addWidget(QtWidgets.QLabel(p[1]), r, 0)
                    gl.addWidget(fld.w, r, 1)
                    self.fields[key] = fld
                    sig = {"enum": "currentIndexChanged", "bool": "toggled"}.get(
                        fld.kind, "valueChanged" if isinstance(fld.w, QtWidgets.QAbstractSpinBox) else "textEdited")
                    getattr(fld.w, sig).connect(lambda *_, k=key: self.dirty.add(k))
                    r += 1
                gl.setColumnStretch(1, 1)
                self.groups[gname] = g
                v.addWidget(g)
            v.addStretch(1)
            sc = QtWidgets.QScrollArea()
            sc.setWidget(w)
            sc.setWidgetResizable(True)
            sc.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            sc.setMinimumWidth(320)
            root.addWidget(sc, 1)
        self._proto_changed()

    def _proto_changed(self, *_):
        visa = self.proto.currentData() == "visa"
        self.find_btn.setVisible(visa)
        self.addr.lineEdit().setPlaceholderText("USB0::0x0957::...::INSTR" if visa else "IP address or hostname")

    def _set_enabled(self, on):
        for w in [self.g_sys, self.g_con, self.rf_btn, self.mod_btn] + list(self.groups.values()):
            w.setEnabled(on)
        for w in (self.proto, self.addr_w, self.demo):
            w.setEnabled(not on)
        self.conn_btn.setText("Disconnect" if on else "Connect")
        if on:
            self._apply_options()

    def _apply_options(self):
        """Grey out modulation groups whose option the instrument does not report in *OPT?."""
        for gname, g in self.groups.items():
            keys = [k for k, p in PARAMS.items() if p[4] == gname]
            ok = all(self.worker.available(k) for k in keys)
            g.setEnabled(ok)
            opt = PARAMS[keys[0]][5] if keys else None
            g.setTitle(gname if ok else f"{gname}  (option {'/'.join(opt)} not installed)")

    # ------------------------------------------------------------- actions
    def on_find(self):
        found = find_visa()
        cur = self.addr.currentText()
        self.addr.clear()
        for res, ks in found:
            self.addr.addItem(("★ " if ks else "") + res, res)
        if cur:
            self.addr.setEditText(cur)
        elif found:
            self.addr.setCurrentIndex(0)
        if not found:
            self.status.setText("no USB/GPIB instrument found by VISA" if pyvisa else
                                "pyvisa is not installed:  pip install pyvisa pyvisa-py")

    def _addr(self) -> str:
        i, txt = self.addr.currentIndex(), self.addr.currentText().strip()
        if i >= 0 and self.addr.itemText(i) == txt and self.addr.itemData(i):
            return self.addr.itemData(i)
        return txt.lstrip("★ ").strip()

    def on_connect(self):
        if self.is_connected:
            self.worker.submit(self.worker.disconnect_dev)
            return
        demo = self.demo.isChecked()
        addr = self._addr()
        if not demo and not addr:
            self.log.emit("N5183B: enter the IP address or VISA address", "warn")
            return
        res = "demo" if demo else resource_for(self.proto.currentData(), addr)
        self.status.setText("connecting…")
        self.conn_btn.setEnabled(False)
        self.worker.submit(lambda: self.worker.connect_dev(demo, res))

    def _on_connected(self, ok, info):
        self.conn_btn.setEnabled(True)
        self.is_connected = ok
        self.idn = info.split("  (")[0] if ok else ""
        self._set_enabled(ok)
        dev = self.worker.dev
        if ok and dev is not None:
            rng = ""
            if dev.fmin and dev.fmax:
                rng = f"\n{fmt_si(dev.fmin, 'Hz')} … {fmt_si(dev.fmax, 'Hz')}"
                self.fields["freq"].w.setToolTip(f"Instrument range {rng.strip()}")
            if dev.pmin is not None and dev.pmax is not None:
                rng += f", {dev.pmin:g} … {dev.pmax:g} dBm"
            opts = f"\noptions: {', '.join(sorted(dev.options))}" if getattr(dev, "options", None) else ""
            info = info + rng + opts
        self.status.setText(f"✓ {info}" if ok else (info or "not connected"))
        self.status.setStyleSheet(f"color:{'#4fd18b' if ok else ('#ff6b6b' if info else '#8a96a3')};")
        if ok:
            self.log.emit(f"N5183B connected: {self.idn}", "ok")
        elif info:
            self.log.emit(f"N5183B: {info}", "err")

    def _on_state(self, st):
        self.dirty.clear()
        for key, fld in self.fields.items():
            if key in st:
                fld.put(st[key])
        self._show_rf(bool(st.get("rf")))
        self._show_mod(st.get("mod", True))

    def _show_rf(self, on):
        self.rf_btn.setChecked(on)
        self.rf_btn.setText(f"RF OUTPUT: {'ON' if on else 'OFF'}")
        self.rf_btn.setStyleSheet("background:#8b1d1d;border-color:#f85149;font-weight:700;" if on else
                                  "font-weight:700;")

    def _show_mod(self, on):
        self.mod_btn.setChecked(bool(on))
        self.mod_btn.setText(f"MOD: {'ON' if on else 'OFF'}")

    def collect(self) -> dict:
        out = {}
        for key, fld in self.fields.items():
            if not self.groups[PARAMS[key][4]].isEnabled():      # option not installed
                continue
            v = fld.get()
            if v is not None:
                out[key] = v
        return out

    def on_apply(self):
        try:
            s = self.collect()
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "N5183B", f"Could not read a field: {e}")
            return
        self.worker.submit(lambda: self.worker.apply(s))

    def on_rf(self, on):
        self._show_rf(not on)            # shown for real once the instrument confirms
        self.worker.submit(lambda: self.worker.apply({"rf": on}, label="RF output"))

    def on_mod(self, on):
        self._show_mod(not on)
        self.worker.submit(lambda: self.worker.apply({"mod": on}, label="modulation"))

    def _cmd(self, c, label, reread=False):
        self.worker.submit(lambda: self.worker.command(c, label, reread))

    def on_preset(self):
        if QtWidgets.QMessageBox.question(self, "N5183B", "Preset the generator (:SYSTem:PRESet)?\n"
                                          "RF output goes OFF and all settings return to defaults.") \
                == QtWidgets.QMessageBox.Yes:
            self._cmd(":SYSTem:PRESet", "preset", reread=True)

    def on_console(self):
        c = self.con_in.text().strip()
        if c:
            self.worker.submit(lambda: self.worker.command(c))

    def shutdown(self):
        self.worker.shutdown()
