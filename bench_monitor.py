#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bench_monitor.py
================
176-channel voltage monitor + R&S HMP4040 power-supply control.

Used as the "Bench" tab of fsw_vsa_monitor.py (keep both files in the same
folder), or standalone:

    python bench_monitor.py            # real hardware
    python bench_monitor.py --demo     # simulated hardware

Hardware
--------
* Arduino (sketch_sep24a.ino) driving 11 x CD74HC4067 multiplexers.
  Protocol (115200 Bd): "SET:<n>\\n" -> "READY:<n>"  (n = 0...175)
      0...143   chips 0-8   0...5 V            V = reading
      144...159 chip 9      -5...0 V           V = -reading
      160...175 chip 10     ~16 V via divider  V = reading x 133/33
* Agilent/Keysight 34405A multimeter over USB (VISA):
  *RST, CONF:VOLT:DC 10, SENS:VOLT:DC:NPLC 1, then READ? per channel.
* R&S HMP4040 over USB (virtual COM port, 9600 Bd), USB-TMC (VISA) or LAN
  (raw SCPI socket, port 5025). SCPI as used by pymeasure's HMP4040 driver:
  INST:NSEL n, VOLT, CURR, OUTP:SEL, OUTP:GEN, MEAS:VOLT?, MEAS:CURR?, SYST:REM/LOC.

Files
-----
* channels.txt (next to this script): optional channel names, lines "17=VCC_CORE".
  Channels named GND are skipped by the range scan.
  Names edited in the table are saved back to it.
* voltage_log_YYYY-MM-DD.csv: one row per reading, same columns as before
  (time, channel, name, voltage), so old logs can still be loaded.

