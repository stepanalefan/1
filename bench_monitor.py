#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bench_monitor.py
================
R&S HMP4040 power-supply control (4 outputs, master output, timed power cycling, daily log).

Used as the "HMP4040" tab of fsw_vsa_monitor.py (keep both files in the same
folder), or standalone:

    python bench_monitor.py            # real hardware
    python bench_monitor.py --demo     # simulated hardware

Hardware
--------
* R&S HMP4040 over USB (virtual COM port, 9600 Bd), USB-TMC (VISA) or LAN
  (raw SCPI socket, port 5025). SCPI as used by pymeasure's HMP4040 driver:
  INST:NSEL n, VOLT, CURR, OUTP:SEL, OUTP:GEN, MEAS:VOLT?, MEAS:CURR?, SYST:REM/LOC.

Also provides visa_manager() (the shared VISA ResourceManager) for the other tabs.

Files
-----
* hmp_log_YYYY-MM-DD.csv: master, on/off, set and measured V/I/P of all 4 channels.

Install:  pip install PyQt5 pyqtgraph numpy pyvisa pyvisa-py pyserial
"""

from __future__ import annotations

import csv
import datetime as _dt
import os
import queue
import socket
import sys
import threading
import time

import numpy as np
from PyQt5 import QtCore, QtWidgets
import pyqtgraph as pg

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # pragma: no cover - demo mode still works
    serial = None
try:
    import pyvisa
except ImportError:  # pragma: no cover
    pyvisa = None


HMP_CHANNELS = (1, 2, 3, 4)
HMP_DEFAULT_PORT = 5025   # raw SCPI socket


def script_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- #
#  Links                                                                       #
# --------------------------------------------------------------------------- #
class SerialLink:
    def __init__(self, port: str, baud: int = 9600, timeout: float = 1.0):
        if serial is None:
            raise RuntimeError("pyserial is not installed:  pip install pyserial")
        self.s = serial.Serial(port, baud, timeout=timeout, write_timeout=timeout)
        self.name = port

    def write(self, cmd: str):
        self.s.write((cmd + "\n").encode())

    def query(self, cmd: str) -> str:
        self.s.reset_input_buffer()
        self.write(cmd)
        line = self.s.readline()
        if not line:
            raise TimeoutError(f"no answer to {cmd!r} from {self.name}")
        return line.decode(errors="ignore").strip()

    def close(self):
        self.s.close()


class SocketLink:
    def __init__(self, host: str, port: int = HMP_DEFAULT_PORT, timeout: float = 2.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""
        self.name = f"{host}:{port}"

    def write(self, cmd: str):
        self.sock.sendall((cmd + "\n").encode())

    def query(self, cmd: str) -> str:
        self.write(cmd)
        while b"\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError(f"{self.name} closed the connection")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode(errors="ignore").strip()

    def close(self):
        self.sock.close()


_VISA_RM = None
VISA_INFO = ""          # which VISA library is in use (shown in the log on the first connection)


_DLL_DIR_HANDLES = []      # os.add_dll_directory() only lasts while its handle is alive


def _keysight_dll_dirs():
    """Fallback when VISA was not opened before PyQt5: let the IVI router find the vendor VISA
    by adding the VISA folders from PATH (and the usual install folders) to the DLL search."""
    if sys.platform != "win32" or _DLL_DIR_HANDLES or not hasattr(os, "add_dll_directory"):
        return
    keys = ("ivi", "visa", "keysight", "agilent", "national instruments")
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if any(k in d.lower() for k in keys)]
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        dirs += [os.path.join(base, "Keysight", "IO Libraries Suite", "bin"),
                 os.path.join(base, "IVI Foundation", "VISA", "Win64", "Bin"),
                 os.path.join(base, "IVI Foundation", "VISA", "WinNT", "Bin")]
    for d in dict.fromkeys(dirs):
        if os.path.isdir(d):
            try:
                _DLL_DIR_HANDLES.append(os.add_dll_directory(d))
            except OSError:
                pass


def _is_pyvisa_py(rm) -> bool:
    return type(rm.visalib).__module__.startswith("pyvisa_py")


def visa_manager():
    """One VISA ResourceManager for the bench. The installed VISA (Keysight IO Libraries / NI-VISA)
    comes first: pyvisa's default lookup, then the DLLs in System32 by full path. pyvisa's default
    silently drops to pyvisa-py (pure Python) when it cannot load the installed VISA, and pyvisa-py
    cannot reach a USB instrument that uses Keysight's USB driver - so pyvisa-py is only accepted
    when nothing else loads, and the reasons are kept in VISA_INFO."""
    global _VISA_RM, VISA_INFO
    if _VISA_RM is not None:
        return _VISA_RM
    main = sys.modules.get("__main__")
    early = getattr(main, "EARLY_VISA_RM", None)            # opened by fsw_vsa_monitor before PyQt5
    if early is not None:
        _VISA_RM, VISA_INFO = early, f"VISA: {early.visalib}"
        return early
    tried, fallback = [], None
    if getattr(main, "EARLY_VISA_ERR", ""):
        tried.append(f"at start-up: {main.EARLY_VISA_ERR}")
    _keysight_dll_dirs()
    candidates = [None]
    if sys.platform == "win32":
        sysdir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        candidates += [os.path.join(sysdir, n) for n in ("visa64.dll", "visa32.dll")]
    for lib in candidates:
        try:
            rm = pyvisa.ResourceManager() if lib is None else pyvisa.ResourceManager(lib)
        except Exception as e:
            tried.append(f"{lib or 'default lookup'}: {type(e).__name__}: {e}")
            continue
        if _is_pyvisa_py(rm):
            tried.append(f"{lib or 'default lookup'}: gave pyvisa-py")
            fallback = fallback or rm
            continue
        _VISA_RM, VISA_INFO = rm, f"VISA: {rm.visalib}"
        return rm
    rm = fallback or pyvisa.ResourceManager("@py")
    _VISA_RM = rm
    VISA_INFO = ("VISA: pyvisa-py (pure Python; USB instruments with Keysight's driver are NOT reachable) - "
                 "the installed VISA could not be loaded: " + " | ".join(tried))
    return rm


class VisaLink:
    def __init__(self, resource: str, timeout_ms: int = 2000):
        if pyvisa is None:
            raise RuntimeError("pyvisa is not installed:  pip install pyvisa pyvisa-py")
        rm = visa_manager()
        self.inst = rm.open_resource(resource)
        self.inst.timeout = timeout_ms
        self.inst.read_termination = "\n"
        self.inst.write_termination = "\n"
        self.name = resource

    def write(self, cmd: str):
        self.inst.write(cmd)

    def query(self, cmd: str) -> str:
        return self.inst.query(cmd).strip()

    def close(self):
        self.inst.close()


def open_link(addr: str):
    """'COM7' / '/dev/ttyUSB0' -> serial, 'TCPIP0::...' / 'USB0::...' -> VISA,
    '192.168.1.20' or '192.168.1.20:5025' -> raw LAN socket."""
    a = addr.strip()
    if "::" in a:
        return VisaLink(a)
    if a.upper().startswith("COM") or a.startswith("/dev/"):
        return SerialLink(a)
    host, _, port = a.partition(":")
    return SocketLink(host, int(port) if port else HMP_DEFAULT_PORT)


# --------------------------------------------------------------------------- #
#  Devices                                                                     #
# --------------------------------------------------------------------------- #
class Hmp4040:
    def __init__(self, addr: str):
        self.link = open_link(addr)
        try:
            self.idn = self.link.query("*IDN?")
            if not any(k in self.idn.upper() for k in ("HMP", "HAMEG", "ROHDE")):
                raise IOError(f"{self.link.name} is not an HMP power supply (*IDN? = {self.idn!r})")
            self.link.write("SYST:REM")
        except Exception:
            self.link.close()
            raise
        self.info = f"{self.idn.split(',')[1] if ',' in self.idn else self.idn} · {self.link.name}"

    def _q(self, cmd) -> str:
        return self.link.query(cmd)

    @staticmethod
    def _bool(s: str) -> bool:
        return s.strip().upper() in ("1", "ON")

    def errors(self) -> list[str]:
        out = []
        for _ in range(10):
            e = self._q("SYST:ERR?")
            if e.lstrip("+").startswith("0"):
                break
            out.append(e)
        return out

    def read_channel(self, ch: int):
        self.link.write(f"INST:NSEL {ch}")
        return float(self._q("VOLT?")), float(self._q("CURR?")), self._bool(self._q("OUTP:SEL?"))

    def set_channel(self, ch: int, v: float, i: float, on: bool) -> list[str]:
        self.link.write(f"INST:NSEL {ch}")
        self.link.write(f"VOLT {v:.3f}")
        self.link.write(f"CURR {i:.3f}")
        self.link.write(f"OUTP:SEL {1 if on else 0}")
        return self.errors()

    def master(self) -> bool:
        return self._bool(self._q("OUTP:GEN?"))

    def set_master(self, on: bool):
        self.link.write(f"OUTP:GEN {1 if on else 0}")

    def measure(self, ch: int):
        self.link.write(f"INST:NSEL {ch}")
        return float(self._q("MEAS:VOLT?")), float(self._q("MEAS:CURR?"))

    def close(self):
        try:
            self.link.write("SYST:LOC")        # give the front panel back
        except Exception:
            pass
        self.link.close()


class DemoHmp:
    idn = "ROHDE&SCHWARZ,HMP4040,DEMO,simulated"
    info = "HMP4040 · simulated"

    def __init__(self):
        self.ch = {c: [5.0, 1.0, False] for c in HMP_CHANNELS}
        self.gen = False
        self.rng = np.random.default_rng()

    def errors(self):
        return []

    def read_channel(self, ch):
        return tuple(self.ch[ch])

    def set_channel(self, ch, v, i, on):
        self.ch[ch] = [v, i, on]
        return []

    def master(self):
        return self.gen

    def set_master(self, on):
        self.gen = on

    def measure(self, ch):
        time.sleep(0.01)
        v, ilim, on = self.ch[ch]
        if not (self.gen and on):
            return 0.0, 0.0
        i = min(ilim, v / (10.0 * ch)) * (1 + self.rng.normal(0, 0.002))
        return v * (1 + self.rng.normal(0, 0.0003)), i

    def close(self):
        pass


# --------------------------------------------------------------------------- #
#  Device discovery                                                            #
# --------------------------------------------------------------------------- #
def detect_hmp(skip_ports=()) -> dict:
    """Find the HMP4040 on a COM port (answers *IDN? with HMP/HAMEG/ROHDE).
    Ports in `skip_ports` (e.g. the MOXA RS-485 ports) are not touched."""
    found = {"hmp": None, "ports": []}
    if serial is None:
        return found
    skip = {p.upper() for p in skip_ports if p}
    ports = list(serial.tools.list_ports.comports())
    found["ports"] = [p.device for p in ports]
    for p in ports:
        if p.device.upper() in skip or "MOXA" in f"{p.description} {p.manufacturer or ''}".upper():
            continue
        try:
            with serial.Serial(p.device, 9600, timeout=0.3, write_timeout=0.3) as s:
                s.write(b"*IDN?\n")
                resp = s.readline().decode(errors="ignore").upper()
            if any(k in resp for k in ("HMP", "HAMEG", "ROHDE")):
                found["hmp"] = p.device
                break
        except Exception:
            pass
    return found


# --------------------------------------------------------------------------- #
#  Workers                                                                     #
# --------------------------------------------------------------------------- #
class _Worker(QtCore.QThread):
    """Thread that owns one instrument: runs submitted jobs, otherwise step()."""
    log = QtCore.pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.jobs: queue.Queue = queue.Queue()
        self.dev = None
        self._quit = False

    def submit(self, fn):
        self.jobs.put(fn)

    def shutdown(self):
        self._quit = True
        self.wait(5000)

    def run(self):
        while not self._quit:
            try:
                fn = self.jobs.get_nowait()
            except queue.Empty:
                fn = None
            if fn is not None:
                try:
                    fn()
                except Exception as e:
                    self.log.emit(f"{type(e).__name__}: {e}", "err")
                continue
            try:
                busy = self.step()
            except Exception as e:
                busy = False
                self.on_error(e)
            if not busy:
                time.sleep(0.01)
        self.on_quit()

    def step(self) -> bool:
        return False

    def on_error(self, e):
        self.log.emit(f"{type(e).__name__}: {e}", "err")

    def on_quit(self):
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None


class HmpCsv:
    """hmp_log_YYYY-MM-DD.csv: one row per interval with master state and all 4 channels."""
    HEADER = ["Time", "Master"] + [f"CH{c}_{k}" for c in HMP_CHANNELS
                                   for k in ("On", "Vset_V", "Iset_A", "V_V", "I_A", "P_W")]

    def __init__(self, folder: str):
        self.folder = folder
        self.f = None
        self.day = None

    def write(self, t: float, master: bool, state: dict, meas: dict):
        day = time.strftime("%Y-%m-%d", time.localtime(t))
        if day != self.day:
            self.close()
            path = os.path.join(self.folder, f"hmp_log_{day}.csv")
            new = not os.path.isfile(path)
            self.f = open(path, "a", newline="", encoding="utf-8")
            self.w = csv.writer(self.f)
            if new:
                self.w.writerow(self.HEADER)
            self.day = day
        row = [time.strftime("%H:%M:%S", time.localtime(t)), int(master)]
        for c in HMP_CHANNELS:
            vs, is_, on = state.get(c, (float("nan"), float("nan"), False))
            v, i = meas.get(c, (float("nan"), float("nan")))
            row += [int(on), f"{vs:.3f}", f"{is_:.3f}", f"{v:.4f}", f"{i:.4f}", f"{v * i:.3f}"]
        self.w.writerow(row)
        self.f.flush()

    def close(self):
        if self.f:
            self.f.close()
            self.f = None
            self.day = None


class HmpWorker(_Worker):
    telemetry = QtCore.pyqtSignal(object, bool)          # {ch: (V, I)}, master output
    state = QtCore.pyqtSignal(object)                    # {ch: (Vset, Iset, on)}
    connected = QtCore.pyqtSignal(bool, str)
    cycle = QtCore.pyqtSignal(bool, str, float, int)     # active, text, remaining s, cycle no.

    TEL_INTERVAL = 0.5

    def __init__(self):
        super().__init__()
        self._meas = {c: (0.0, 0.0) for c in HMP_CHANNELS}
        self._tel_ch = 1
        self._next_tel = 0.0
        self._cyc = None
        self._next_cyc_emit = 0.0

    # -- jobs (called in this thread) ----------------------------------------
    def connect_dev(self, demo: bool, addr: str):
        self.disconnect_dev()
        self.dev = DemoHmp() if demo else Hmp4040(addr)
        self.connected.emit(True, self.dev.info)
        self.read_state()

    def disconnect_dev(self):
        if self._cyc:
            self.stop_cycle("stopped: HMP disconnected")
        if self.dev is not None:
            self.dev.close()
            self.dev = None
            self.connected.emit(False, "")

    def read_state(self):
        if self.dev:
            self.state.emit({c: self.dev.read_channel(c) for c in HMP_CHANNELS})

    def set_channel(self, ch, v, i, on):
        errs = self.dev.set_channel(ch, v, i, on)
        for e in errs:
            self.log.emit(f"HMP4040 OUT{ch}: {e}", "err")
        self.log.emit(f"HMP4040 OUT{ch}: {v:.3f} V, {i:.3f} A, output {'selected' if on else 'off'}",
                      "warn" if errs else "ok")
        self.state.emit({ch: self.dev.read_channel(ch)})

    def set_master(self, on):
        self.dev.set_master(on)
        self.log.emit(f"HMP4040 master output {'ON' if on else 'OFF'}", "ok")
        self._next_tel = 0.0                           # refresh the display at once

    def start_cycle(self, on_s, off_s, total_s):
        now = time.monotonic()
        self._cyc = {"on": on_s, "off": off_s, "end": now + total_s, "phase": "on",
                     "phase_end": now + on_s, "n": 1}
        self.dev.set_master(True)
        self.log.emit(f"Power cycling started: {on_s:g} s ON / {off_s:g} s OFF for "
                      f"{total_s / 60:g} min", "ok")
        self._emit_cycle(now)

    def stop_cycle(self, reason="stopped by user", power_off=False):
        if self._cyc:
            n = self._cyc["n"]
            self._cyc = None
            if power_off and self.dev is not None:
                self.dev.set_master(False)
            self.log.emit(f"Power cycling {reason} after {n} cycle(s) - "
                          f"{'master output switched OFF' if power_off else 'output left as it is'}", "warn")
            self.cycle.emit(False, f"Stopped ({reason})", 0.0, n)

    # -- loop ------------------------------------------------------------------
    def _emit_cycle(self, now):
        c = self._cyc
        txt = f"Cycle {c['n']}: power {'ON' if c['phase'] == 'on' else 'OFF'}  " \
              f"({max(0.0, c['phase_end'] - now):.0f} s left in phase)"
        self.cycle.emit(True, txt, max(0.0, c["end"] - now), c["n"])
        self._next_cyc_emit = now + 0.25

    def _cycle_tick(self, now):
        c = self._cyc
        if c is None:
            return
        if now >= c["end"]:
            self.dev.set_master(False)
            self._cyc = None
            self.log.emit(f"Power cycling finished after {c['n']} cycle(s): output switched OFF", "ok")
            self.cycle.emit(False, "Finished - power OFF", 0.0, c["n"])
            self._next_tel = 0.0
            return
        if now >= c["phase_end"]:
            if c["phase"] == "on":
                self.dev.set_master(False)
                c["phase"], c["phase_end"] = "off", c["phase_end"] + c["off"]
            else:
                self.dev.set_master(True)
                c["phase"], c["phase_end"] = "on", c["phase_end"] + c["on"]
                c["n"] += 1
            self._next_tel = 0.0
            self._emit_cycle(now)
        elif now >= self._next_cyc_emit:
            self._emit_cycle(now)

    def step(self) -> bool:
        if self.dev is None:
            return False
        now = time.monotonic()
        self._cycle_tick(now)
        if now < self._next_tel:
            return False
        ch = self._tel_ch                       # one channel per step keeps cycle timing tight
        self._meas[ch] = self.dev.measure(ch)
        if ch == HMP_CHANNELS[-1]:
            self._tel_ch = HMP_CHANNELS[0]
            self.telemetry.emit(dict(self._meas), self.dev.master())
            self._next_tel = now + self.TEL_INTERVAL
        else:
            self._tel_ch = ch + 1
        return True

    def on_error(self, e):
        msg = f"HMP4040 communication error: {type(e).__name__}: {e}"
        if self._cyc:
            msg += "  -  POWER CYCLING STOPPED, check the supply!"
            self._cyc = None
            self.cycle.emit(False, "Stopped (communication error)", 0.0, 0)
        self.log.emit(msg, "err")
        try:
            self.dev.close()
        except Exception:
            pass
        self.dev = None
        self.connected.emit(False, "error")

    def on_quit(self):
        self._cyc = None
        super().on_quit()


# --------------------------------------------------------------------------- #
#  GUI                                                                         #
# --------------------------------------------------------------------------- #
def _spin(lo, hi, dec, suffix, val, step=None):
    w = QtWidgets.QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setDecimals(dec)
    w.setSuffix(suffix)
    w.setValue(val)
    if step:
        w.setSingleStep(step)
    w.setKeyboardTracking(False)
    return w



class HmpChannelBox(QtWidgets.QGroupBox):
    changed = QtCore.pyqtSignal(int, float, float, bool)

    def __init__(self, ch: int):
        super().__init__(f"OUT {ch}")
        self.ch = ch
        self._known = None
        f = QtWidgets.QFormLayout(self)
        self.on = QtWidgets.QCheckBox("Enable output")
        self.on.setToolTip("OUTP:SEL - an enabled channel is switched by the MASTER output")
        self.v = _spin(0, 32.0, 3, " V", 5.0, 0.1)
        self.i = _spin(0, 10.0, 3, " A", 1.0, 0.05)
        self.meas = QtWidgets.QLabel("—")
        self.meas.setStyleSheet("font-size:15px;font-weight:700;color:#4fd18b;")
        f.addRow(self.on)
        self.v.setPrefix("U  ")
        self.v.setToolTip("Voltage set-point")
        self.i.setPrefix("I ≤ ")
        self.i.setToolTip("Current limit")
        f.addRow(self.v)
        f.addRow(self.i)
        f.addRow(self.meas)
        self.on.toggled.connect(self._emit)
        self.v.valueChanged.connect(self._emit)      # keyboard tracking is off: fires on
        self.i.valueChanged.connect(self._emit)      # Enter / focus-out / arrow clicks

    def values(self):
        return round(self.v.value(), 3), round(self.i.value(), 3), self.on.isChecked()

    def _emit(self, *_):
        vals = self.values()
        if vals != self._known:
            self._known = vals
            self.changed.emit(self.ch, *vals)

    def set_state(self, v, i, on):
        for w in (self.v, self.i, self.on):
            w.blockSignals(True)
        self.v.setValue(v)
        self.i.setValue(i)
        self.on.setChecked(on)
        for w in (self.v, self.i, self.on):
            w.blockSignals(False)
        self._known = self.values()

    def set_meas(self, v, i):
        self.meas.setText(f"{v:.3f} V   {i:.3f} A\n{v * i:.2f} W")



class BenchTab(QtCore.QObject):
    """HMP4040 tab. Provides `settings_panel` (left column) and `page` (tab content)."""
    log = QtCore.pyqtSignal(str, str)
    _detected = QtCore.pyqtSignal(object)

    def __init__(self, parent=None, skip_ports=None):
        super().__init__(parent)
        self.skip_ports = skip_ports or (lambda: [])   # COM ports auto-detect must not touch (RS-485)
        self._hmp_ok = False
        self._hmp_master = False
        self.log_folder = script_dir()
        self.folder_listeners = []   # called after the log folder changes

        self.hmp = HmpWorker()
        self.hmp.log.connect(self.log)
        self.hmp_last_state = {}   # ch -> (Vset, Iset, on), read by the Main tab
        self.hmp_last_meas = {}    # ch -> (V, I)
        self.hmp_csv: HmpCsv | None = None
        self._hmp_next_log = 0.0
        self.hmp.telemetry.connect(self._on_telemetry)
        self.hmp.state.connect(self._on_hmp_state)
        self.hmp.connected.connect(self._on_hmp_connected)
        self.hmp.cycle.connect(self._on_cycle)
        self._detected.connect(self._on_detected)

        self.settings_panel = self._build_settings()
        self.page = self._build_page()
        self._set_hmp_connected(False)
        self._on_hmp_log_toggled(self.hmp_log_chk.isChecked())
        self.hmp.start()

    # ------------------------------------------------------------------ build
    def _build_settings(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(page)
        lv.setContentsMargins(4, 4, 4, 4)

        self.demo_chk = QtWidgets.QCheckBox("Simulated hardware (demo)")
        lv.addWidget(self.demo_chk)

        g = QtWidgets.QGroupBox("HMP4040 (USB or LAN)")
        f = QtWidgets.QFormLayout(g)
        self.hmp_addr = QtWidgets.QComboBox()
        self.hmp_addr.setEditable(True)
        self.hmp_addr.setToolTip("USB (virtual COM, 9600 Bd):  COM7\n"
                                 "LAN (raw SCPI socket):     192.168.1.20  or  192.168.1.20:5025\n"
                                 "VISA (USB-TMC / LAN):      USB0::...::INSTR,  TCPIP0::192.168.1.20::5025::SOCKET")
        self.hmp_addr.lineEdit().setPlaceholderText("COM7 / IP / VISA")
        self.hmp_conn_btn = QtWidgets.QPushButton("Connect")
        self.hmp_conn_btn.clicked.connect(self.on_hmp_connect)
        self.detect_btn = QtWidgets.QPushButton("Auto-detect (USB)")
        self.detect_btn.setToolTip("Looks for the HMP4040 on the COM ports (MOXA RS-485 ports are skipped)")
        self.detect_btn.clicked.connect(self.on_detect)
        self.hmp_status = QtWidgets.QLabel("not connected")
        self.hmp_status.setWordWrap(True)
        f.addRow("Address", self.hmp_addr)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.hmp_conn_btn)
        row.addWidget(self.detect_btn)
        f.addRow(row)
        f.addRow(self.hmp_status)
        lv.addWidget(g)

        g = QtWidgets.QGroupBox("Logging")
        f = QtWidgets.QFormLayout(g)
        self.hmp_log_chk = QtWidgets.QCheckBox("Write daily HMP4040 log")
        self.hmp_log_chk.setToolTip("hmp_log_YYYY-MM-DD.csv: master, on/off, set and measured V/I/P "
                                    "of all 4 channels, while the HMP4040 is connected")
        self.hmp_log_chk.setChecked(True)
        self.hmp_log_chk.toggled.connect(self._on_hmp_log_toggled)
        self.hmp_log_int = _spin(0.5, 3600, 1, " s", 1.0)
        self.hmp_log_int.setToolTip("Time between HMP log rows (the supply is read every 0.5 s)")
        self.folder_btn = QtWidgets.QPushButton("Folder…")
        self.folder_btn.clicked.connect(self.on_folder)
        self.folder_lbl = QtWidgets.QLabel(self.log_folder)
        self.folder_lbl.setWordWrap(True)
        self.folder_lbl.setStyleSheet("color:#8a96a3;font-size:11px;")
        f.addRow(self.hmp_log_chk)
        f.addRow("Log every", self.hmp_log_int)
        f.addRow(self.folder_btn, self.folder_lbl)
        lv.addWidget(g)
        lv.addStretch(1)
        self._fill_ports()
        return page

    def _build_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(page)
        root.setContentsMargins(4, 4, 4, 4)
        self.g_hmp = QtWidgets.QWidget()
        hrow = QtWidgets.QHBoxLayout(self.g_hmp)
        hrow.setContentsMargins(0, 0, 0, 0)
        self.hmp_boxes = {}
        for ch in HMP_CHANNELS:
            box = HmpChannelBox(ch)
            box.changed.connect(self.on_hmp_channel)
            self.hmp_boxes[ch] = box
            hrow.addWidget(box)
        side = QtWidgets.QVBoxLayout()
        self.master_btn = QtWidgets.QPushButton("MASTER OUTPUT: OFF")
        self.master_btn.setMinimumHeight(40)
        self.master_btn.clicked.connect(self.on_master)
        side.addWidget(self.master_btn)
        cg = QtWidgets.QGroupBox("Power cycling")
        cf = QtWidgets.QFormLayout(cg)
        self.cyc_on = _spin(1, 86400, 0, " s", 10)
        self.cyc_off = _spin(1, 86400, 0, " s", 5)
        self.cyc_total = _spin(0.1, 100000, 1, " min", 60)
        self.cyc_btn = QtWidgets.QPushButton("▶ Start cycling")
        self.cyc_btn.setCheckable(True)
        self.cyc_btn.toggled.connect(self.on_cycle)
        self.cyc_status = QtWidgets.QLabel("Idle")
        self.cyc_left = QtWidgets.QLabel("Time left: --:--:--")
        self.cyc_left.setStyleSheet("color:#58a6ff;font-weight:600;")
        cf.addRow("ON", self.cyc_on)
        cf.addRow("OFF", self.cyc_off)
        cf.addRow("Duration", self.cyc_total)
        cf.addRow(self.cyc_btn)
        cf.addRow(self.cyc_status)
        cf.addRow(self.cyc_left)
        side.addWidget(cg)
        hrow.addLayout(side)
        root.addWidget(self.g_hmp)

        # --- measured power of the 4 outputs
        bar = QtWidgets.QHBoxLayout()
        self.follow = QtWidgets.QCheckBox("Follow")
        self.follow.setChecked(True)
        self.window_s = QtWidgets.QSpinBox()
        self.window_s.setRange(10, 86400)
        self.window_s.setValue(300)
        self.window_s.setPrefix("Window ")
        self.window_s.setSuffix(" s")
        self.plot_what = QtWidgets.QComboBox()
        self.plot_what.addItems(["Current, A", "Voltage, V", "Power, W"])
        self.plot_what.currentIndexChanged.connect(self._redraw)
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
        colors = ["#f0b03e", "#79c0ff", "#4fd18b", "#d2a8ff"]
        self.curves = {ch: self.plot.plot([], [], pen=pg.mkPen(colors[k], width=1.5), name=f"OUT {ch}")
                       for k, ch in enumerate(HMP_CHANNELS)}
        self.hist = {ch: ([], [], []) for ch in HMP_CHANNELS}     # t, V, I
        root.addWidget(self.plot, 1)
        self.timer = QtCore.QTimer(self, interval=500, timeout=self._redraw)
        self.timer.start()
        return page

    # ---------------------------------------------------------------- helpers
    def _fill_ports(self):
        ports = []
        if serial is not None:
            try:
                ports = [p.device for p in serial.tools.list_ports.comports()]
            except Exception:
                pass
        cur = self.hmp_addr.currentText()
        self.hmp_addr.blockSignals(True)
        self.hmp_addr.clear()
        self.hmp_addr.addItems(ports)
        self.hmp_addr.setEditText(cur)
        self.hmp_addr.blockSignals(False)

    def _set_hmp_connected(self, on: bool):
        self._hmp_ok = on
        self.hmp_conn_btn.setText("Disconnect" if on else "Connect")
        self.hmp_addr.setEnabled(not on)
        self.detect_btn.setEnabled(not on)
        self.g_hmp.setEnabled(on)
        if not on:
            self._show_master(False)
            for b in self.hmp_boxes.values():
                b.meas.setText("—")
            if self.cyc_btn.isChecked():
                self.cyc_btn.blockSignals(True)
                self.cyc_btn.setChecked(False)
                self.cyc_btn.setText("▶ Start cycling")
                self.cyc_btn.blockSignals(False)

    def _show_master(self, on: bool):
        self._hmp_master = on
        self.master_btn.setText(f"MASTER OUTPUT: {'ON' if on else 'OFF'}")
        self.master_btn.setStyleSheet("background:#1f6f43;border-color:#2ea043;font-weight:700;"
                                      if on else "font-weight:700;")

    def _on_hmp_log_toggled(self, on):
        if self.hmp_csv:
            self.hmp_csv.close()
        self.hmp_csv = HmpCsv(self.log_folder) if on else None
        self._hmp_next_log = 0.0

    # ---------------------------------------------------------------- actions
    def on_detect(self):
        self.detect_btn.setEnabled(False)
        self.detect_btn.setText("Detecting…")
        skip = list(self.skip_ports())
        threading.Thread(target=lambda: self._detected.emit(detect_hmp(skip)), daemon=True).start()

    def _on_detected(self, found: dict):
        self.detect_btn.setText("Auto-detect (USB)")
        self.detect_btn.setEnabled(not self._hmp_ok)
        self._fill_ports()
        if found["hmp"] and self.hmp_addr.isEnabled():
            self.hmp_addr.setEditText(found["hmp"])
        self.log.emit(f"Detected: HMP4040 {found['hmp'] or '— (enter IP for LAN)'}",
                      "ok" if found["hmp"] else "warn")

    def on_hmp_connect(self):
        if self._hmp_ok:
            self.hmp.submit(self.hmp.disconnect_dev)
            return
        demo = self.demo_chk.isChecked()
        addr = self.hmp_addr.currentText().strip()
        if not demo and not addr:
            self.log.emit("Enter the HMP4040 address: COM port, IP address or VISA string", "warn")
            return
        self.hmp_status.setText("connecting…")
        self.hmp_conn_btn.setEnabled(False)

        def job():
            try:
                self.hmp.connect_dev(demo, addr)
            except Exception as e:
                self.hmp.connected.emit(False, f"{type(e).__name__}: {e}")
        self.hmp.submit(job)

    def on_folder(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self.page, "CSV log folder", self.log_folder)
        if d:
            self.log_folder = d
            self.folder_lbl.setText(d)
            self._on_hmp_log_toggled(self.hmp_log_chk.isChecked())
            for fn in self.folder_listeners:
                fn()

    def on_hmp_channel(self, ch, v, i, on):
        self.hmp.submit(lambda: self.hmp.set_channel(ch, v, i, on))

    def on_master(self):
        on = not self._hmp_master
        self.hmp.submit(lambda: self.hmp.set_master(on))

    def on_cycle(self, on):
        self.cyc_btn.setText("■ Stop cycling" if on else "▶ Start cycling")
        if on:
            a, b, t = self.cyc_on.value(), self.cyc_off.value(), self.cyc_total.value() * 60
            self.hmp.submit(lambda: self.hmp.start_cycle(a, b, t))
        else:
            self.hmp.submit(self.hmp.stop_cycle)

    def on_clear(self):
        for lists in self.hist.values():
            for x in lists:
                x.clear()
        self.follow.setChecked(True)
        self._redraw()

    # --------------------------------------------------------------- callbacks
    def _on_hmp_connected(self, ok, info):
        self.hmp_conn_btn.setEnabled(True)
        self._set_hmp_connected(ok)
        if ok:
            self.hmp_status.setText(f"✓ {info}")
            self.log.emit(f"HMP4040 connected: {info}", "ok")
        else:
            self.hmp_status.setText(info or "not connected")
            if info and info != "error":
                self.log.emit(f"HMP4040: {info}", "err")

    def _on_hmp_state(self, st: dict):
        self.hmp_last_state.update(st)
        for ch, (v, i, on) in st.items():
            self.hmp_boxes[ch].set_state(v, i, on)

    def _on_telemetry(self, meas: dict, master: bool):
        self.hmp_last_meas.update(meas)
        now = time.time()
        if self.hmp_csv and now >= self._hmp_next_log:
            self._hmp_next_log = now + self.hmp_log_int.value()
            try:
                self.hmp_csv.write(now, master, self.hmp_last_state, self.hmp_last_meas)
            except OSError as e:
                self.log.emit(f"HMP log stopped: {e}", "err")
                self.hmp_log_chk.setChecked(False)
        for ch, (v, i) in meas.items():
            self.hmp_boxes[ch].set_meas(v, i)
            ts, vs, is_ = self.hist[ch]
            ts.append(now)
            vs.append(v)
            is_.append(i)
            if len(ts) > 200_000:
                for x in (ts, vs, is_):
                    del x[:50_000]
        self._show_master(master)

    def _redraw(self, *_):
        k = self.plot_what.currentIndex()
        self.plot.setLabel("left", ("Current", "Voltage", "Power")[k], units=("A", "V", "W")[k])
        for ch, (ts, vs, is_) in self.hist.items():
            if k == 0:
                y = is_
            elif k == 1:
                y = vs
            else:
                y = [a * b for a, b in zip(vs, is_)]
            self.curves[ch].setData(np.asarray(ts, float), np.asarray(y, float))
        if self.follow.isChecked():
            now = time.time()
            self.plot.setXRange(now - self.window_s.value(), now, padding=0)

    def _on_cycle(self, active, text, remaining, n):
        self.cyc_status.setText(text)
        r = int(remaining)
        self.cyc_left.setText(f"Time left: {r // 3600:02d}:{r % 3600 // 60:02d}:{r % 60:02d}")
        if not active and self.cyc_btn.isChecked():
            self.cyc_btn.blockSignals(True)
            self.cyc_btn.setChecked(False)
            self.cyc_btn.setText("▶ Start cycling")
            self.cyc_btn.blockSignals(False)

    # ---------------------------------------------------------------- shutdown
    def confirm_close(self, parent) -> bool:
        if not self.cyc_btn.isChecked():
            return True
        r = QtWidgets.QMessageBox.question(
            parent, "Power cycling is running",
            "Power cycling is still running.\n\nStop it and switch the HMP4040 master output OFF?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel)
        if r != QtWidgets.QMessageBox.Yes:
            return False
        self.hmp.submit(lambda: self.hmp.stop_cycle("stopped: application closed", power_off=True))
        return True

    def shutdown(self):
        self.timer.stop()
        deadline = time.monotonic() + 2.0
        while not self.hmp.jobs.empty() and time.monotonic() < deadline:
            time.sleep(0.02)                 # let a pending "power off" job run
        self.hmp.shutdown()
        if self.hmp_csv:
            self.hmp_csv.close()


# --------------------------------------------------------------------------- #
#  Standalone window                                                           #
# --------------------------------------------------------------------------- #
STYLE = """
    QWidget{background:#0f1216;color:#c9d1d9;}
    QGroupBox{border:1px solid #2a3038;border-radius:6px;margin-top:14px;padding-top:6px;font-weight:600;}
    QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 4px;color:#8a96a3;}
    QLineEdit,QSpinBox,QDoubleSpinBox,QComboBox,QPlainTextEdit,QTableWidget{background:#161b22;
        border:1px solid #30363d;border-radius:4px;padding:3px;}
    QHeaderView::section{background:#161b22;color:#8a96a3;border:0;padding:3px;}
    QPushButton{background:#21262d;border:1px solid #30363d;border-radius:5px;padding:5px 10px;}
    QPushButton:hover{background:#2d333b;}
    QPushButton:checked{background:#1f6f43;border-color:#2ea043;}
    QPushButton:disabled,QLineEdit:disabled,QSpinBox:disabled,QDoubleSpinBox:disabled,
    QComboBox:disabled,QCheckBox:disabled{color:#4d5560;}
"""


class BenchWindow(QtWidgets.QMainWindow):
    def __init__(self, demo=False):
        super().__init__()
        self.setWindowTitle("HMP4040 control")
        self.resize(1500, 900)
        pg.setConfigOptions(antialias=False, background="#0f1216", foreground="#c9d1d9")
        self.bench = BenchTab(self)
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(self.bench.settings_panel)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(360)
        split.addWidget(scroll)
        split.addWidget(self.bench.page)
        split.setSizes([370, 1130])
        self.logbox = QtWidgets.QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumHeight(130)
        central = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(central)
        v.addWidget(split, 1)
        v.addWidget(self.logbox)
        self.setCentralWidget(central)
        self.setStyleSheet(STYLE)
        self.bench.log.connect(self.logmsg)
        if demo:
            self.bench.demo_chk.setChecked(True)

    def logmsg(self, text, level="info"):
        color = {"info": "#c9d1d9", "ok": "#4fd18b", "warn": "#f0b03e", "err": "#ff6b6b"}[level]
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        self.logbox.appendHtml(f'<span style="color:#6e7681">{ts}</span> <span style="color:{color}">{text}</span>')

    def closeEvent(self, ev):
        if not self.bench.confirm_close(self):
            ev.ignore()
            return
        self.bench.shutdown()
        ev.accept()


def main():
    import argparse
    ap = argparse.ArgumentParser(description="HMP4040 control")
    ap.add_argument("--demo", action="store_true", help="simulated hardware")
    args = ap.parse_args()
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    w = BenchWindow(args.demo)
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