Install:  pip install PyQt5 pyqtgraph numpy pyvisa pyvisa-py pyserial
"""

from __future__ import annotations

import csv
import datetime as _dt
import os
import queue
import re
import socket
import sys
import threading
import time
from collections import deque

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
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


# --------------------------------------------------------------------------- #
#  Channel map (must match the Arduino sketch)                                 #
# --------------------------------------------------------------------------- #
N_CHANNELS = 176
R_TOP, R_BOTTOM = 100.0, 33.0   # kOhm: 16 V -> R_TOP -> mux input -> R_BOTTOM -> GND
DIVIDER_RATIO = (R_TOP + R_BOTTOM) / R_BOTTOM   # = 4.03
GROUPS = [  # first, last, label
    (0, 143, "0…5 V"),
    (144, 159, "−5…0 V"),
    (160, 175, "16 V (÷)"),
]
HIST_LEN = 20000          # points kept per channel for the plot
HMP_CHANNELS = (1, 2, 3, 4)
HMP_DEFAULT_PORT = 5025   # raw SCPI socket


def group_of(ch: int) -> int:
    for k, (a, b, _) in enumerate(GROUPS):
        if a <= ch <= b:
            return k
    raise ValueError(ch)


def convert(ch: int, raw: float) -> float:
    """Multimeter reading -> real channel voltage.
    Mux 10 (ch 144-159): inverting op-amp, -3 V in gives +3 V at the mux.
    Mux 11 (ch 160-175): 100k/33k divider, Vin = Vmux * (100+33)/33."""
    g = group_of(ch)
    if g == 1:
        return -raw
    if g == 2:
        return raw * DIVIDER_RATIO
    return raw


def script_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_channel_names(path: str) -> dict[int, str]:
    names = {ch: f"Ch_{ch}" for ch in range(N_CHANNELS)}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    if k.strip().isdigit() and int(k) < N_CHANNELS:
                        names[int(k)] = v.strip()
    except FileNotFoundError:
        pass
    return names


SKIP_NAMES = {"GND"}      # channels with these names (any case) are left out of range scans


def is_skipped(name: str) -> bool:
    return name.strip().upper() in SKIP_NAMES


def save_channel_names(path: str, names: dict[int, str]):
    with open(path, "w", encoding="utf-8") as f:
        for ch in range(N_CHANNELS):
            f.write(f"{ch}={names[ch]}\n")


# --------------------------------------------------------------------------- #
#  Transports for the HMP4040 (COM port / LAN socket / VISA)                   #
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


class VisaLink:
    def __init__(self, resource: str, timeout_ms: int = 2000):
        if pyvisa is None:
            raise RuntimeError("pyvisa is not installed:  pip install pyvisa pyvisa-py")
        try:
            rm = pyvisa.ResourceManager()
        except Exception:
            rm = pyvisa.ResourceManager("@py")
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
class MuxScanner:
    """Arduino multiplexer + 34405A multimeter."""

    def __init__(self, arduino_port: str, dmm_resource: str):
        if serial is None:
            raise RuntimeError("pyserial is not installed:  pip install pyserial")
        if pyvisa is None:
            raise RuntimeError("pyvisa is not installed:  pip install pyvisa pyvisa-py")
        self.t_switch = self.t_read = 0.0      # accumulated per scan, for the timing readout
        self.ard = serial.Serial(arduino_port, 115200, timeout=1.0, write_timeout=1.0)
        try:
            time.sleep(2.0)                    # most Arduinos reboot when the port opens
            self.ard.reset_input_buffer()
            for attempt in range(3):           # first command may be eaten by the bootloader
                try:
                    self.select(0)
                    break
                except IOError:
                    if attempt == 2:
                        raise
                    time.sleep(0.5)
                    self.ard.reset_input_buffer()
            try:
                rm = pyvisa.ResourceManager()
            except Exception:
                rm = pyvisa.ResourceManager("@py")
            self.dmm = rm.open_resource(dmm_resource)
            self.dmm.timeout = 3000
            self.dmm_idn = self.dmm.query("*IDN?").strip()
            for cmd in ("*RST", "CONF:VOLT:DC 10", "SENS:VOLT:DC:NPLC 1"):
                self.dmm.write(cmd)
        except Exception:
            self.ard.close()
            raise
        self.info = f"Arduino {arduino_port} · {self.dmm_idn.split(',')[1] if ',' in self.dmm_idn else self.dmm_idn}"

    def select(self, ch: int):
        self.ard.write(f"SET:{ch}\n".encode())
        resp = self.ard.readline().decode(errors="ignore").strip()
        if resp != f"READY:{ch}":
            raise IOError(f"Arduino did not confirm channel {ch} (answer: {resp!r})")

    def measure(self, ch: int, settle_s: float = 0.0) -> float:
        t0 = time.perf_counter()
        self.select(ch)
        if settle_s > 0:
            time.sleep(settle_s)
        t1 = time.perf_counter()
        v = float(self.dmm.query("READ?"))
        self.t_switch += t1 - t0
        self.t_read += time.perf_counter() - t1
        return v

    def close(self):
        for dev in (getattr(self, "ard", None), getattr(self, "dmm", None)):
            try:
                dev.close()
            except Exception:
                pass


class DemoScanner:
    info = "simulated Arduino + 34405A"

    def __init__(self):
        self.rng = np.random.default_rng()
        self.base = np.array([(1.8, 2.5, 3.3, 5.0)[ch % 4] for ch in range(N_CHANNELS)])

    def measure(self, ch: int, settle_s: float = 0.0) -> float:
        time.sleep(0.015 + settle_s)
        g = group_of(ch)
        if g == 1:
            return 5.0 + self.rng.normal(0, 0.01)                   # displayed as -5 V
        if g == 2:
            return 16.0 / DIVIDER_RATIO + self.rng.normal(0, 0.005)  # displayed as ~16 V
        return self.base[ch] + self.rng.normal(0, 0.004)

    def close(self):
        pass


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
ARDUINO_HINTS = ("ARDUINO", "CH340", "CH341", "FT232", "FTDI", "CP210", "USB-SERIAL", "USB SERIAL")


def detect_devices(skip_ports=()) -> dict:
    """Find the 34405A (VISA, Agilent vendor id 0x0957), the HMP4040 (COM port
    answering *IDN? with HMP/HAMEG/ROHDE) and the Arduino (remaining COM port,
    preferring typical USB-serial chips). Ports in `skip_ports` are not touched."""
    found = {"dmm": None, "hmp": None, "arduino": None, "ports": [], "visa": []}
    if pyvisa is not None:
        try:
            try:
                rm = pyvisa.ResourceManager()
            except Exception:
                rm = pyvisa.ResourceManager("@py")
            found["visa"] = [r for r in rm.list_resources() if r.upper().startswith("USB")]
            for r in found["visa"]:
                if "0X0957" in r.upper():
                    found["dmm"] = r
                    break
        except Exception:
            pass
    if serial is None:
        return found
    skip = {p.upper() for p in skip_ports if p}
    ports = list(serial.tools.list_ports.comports())
    found["ports"] = [p.device for p in ports]
    for p in ports:
        if p.device.upper() in skip:
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
    rest = [p for p in ports if p.device.upper() not in skip and p.device != found["hmp"]]
    for p in rest:
        if any(h in f"{p.description} {p.manufacturer or ''}".upper() for h in ARDUINO_HINTS):
            found["arduino"] = p.device
            break
    if found["arduino"] is None and rest:
        found["arduino"] = rest[0].device
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


class DailyCsv:
    """voltage_log_YYYY-MM-DD.csv, same columns as the original program."""
    HEADER = ["Время", "Номер_канала", "Название_канала", "Напряжение_В"]

    def __init__(self, folder: str):
        self.folder = folder
        self.f = None
        self.day = None
        self.last_flush = 0.0

    def write(self, t: float, ch: int, name: str, v: float):
        day = time.strftime("%Y-%m-%d", time.localtime(t))
        if day != self.day:
            self.close()
            path = os.path.join(self.folder, f"voltage_log_{day}.csv")
            new = not os.path.isfile(path)
            self.f = open(path, "a", newline="", encoding="utf-8")
            self.w = csv.writer(self.f)
            if new:
                self.w.writerow(self.HEADER)
            self.day = day
        self.w.writerow([time.strftime("%H:%M:%S", time.localtime(t)), ch, name, f"{v:.6f}"])
        if t - self.last_flush > 1.0:
            self.f.flush()
            self.last_flush = t

    def close(self):
        if self.f:
            self.f.close()
            self.f = None
            self.day = None


class ScanWorker(_Worker):
    sample = QtCore.pyqtSignal(int, float, float)        # channel, time, voltage
    scan_done = QtCore.pyqtSignal(float, int)            # seconds, channels
    timing = QtCore.pyqtSignal(float, float)             # s per channel: Arduino switch, DMM read
    connected = QtCore.pyqtSignal(bool, str)
    stopped = QtCore.pyqtSignal(str)                     # reason ("" = by user)

    def __init__(self):
        super().__init__()
        self.running = False
        self.channels = list(range(N_CHANNELS))
        self.settle_s = 0.0
        self.names = {ch: f"Ch_{ch}" for ch in range(N_CHANNELS)}
        self.csv: DailyCsv | None = None
        self._idx = 0
        self._scan_t0 = None
        self._unflushed = False

    def connect_dev(self, demo: bool, arduino: str, dmm: str):
        self.disconnect_dev()
        self.dev = DemoScanner() if demo else MuxScanner(arduino, dmm)
        self.connected.emit(True, self.dev.info)

    def disconnect_dev(self):
        self.running = False
        if self.dev is not None:
            self.dev.close()
            self.dev = None
            self.connected.emit(False, "")

    def set_channels(self, chans: list[int]):
        self.channels = chans
        self._idx = 0
        self._scan_t0 = None

    def step(self) -> bool:
        if self.dev is None or not self.running or not self.channels:
            if self.csv and self.csv.f and not self.csv.f.closed and self._unflushed:
                self.csv.f.flush()
                self._unflushed = False
            return False
        if self._idx >= len(self.channels):
            self._idx = 0
        if self._idx == 0:
            if self._scan_t0 is not None and len(self.channels) > 1:
                self.scan_done.emit(time.perf_counter() - self._scan_t0, len(self.channels))
                if hasattr(self.dev, "t_switch"):
                    n = len(self.channels)
                    self.timing.emit(self.dev.t_switch / n, self.dev.t_read / n)
                    self.dev.t_switch = self.dev.t_read = 0.0
            self._scan_t0 = time.perf_counter()
        ch = self.channels[self._idx]
        self._idx += 1
        raw = self.dev.measure(ch, self.settle_s)
        v = convert(ch, raw)
        t = time.time()
        if self.csv:
            self.csv.write(t, ch, self.names.get(ch, str(ch)), v)
            self._unflushed = True
        self.sample.emit(ch, t, v)
        return True

    def on_error(self, e):
        # Never fall back to fake data: stop and say why.
        self.running = False
        self.stopped.emit(f"{type(e).__name__}: {e}")

    def on_quit(self):
        if self.csv:
            self.csv.close()
        super().on_quit()


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


def _ch_color(ch: int) -> QtGui.QColor:
    return pg.intColor(ch, hues=22, values=8, minValue=140, maxValue=255)


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
    """Provides `settings_panel` (left column) and `page` (tab content)."""
    log = QtCore.pyqtSignal(str, str)
    _detected = QtCore.pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.names_file = os.path.join(script_dir(), "channels.txt")
        self.names = load_channel_names(self.names_file)
        self.hist = {ch: (deque(maxlen=HIST_LEN), deque(maxlen=HIST_LEN)) for ch in range(N_CHANNELS)}
        self.last = np.full(N_CHANNELS, np.nan)
        self.vmin = np.full(N_CHANNELS, np.inf)
        self.vmax = np.full(N_CHANNELS, -np.inf)
        self._dirty_rows: set[int] = set()
        self._dirty_curves: set[int] = set()
        self._current = None
        self._scan_ok = False
        self._hmp_ok = False
        self._hmp_master = False
        self.log_folder = script_dir()

        self.scan = ScanWorker()
        self.hmp = HmpWorker()
        for w in (self.scan, self.hmp):
            w.log.connect(self.log)
        self.scan.sample.connect(self._on_sample)
        self.scan.scan_done.connect(self._on_scan_done)
        self.scan.timing.connect(self._on_scan_timing)
        self.scan.connected.connect(self._on_scan_connected)
        self.scan.stopped.connect(self._on_scan_stopped)
        self.hmp.telemetry.connect(self._on_telemetry)
        self.hmp.state.connect(self._on_hmp_state)
        self.hmp.connected.connect(self._on_hmp_connected)
        self.hmp.cycle.connect(self._on_cycle)
        self._detected.connect(self._on_detected)
        self.scan.names = dict(self.names)

        self.settings_panel = self._build_settings()
        self.page = self._build_page()
        self._set_scan_connected(False)
        self._set_hmp_connected(False)
        self._on_log_toggled(self.log_chk.isChecked())

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(250)
        self.scan.start()
        self.hmp.start()

    # ------------------------------------------------------------------ build
    def _build_settings(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(page)
        lv.setContentsMargins(4, 4, 4, 4)

        self.demo_chk = QtWidgets.QCheckBox("Simulated hardware (demo)")
        self.detect_btn = QtWidgets.QPushButton("Auto-detect devices")
        self.detect_btn.clicked.connect(self.on_detect)
        lv.addWidget(self.demo_chk)
        lv.addWidget(self.detect_btn)

        g = QtWidgets.QGroupBox("Multiplexer && multimeter")
        f = QtWidgets.QFormLayout(g)
        self.ard_port = QtWidgets.QComboBox()
        self.ard_port.setEditable(True)
        self.ard_port.setToolTip("COM port of the Arduino (115200 Bd)")
        self.dmm_res = QtWidgets.QComboBox()
        self.dmm_res.setEditable(True)
        self.dmm_res.setToolTip("VISA resource of the 34405A, e.g. USB0::0x0957::0x0618::MY5327xxxx::INSTR")
        self.scan_conn_btn = QtWidgets.QPushButton("Connect")
        self.scan_conn_btn.clicked.connect(self.on_scan_connect)
        self.scan_status = QtWidgets.QLabel("not connected")
        self.scan_status.setWordWrap(True)
        f.addRow("Arduino", self.ard_port)
        f.addRow("Multimeter", self.dmm_res)
        f.addRow(self.scan_conn_btn)
        f.addRow(self.scan_status)
        lv.addWidget(g)

        self.g_scan = g = QtWidgets.QGroupBox("Scan")
        f = QtWidgets.QFormLayout(g)
        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(["Auto scan (channel range)", "Manual (one channel)"])
        self.first_ch = QtWidgets.QSpinBox()
        self.last_ch = QtWidgets.QSpinBox()
        self.manual_ch = QtWidgets.QSpinBox()
        for w in (self.first_ch, self.last_ch, self.manual_ch):
            w.setRange(0, N_CHANNELS - 1)
            w.setKeyboardTracking(False)
        self.last_ch.setValue(N_CHANNELS - 1)
        rng = QtWidgets.QHBoxLayout()
        rng.addWidget(self.first_ch, 1)
        rng.addWidget(QtWidgets.QLabel("…"))
        rng.addWidget(self.last_ch, 1)
        self.settle = QtWidgets.QSpinBox()
        self.settle.setRange(0, 5000)
        self.settle.setSuffix(" ms")
        self.settle.setToolTip("Extra wait after switching, on top of the 1 ms in the Arduino sketch")
        self.run_btn = QtWidgets.QPushButton("▶ Start scan")
        self.run_btn.setCheckable(True)
        self.run_btn.toggled.connect(self.on_run)
        self.scan_info = QtWidgets.QLabel("—")
        self.scan_info.setStyleSheet("color:#8a96a3;")
        for w in (self.mode,):
            w.currentIndexChanged.connect(self._push_channels)
        for w in (self.first_ch, self.last_ch, self.manual_ch):
            w.valueChanged.connect(self._push_channels)
        self.settle.valueChanged.connect(lambda v: setattr(self.scan, "settle_s", v / 1000))
        f.addRow("Mode", self.mode)
        f.addRow("Channels", rng)
        self.skip_lbl = QtWidgets.QLabel("")
        self.skip_lbl.setStyleSheet("color:#8a96a3;")
        self.skip_lbl.setToolTip("Channels named GND in channels.txt are not polled in range mode")
        f.addRow("", self.skip_lbl)
        f.addRow("Manual ch.", self.manual_ch)
        f.addRow("Settle delay", self.settle)
        f.addRow(self.run_btn)
        f.addRow(self.scan_info)
        lv.addWidget(g)

        g = QtWidgets.QGroupBox("Logging")
        f = QtWidgets.QFormLayout(g)
        self.log_chk = QtWidgets.QCheckBox("Write daily CSV log")
        self.log_chk.setToolTip("voltage_log_YYYY-MM-DD.csv, one row per reading")
        self.log_chk.setChecked(True)
        self.log_chk.toggled.connect(self._on_log_toggled)
        self.folder_btn = QtWidgets.QPushButton("Folder…")
        self.folder_btn.clicked.connect(self.on_folder)
        self.folder_lbl = QtWidgets.QLabel(self.log_folder)
        self.folder_lbl.setWordWrap(True)
        self.folder_lbl.setStyleSheet("color:#8a96a3;font-size:11px;")
        f.addRow(self.log_chk)
        f.addRow(self.folder_btn, self.folder_lbl)
        load_btn = QtWidgets.QPushButton("Load CSV…")
        load_btn.setToolTip("Show an old voltage_log_*.csv in the plot")
        load_btn.clicked.connect(self.on_load_csv)
        clear_btn = QtWidgets.QPushButton("Clear plot")
        clear_btn.clicked.connect(self.on_clear)
        f.addRow(load_btn, clear_btn)
        lv.addWidget(g)

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
        self.hmp_status = QtWidgets.QLabel("not connected")
        self.hmp_status.setWordWrap(True)
        f.addRow("Address", self.hmp_addr)
        f.addRow(self.hmp_conn_btn)
        f.addRow(self.hmp_status)
        lv.addWidget(g)
        lv.addStretch(1)
        self._fill_ports()
        return page

    def _build_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(page)
        root.setContentsMargins(4, 4, 4, 4)

        # --- HMP4040 row
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

        # --- current reading
        self.cur_lbl = QtWidgets.QLabel("Channel —")
        self.cur_lbl.setStyleSheet("font-size:20px;font-weight:700;color:#ffc400;")
        root.addWidget(self.cur_lbl)

        # --- plot + table
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        bar = QtWidgets.QHBoxLayout()
        self.show_mode = QtWidgets.QComboBox()
        self.show_mode.addItems(["All channels", "Selected rows"])
        self.show_mode.setToolTip("Which channels are drawn in the plot")
        self.show_mode.currentIndexChanged.connect(self._update_visibility)
        self.follow = QtWidgets.QCheckBox("Follow")
        self.follow.setChecked(True)
        self.window_s = QtWidgets.QSpinBox()
        self.window_s.setRange(10, 86400)
        self.window_s.setValue(60)
        self.window_s.setPrefix("Window ")
        self.window_s.setSuffix(" s")
        for w in (self.show_mode, self.follow, self.window_s):
            bar.addWidget(w)
        bar.addStretch(1)
        lv.addLayout(bar)
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("left", "Voltage", units="V")
        self.plot.getPlotItem().setMenuEnabled(False)
        self.plot.getPlotItem().setAutoVisible(y=True)
        self.plot.getViewBox().sigRangeChangedManually.connect(lambda *_: self.follow.setChecked(False))
        self.curves = {}
        for ch in range(N_CHANNELS):
            c = self.plot.plot([], [], pen=pg.mkPen(_ch_color(ch), width=1))
            c.setClipToView(True)
            c.setDownsampling(auto=True, method="peak")
            self.curves[ch] = c
        lv.addWidget(self.plot, 1)
        split.addWidget(left)

        self.table = QtWidgets.QTableWidget(N_CHANNELS, 6)
        self.table.setHorizontalHeaderLabels(["Ch", "Name", "Range", "Voltage", "Min", "Max"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.DoubleClicked |
                                   QtWidgets.QAbstractItemView.EditKeyPressed)
        self.table.setToolTip("Select rows to plot them ('Plot: selected').\n"
                              "Double-click a name to rename (saved to channels.txt).\n"
                              "Double-click any other cell to measure that channel manually.")
        mono = QtGui.QFont("Consolas")
        mono.setStyleHint(QtGui.QFont.Monospace)
        ro = QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled
        for ch in range(N_CHANNELS):
            cells = [str(ch), self.names[ch], GROUPS[group_of(ch)][2], "—", "—", "—"]
            for col, text in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(text)
                if col != 1:
                    it.setFlags(ro)
                if col >= 3:
                    it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                    it.setFont(mono)
                self.table.setItem(ch, col, it)
            self.table.item(ch, 0).setForeground(QtGui.QBrush(_ch_color(ch)))
            self._style_row(ch)
        hdr = self.table.horizontalHeader()
        for col, width in ((0, 40), (2, 72), (3, 74), (4, 74), (5, 74)):
            self.table.setColumnWidth(col, width)
        hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.table.setMinimumWidth(320)
        self.table.itemChanged.connect(self._on_name_edited)
        self.table.itemSelectionChanged.connect(self._update_visibility)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        split.addWidget(self.table)
        split.setSizes([760, 520])
        root.addWidget(split, 1)
        return page

    # ---------------------------------------------------------------- helpers
    def _fill_ports(self):
        ports = []
        if serial is not None:
            try:
                ports = [p.device for p in serial.tools.list_ports.comports()]
            except Exception:
                pass
        for combo in (self.ard_port, self.hmp_addr):
            cur = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(ports)
            combo.setEditText(cur)
            combo.blockSignals(False)

    def _set_scan_connected(self, on: bool):
        self._scan_ok = on
        self.scan_conn_btn.setText("Disconnect" if on else "Connect")
        self.g_scan.setEnabled(on)
        self.ard_port.setEnabled(not on)
        self.dmm_res.setEnabled(not on)
        if not on and self.run_btn.isChecked():
            self.run_btn.setChecked(False)

    def _set_hmp_connected(self, on: bool):
        self._hmp_ok = on
        self.hmp_conn_btn.setText("Disconnect" if on else "Connect")
        self.hmp_addr.setEnabled(not on)
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

    def _push_channels(self, *_):
        if self.mode.currentIndex() == 1:
            chans = [self.manual_ch.value()]
        else:
            a, b = sorted((self.first_ch.value(), self.last_ch.value()))
            chans = [ch for ch in range(a, b + 1) if not is_skipped(self.names[ch])]
            skipped = (b - a + 1) - len(chans)
            self.skip_lbl.setText(f"{skipped} channel(s) named GND skipped" if skipped else "")
        self.scan.submit(lambda: self.scan.set_channels(chans))

    def _on_log_toggled(self, on):
        folder = self.log_folder
        self.scan.submit(lambda: setattr(self.scan, "csv", DailyCsv(folder) if on else None))

    # ---------------------------------------------------------------- actions
    def on_detect(self):
        self.detect_btn.setEnabled(False)
        self.detect_btn.setText("Detecting…")
        skip = []
        if self._scan_ok:
            skip.append(self.ard_port.currentText())
        if self._hmp_ok:
            skip.append(self.hmp_addr.currentText())
        threading.Thread(target=lambda: self._detected.emit(detect_devices(skip)), daemon=True).start()

    def _on_detected(self, found: dict):
        self.detect_btn.setEnabled(True)
        self.detect_btn.setText("Auto-detect devices")
        self._fill_ports()
        cur = self.dmm_res.currentText()
        self.dmm_res.clear()
        self.dmm_res.addItems(found["visa"])
        self.dmm_res.setEditText(cur)
        for key, combo in (("arduino", self.ard_port), ("dmm", self.dmm_res), ("hmp", self.hmp_addr)):
            if found[key] and combo.isEnabled():
                combo.setEditText(found[key])
        self.log.emit(f"Detected: Arduino {found['arduino'] or '—'}, 34405A {found['dmm'] or '—'}, "
                      f"HMP4040 {found['hmp'] or '— (enter IP for LAN)'}", "ok")

    def on_scan_connect(self):
        if self._scan_ok:
            self.scan.submit(self.scan.disconnect_dev)
            return
        demo = self.demo_chk.isChecked()
        ard, dmm = self.ard_port.currentText().strip(), self.dmm_res.currentText().strip()
        if not demo and not (ard and dmm):
            self.log.emit("Enter the Arduino COM port and the multimeter VISA resource "
                          "(or use Auto-detect / demo)", "warn")
            return
        self.scan_status.setText("connecting… (Arduino reset takes ~2 s)")
        self.scan_conn_btn.setEnabled(False)

        def job():
            try:
                self.scan.connect_dev(demo, ard, dmm)
            except Exception as e:
                self.scan.connected.emit(False, f"{type(e).__name__}: {e}")
        self.scan.submit(job)

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

    def on_run(self, on):
        self.run_btn.setText("■ Stop scan" if on else "▶ Start scan")
        if on:
            self._push_channels()
        self.scan.running = on
        if on:
            self.log.emit("Voltage scan started", "ok")

    def on_folder(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self.page, "CSV log folder", self.log_folder)
        if d:
            self.log_folder = d
            self.folder_lbl.setText(d)
            self._on_log_toggled(self.log_chk.isChecked())

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
        for t, v in self.hist.values():
            t.clear()
            v.clear()
        self.last[:] = np.nan
        self.vmin[:] = np.inf
        self.vmax[:] = -np.inf
        self._dirty_rows = set(range(N_CHANNELS))
        self._dirty_curves = set(range(N_CHANNELS))
        self.follow.setChecked(True)

    def on_load_csv(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self.page, "Load voltage log", self.log_folder,
                                                        "CSV (*.csv)")
        if not path:
            return
        m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path))
        day = _dt.date.fromisoformat(m.group(1)) if m else _dt.date.today()
        n = 0
        try:
            with open(path, encoding="utf-8") as f:
                rows = csv.reader(f)
                next(rows, None)
                for row in rows:
                    if len(row) < 4 or not row[1].strip().isdigit():
                        continue
                    ch = int(row[1])
                    if not 0 <= ch < N_CHANNELS:
                        continue
                    hh, mm, ss = (int(x) for x in row[0].split(":"))
                    t = _dt.datetime.combine(day, _dt.time(hh, mm, ss)).timestamp()
                    self._add(ch, t, float(row[3]))
                    n += 1
        except Exception as e:
            self.log.emit(f"Could not load {path}: {type(e).__name__}: {e}", "err")
            return
        self.follow.setChecked(False)
        self.plot.enableAutoRange()
        self.log.emit(f"Loaded {n} readings from {os.path.basename(path)}", "ok")

    # --------------------------------------------------------------- callbacks
    def _add(self, ch, t, v):
        ts, vs = self.hist[ch]
        ts.append(t)
        vs.append(v)
        self.last[ch] = v
        self.vmin[ch] = min(self.vmin[ch], v)
        self.vmax[ch] = max(self.vmax[ch], v)
        self._dirty_rows.add(ch)
        self._dirty_curves.add(ch)

    def _on_sample(self, ch, t, v):
        self._add(ch, t, v)
        self._current = ch
        self.cur_lbl.setText(f"Channel {ch}  ·  {self.names[ch]}  ·  {v:.4f} V")

    def _on_scan_done(self, secs, n):
        self._scan_text = f"Last scan: {n} ch in {secs:.1f} s  ({n / secs:.1f} ch/s)"
        self.scan_info.setText(self._scan_text)

    def _on_scan_timing(self, sw, rd):
        self.scan_info.setText(f"{getattr(self, '_scan_text', '')}\n"
                               f"per channel: switch {sw * 1000:.1f} ms · DMM {rd * 1000:.1f} ms")

    def _on_scan_connected(self, ok, info):
        self.scan_conn_btn.setEnabled(True)
        self._set_scan_connected(ok)
        if ok:
            self.scan_status.setText(f"✓ {info}")
            self.log.emit(f"Scanner connected: {info}", "ok")
        else:
            self.scan_status.setText(info or "not connected")
            if info:
                self.log.emit(f"Scanner: {info}", "err")

    def _on_scan_stopped(self, reason):
        if self.run_btn.isChecked():
            self.run_btn.setChecked(False)
        if reason:
            self.log.emit(f"Scan stopped: {reason}", "err")
            self.scan_status.setText(f"⚠ {reason}")

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
        for ch, (v, i, on) in st.items():
            self.hmp_boxes[ch].set_state(v, i, on)

    def _on_telemetry(self, meas: dict, master: bool):
        for ch, (v, i) in meas.items():
            self.hmp_boxes[ch].set_meas(v, i)
        self._show_master(master)

    def _on_cycle(self, active, text, remaining, n):
        self.cyc_status.setText(text)
        r = int(remaining)
        self.cyc_left.setText(f"Time left: {r // 3600:02d}:{r % 3600 // 60:02d}:{r % 60:02d}")
        if not active and self.cyc_btn.isChecked():
            self.cyc_btn.blockSignals(True)
            self.cyc_btn.setChecked(False)
            self.cyc_btn.setText("▶ Start cycling")
            self.cyc_btn.blockSignals(False)

    def _on_name_edited(self, item):
        if item.column() != 1:
            return
        ch, name = item.row(), item.text().strip() or f"Ch_{item.row()}"
        if name == self.names[ch]:
            return
        self.names[ch] = name
        self._style_row(ch)
        self._push_channels()
        names = dict(self.names)
        self.scan.submit(lambda: setattr(self.scan, "names", names))
        try:
            save_channel_names(self.names_file, self.names)
        except OSError as e:
            self.log.emit(f"Could not save {self.names_file}: {e}", "err")

    def _style_row(self, ch: int):
        """Grey out channels that the range scan skips."""
        grey = is_skipped(self.names[ch])
        for col in range(1, 6):
            it = self.table.item(ch, col)
            if it is not None:
                it.setForeground(QtGui.QBrush(QtGui.QColor("#666")) if grey else QtGui.QBrush())
        self.table.item(ch, 1).setToolTip("Skipped by the range scan (named GND)" if grey else "")

    def _on_cell_double_clicked(self, row, col):
        if col != 1:
            self.manual_ch.setValue(row)
            self.mode.setCurrentIndex(1)

    def _selected_channels(self) -> set[int]:
        return {i.row() for i in self.table.selectionModel().selectedRows()}

    def _update_visibility(self, *_):
        sel = self._selected_channels() if self.show_mode.currentIndex() == 1 else None
        for ch, c in self.curves.items():
            c.setVisible(sel is None or ch in sel)

    def _refresh(self):
        """4 Hz GUI refresh: table rows and curves that got new data."""
        if self._dirty_rows:
            tbl = self.table
            tbl.blockSignals(True)
            for ch in self._dirty_rows:
                if np.isfinite(self.last[ch]):
                    tbl.item(ch, 3).setText(f"{self.last[ch]:.4f}")
                    tbl.item(ch, 4).setText(f"{self.vmin[ch]:.4f}")
                    tbl.item(ch, 5).setText(f"{self.vmax[ch]:.4f}")
                else:
                    for col in (3, 4, 5):
                        tbl.item(ch, col).setText("—")
            tbl.blockSignals(False)
            self._dirty_rows.clear()
        for ch in self._dirty_curves:
            ts, vs = self.hist[ch]
            self.curves[ch].setData(np.fromiter(ts, float, len(ts)), np.fromiter(vs, float, len(vs)))
        self._dirty_curves.clear()
        if self.follow.isChecked():
            now = time.time()
            self.plot.setXRange(now - self.window_s.value(), now, padding=0)

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
        self.scan.running = False
        deadline = time.monotonic() + 2.0
        while not self.hmp.jobs.empty() and time.monotonic() < deadline:
            time.sleep(0.02)                 # let a pending "power off" job run
        self.scan.shutdown()
        self.hmp.shutdown()


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
        self.setWindowTitle("Bench monitor: 176 channels + HMP4040")
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
    ap = argparse.ArgumentParser(description="176-channel voltage monitor + HMP4040")
    ap.add_argument("--demo", action="store_true", help="simulated hardware")
    args = ap.parse_args()
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    w = BenchWindow(args.demo)
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
