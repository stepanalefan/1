# -*- coding: utf-8 -*-
"""
it6800_supply.py
================
ITECH IT6800-series programmable DC power supply, default model IT6832 (0-32 V, 0-6 A, 192 W, one output);
the model list sets the ranges (IT6821 / 6822 / 6823 / 6831 / 6832 / 6833 / 6834).

Used as the "IT6832" tab of fsw_vsa_monitor.py, or standalone:  python it6800_supply.py [--demo]

Connection
----------
The rear DB9 of the IT6800 is a **TTL-level** serial port (not RS-232): use ITECH's IT-E121 / IT-E122
adapter, or an Arduino as USB-to-TTL bridge (arduino/it6800_bridge/it6800_bridge.ino). 8 data bits,
no parity, 1 stop bit (fixed); baud rate 4800 / 9600 / 19200 / 38400 (default 9600) and the communication
address (front panel >ADDR: 0...30, default 0) are set on the front panel and must match the settings here.
The manuals number the DB9 pins but do not assign the signals: measure before wiring (see the sketch). An Arduino Uno / Nano resets when
the port is opened: "Arduino bridge" waits 2 s before the first command.

Protocols
---------
* Frame (IT6800 series Frame Format Programming Guide, IT6821...IT6834 - default for the IT6832):
  26-byte frames  AAh | address | command | 22 data bytes | checksum (sum of bytes 1-25, low 8 bits);
  numbers little-endian (low byte first).
      20h  remote control   data[0] = 1 remote / 0 front panel
      21h  output           data[0] = 1 on / 0 off
      22h  max. voltage     uint32 mV  (upper limit of the voltage setting, LVP; 16.000 V = 00003E80h)
      23h  output voltage   uint32 mV
      24h  output current   uint16 mA  (1.000 A = 03E8h)
      25h  new address      data[0]
      26h  read state       reply: current uint16 mA [3:5], voltage uint32 mV [5:9], state [9]
                            (bit 0 output on, bit 1 over-temperature, bits 3-2 mode 1 CV / 2 CC / 3 unreg.,
                            bits 6-4 fan speed 0...5, bit 7 remote), set current uint16 mA [10:12],
                            max. voltage uint32 mV [12:16], set voltage uint32 mV [16:20]
      31h  identification   reply: model ASCII [3:8], software version [8] low / [9] high (02 03 -> V2.03),
                            serial number ASCII [10:20]
      37h  local key        data[0] = 1 enable / 0 disable
      12h  command result   data[0] 80h ok, 90h checksum error, A0h parameter error / out of range,
                            B0h command not executed, C0h command not effective
  All of the above as in the IT6800 Frame Format Programming Guide (IT6821...IT6834, chapter 3).
  IT6832: programming resolution 10 mV / 10 mA, readback 10 mV (100 mV above 20 V) / 10 mA, LVP 0-33 V.
* SCPI (IT6800A/B series and firmware with SCPI), lines ending with LF:
      SYSTem:REMote / SYSTem:LOCal, *IDN?, VOLTage <V>, VOLTage?, CURRent <A>, CURRent?,
      OUTPut ON|OFF, OUTPut?, MEASure:VOLTage?, MEASure:CURRent?, MEASure:POWer?,
      VOLTage:PROTection <V>, VOLTage:PROTection:STATe ON, SYSTem:ERRor?
Every set command is checked: frame -> 12h status byte, SCPI -> SYSTem:ERRor?.
"""

from __future__ import annotations

import csv
import os
import struct
import sys
import time

import numpy as np
from PyQt5 import QtCore, QtWidgets
import pyqtgraph as pg

import bench_monitor as bm

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # pragma: no cover
    serial = None

MODELS = {  # model: (V max, A max, W) - IT6800 user manual, specifications; LVP (max. voltage) = V max + 1 V
    "IT6832": (32.0, 6.0, 192.0), "IT6831": (18.0, 10.0, 180.0), "IT6833": (72.0, 3.0, 216.0),
    "IT6834": (150.0, 1.2, 180.0), "IT6821": (18.0, 5.0, 90.0), "IT6822": (32.0, 3.0, 96.0),
    "IT6823": (72.0, 1.5, 108.0),
}


def lvp_max(model: str) -> float:
    return MODELS[model][0] + 1.0
MODEL = "IT6832"                                 # default (bench model)
V_MAX, I_MAX, P_MAX = MODELS[MODEL]
BAUDS = (4800, 9600, 19200, 38400)
FRAME_LEN = 26
STATUS = {0x80: "ok", 0x90: "checksum error", 0xA0: "parameter error / out of range",
          0xB0: "command not executed", 0xC0: "command not effective"}
# reply of 26h: (name, start, length) of each field in the 26-byte frame
FRAME_LAYOUT = {"i_mA": (3, 2), "v_mV": (5, 4), "state": (9, 1), "iset_mA": (10, 2), "vmax_mV": (12, 4),
                "vset_mV": (16, 4)}
MODES = {1: "CV", 2: "CC", 3: "UNREG"}
ARDUINO_IDS = {(0x2341, None): "Arduino", (0x2A03, None): "Arduino", (0x1A86, 0x7523): "CH340 (Arduino clone)",
               (0x0403, 0x6001): "FTDI", (0x10C4, 0xEA60): "CP210x"}


def list_ports() -> list[tuple[str, str]]:
    """[(port, description)] of the serial ports; USB-serial bridges first."""
    if serial is None:
        return []
    out = []
    for p in serial.tools.list_ports.comports():
        tag = next((t for (vid, pid), t in ARDUINO_IDS.items()
                    if p.vid == vid and (pid is None or p.pid == pid)), "")
        out.append((p.device, f"{p.description}" + (f"  [{tag}]" if tag else "")))
    out.sort(key=lambda d: "[" not in d[1])
    return out


# --------------------------------------------------------------------------- #
#  Protocols                                                                   #
# --------------------------------------------------------------------------- #
def make_frame(addr: int, cmd: int, data: bytes = b"") -> bytes:
    body = bytes([0xAA, addr & 0xFF, cmd & 0xFF]) + (bytes(data) + bytes(22))[:22]
    return body + bytes([sum(body) & 0xFF])


def check_frame(fr: bytes) -> bool:
    return len(fr) == FRAME_LEN and fr[0] == 0xAA and (sum(fr[:25]) & 0xFF) == fr[25]


def field(fr: bytes, name: str) -> int:
    start, n = FRAME_LAYOUT[name]
    return int.from_bytes(fr[start:start + n], "little")


def hexs(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b)


class SerialLink:
    def __init__(self, port: str, baud: int, arduino_wait: bool, timeout=0.6):
        if serial is None:
            raise RuntimeError("pyserial is not installed (pip install pyserial)")
        self.s = serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1, timeout=timeout)
        if arduino_wait:
            time.sleep(2.0)                          # Uno / Nano reset on opening the port
        self.s.reset_input_buffer()

    def write(self, b: bytes):
        self.s.write(b)
        self.s.flush()

    def read(self, n: int) -> bytes:
        return self.s.read(n)

    def readline(self) -> bytes:
        return self.s.readline()

    def flush_in(self):
        self.s.reset_input_buffer()

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


class FrameDevice:
    """IT6800 frame protocol."""
    proto = "frame"

    def __init__(self, link, addr: int, model=MODEL):
        self.link, self.addr = link, addr
        self.trace = []                                 # (tx, rx) hex of the last exchanges
        self.set_remote(True)
        self.ident = {}
        try:
            self.ident = self.identify()
        except Exception:
            pass
        idm = self.ident.get("model", "")
        self.info = (f"{idm or model}, SN {self.ident.get('serial', '?')}, {self.ident.get('version', '')}, "
                     f"frame protocol, address {addr}" if self.ident else f"{model}, frame protocol, address {addr}")
        self.model_mismatch = bool(idm) and idm.upper() != model.upper()
        self.read()                                     # proves the link

    def identify(self) -> dict:
        """31h: model (ASCII), software version, serial number (ASCII)."""
        rx = self._xfer(0x31)
        model = rx[3:8].split(b"\0")[0].decode("ascii", "replace").strip()
        if model and not model.upper().startswith("IT"):
            model = "IT" + model
        return {"model": model, "version": f"V{rx[9]:X}.{rx[8]:02X}",
                "serial": rx[10:20].split(b"\0")[0].decode("ascii", "replace").strip()}

    def set_local_key(self, on: bool):
        self._set(0x37, bytes([1 if on else 0]), "local key")

    def _xfer(self, cmd: int, data: bytes = b"") -> bytes:
        tx = make_frame(self.addr, cmd, data)
        self.link.flush_in()
        self.link.write(tx)
        rx = self.link.read(FRAME_LEN)
        k = rx.find(b"\xAA")                          # resynchronise on the start byte
        if k > 0:
            rx = rx[k:] + self.link.read(k)
        self.trace = (self.trace + [(hexs(tx), hexs(rx))])[-20:]
        if len(rx) < FRAME_LEN:
            raise TimeoutError(f"no reply to command {cmd:02X}h ({len(rx)} of 26 bytes) - check port, baud, "
                               f"address, TTL wiring")
        if not check_frame(rx):
            raise IOError(f"bad reply to {cmd:02X}h (checksum): {hexs(rx)}")
        return rx

    def _set(self, cmd: int, data: bytes, what: str):
        rx = self._xfer(cmd, data)
        if rx[2] == 0x12 and rx[3] != 0x80:
            raise RuntimeError(f"{what}: {STATUS.get(rx[3], f'status {rx[3]:02X}h')}")

    def set_remote(self, on: bool):
        self._set(0x20, bytes([1 if on else 0]), "remote control")

    def set_output(self, on: bool):
        self._set(0x21, bytes([1 if on else 0]), "output")

    def set_vmax(self, v: float):
        self._set(0x22, struct.pack("<I", int(round(v * 1000))), "max. voltage")

    def set_voltage(self, v: float):
        self._set(0x23, struct.pack("<I", int(round(v * 1000))), "voltage")

    def set_current(self, i: float):
        self._set(0x24, struct.pack("<H", int(round(i * 1000))), "current")

    def read(self) -> dict:
        rx = self._xfer(0x26)
        st = field(rx, "state")
        return {"v": field(rx, "v_mV") / 1000, "i": field(rx, "i_mA") / 1000, "on": bool(st & 1),
                "ot": bool(st & 2), "mode": MODES.get((st >> 2) & 3, "") if st & 1 else "OFF",
                "fan": (st >> 4) & 7, "remote": bool(st & 0x80),
                "vset": field(rx, "vset_mV") / 1000, "iset": field(rx, "iset_mA") / 1000,
                "vmax": field(rx, "vmax_mV") / 1000}

    def raw(self, text: str) -> str:
        """Console: hex bytes. 3+ bytes starting with AA = whole frame (checksum added if 25 bytes),
        else 'cmd [data...]'."""
        b = bytes(int(x, 16) for x in text.replace(",", " ").split())
        if len(b) >= 3 and b[0] == 0xAA:
            tx = (b + bytes(25))[:25]
            tx = tx + bytes([sum(tx) & 0xFF]) if len(b) <= 25 else b[:26]
            self.link.flush_in()
            self.link.write(tx)
            rx = self.link.read(FRAME_LEN)
            self.trace = (self.trace + [(hexs(tx), hexs(rx))])[-20:]
            return f"> {hexs(tx)}\n< {hexs(rx) or '(no reply)'}"
        rx = self._xfer(b[0], b[1:])
        return f"> {self.trace[-1][0]}\n< {self.trace[-1][1]}" + \
            (f"   [{STATUS.get(rx[3], hex(rx[3]))}]" if rx[2] == 0x12 else "")

    def close(self):
        try:
            self.set_remote(False)
        except Exception:
            pass
        self.link.close()


class ScpiDevice:
    """SCPI over the same serial line (IT6800A/B and SCPI firmware)."""
    proto = "scpi"

    def __init__(self, link, model=MODEL):
        self.link = link
        self.trace = []
        self.w("SYSTem:REMote")
        self.info = self.q("*IDN?") or model
        self.errors()

    def w(self, c: str):
        self.link.write((c + "\n").encode())
        self.trace = (self.trace + [(c, "")])[-20:]

    def q(self, c: str) -> str:
        self.link.flush_in()
        self.w(c)
        r = self.link.readline().decode(errors="replace").strip()
        self.trace[-1] = (c, r)
        if not r:
            raise TimeoutError(f"no reply to {c} - check port, baud, TTL wiring")
        return r

    def errors(self) -> list[str]:
        out = []
        for _ in range(10):
            e = self.q("SYSTem:ERRor?")
            if e.startswith(("0", "+0")):
                break
            out.append(e)
        return out

    def _set(self, c: str):
        self.w(c)
        errs = self.errors()
        if errs:
            raise RuntimeError(f"{c}: {'; '.join(errs)}")

    def set_remote(self, on: bool):
        self.w("SYSTem:REMote" if on else "SYSTem:LOCal")

    def set_local_key(self, on: bool):
        self.w("SYSTem:LOCal" if on else "SYSTem:REMote")

    def set_output(self, on: bool):
        self._set(f"OUTPut {'ON' if on else 'OFF'}")

    def set_vmax(self, v: float):
        self._set(f"VOLTage:PROTection {v:.3f}")
        self._set("VOLTage:PROTection:STATe ON")

    def set_voltage(self, v: float):
        self._set(f"VOLTage {v:.3f}")

    def set_current(self, i: float):
        self._set(f"CURRent {i:.4f}")

    def read(self) -> dict:
        v, i = float(self.q("MEASure:VOLTage?")), float(self.q("MEASure:CURRent?"))
        on = self.q("OUTPut?").strip().upper() in ("1", "ON")
        vset, iset = float(self.q("VOLTage?")), float(self.q("CURRent?"))
        try:
            vmax = float(self.q("VOLTage:PROTection?"))
        except (ValueError, TimeoutError):
            vmax = float("nan")
        mode = ("CC" if iset > 0 and i >= 0.99 * iset else "CV") if on else "OFF"
        return {"v": v, "i": i, "on": on, "ot": False, "mode": mode, "vset": vset, "iset": iset, "vmax": vmax}

    def raw(self, text: str) -> str:
        if text.strip().endswith("?"):
            return f"> {text}\n< {self.q(text)}"
        self.w(text)
        errs = self.errors()
        return f"> {text}" + (f"\n< {'; '.join(errs)}" if errs else "")

    def close(self):
        try:
            self.set_remote(False)
        except Exception:
            pass
        self.link.close()


class DemoDevice:
    """Simulated supply with a 10 Ω load."""
    proto = "demo"

    def __init__(self, model=MODEL):
        self.info = f"{model} (simulated, 10 Ω load)"
        self.s = {"vset": 12.0, "iset": 0.5, "vmax": MODELS[model][0], "on": False}
        self.trace = []
        self.rng = np.random.default_rng()

    def set_remote(self, on):
        pass

    def set_local_key(self, on):
        pass

    def set_output(self, on):
        self.s["on"] = bool(on)

    def set_vmax(self, v):
        self.s["vmax"] = v
        self.s["vset"] = min(self.s["vset"], v)

    def set_voltage(self, v):
        if v > self.s["vmax"]:
            raise RuntimeError(f"voltage: parameter error ({v:g} V > max. {self.s['vmax']:g} V)")
        self.s["vset"] = v

    def set_current(self, i):
        self.s["iset"] = i

    def read(self):
        s = self.s
        if not s["on"]:
            return {"v": 0.0, "i": 0.0, "on": False, "ot": False, "mode": "OFF", **{k: s[k] for k in
                                                                                   ("vset", "iset", "vmax")}}
        i = s["vset"] / 10.0
        mode = "CV"
        if i > s["iset"]:
            i, mode = s["iset"], "CC"
        v = i * 10.0 if mode == "CC" else s["vset"]
        return {"v": v + self.rng.normal(0, 0.001), "i": max(0.0, i + self.rng.normal(0, 0.0002)), "on": True,
                "ot": False, "mode": mode, **{k: s[k] for k in ("vset", "iset", "vmax")}}

    def raw(self, text):
        return f"> {text}\n< (demo)"

    def close(self):
        pass


# --------------------------------------------------------------------------- #
#  Worker                                                                      #
# --------------------------------------------------------------------------- #
class IT68Worker(bm._Worker):
    telemetry = QtCore.pyqtSignal(object)               # read() dict
    connected = QtCore.pyqtSignal(bool, str)
    console = QtCore.pyqtSignal(str)

    TEL_INTERVAL = 0.5

    def __init__(self):
        super().__init__()
        self._next_tel = 0.0
        self.model = MODEL

    def connect_dev(self, demo, port, baud, addr, proto, arduino_wait):
        self.disconnect_dev()
        if demo:
            self.dev = DemoDevice(self.model)
        else:
            link = SerialLink(port, baud, arduino_wait)
            try:
                self.dev = FrameDevice(link, addr, self.model) if proto == "frame" else ScpiDevice(link, self.model)
            except Exception:
                link.close()
                raise
        self.connected.emit(True, self.dev.info)
        if getattr(self.dev, "model_mismatch", False):
            self.log.emit(f"{self.model} selected, but the supply reports {self.dev.ident.get('model')} - "
                          f"check the model (ranges)", "warn")
        self._next_tel = 0.0

    def set_local_key(self, on):
        self.dev.set_local_key(on)
        self.log.emit(f"{self.model} front-panel local key {'enabled' if on else 'disabled'}", "ok")

    def disconnect_dev(self):
        if self.dev is not None:
            self.dev.close()
            self.dev = None
            self.connected.emit(False, "")

    def apply(self, v, i, vmax, on):
        """Max. voltage first, then voltage, current, output (each checked)."""
        d = self.dev
        done, errs = [], []
        for what, fn, val in (("max. voltage", d.set_vmax, vmax), ("voltage", d.set_voltage, v),
                              ("current", d.set_current, i), ("output", d.set_output, on)):
            if val is None:
                continue
            try:
                fn(val)
                done.append(f"{what} {'ON' if val is True else 'OFF' if val is False else f'{val:g}'}")
            except Exception as e:
                errs.append(f"{what}: {e}")
        self.log.emit(f"{self.model}: " + ", ".join(done) + (f"  ERRORS: {'; '.join(errs)}" if errs else ""),
                      "warn" if errs else "ok")
        self._next_tel = 0.0
        if errs:
            raise RuntimeError("; ".join(errs))

    def set_output(self, on, reason=""):
        self.dev.set_output(on)
        self.log.emit(f"{self.model} output {'ON' if on else 'OFF'}" + (f" ({reason})" if reason else ""), "ok")
        self._next_tel = 0.0

    def command(self, text):
        try:
            self.console.emit(self.dev.raw(text))
        except Exception as e:
            self.console.emit(f"> {text}\n! {type(e).__name__}: {e}")

    def step(self) -> bool:
        if self.dev is None:
            return False
        now = time.monotonic()
        if now < self._next_tel:
            return False
        self.telemetry.emit(self.dev.read())
        self._next_tel = now + self.TEL_INTERVAL
        return True

    def on_error(self, e):
        self.log.emit(f"{self.model} communication error: {type(e).__name__}: {e}", "err")
        try:
            self.dev.close()
        except Exception:
            pass
        self.dev = None
        self.connected.emit(False, "error")


class IT68Csv:
    """<model>_log_YYYY-MM-DD.csv (e.g. it6832_log_2026-11-13.csv)"""
    header = ["Time", "On", "Vset_V", "Iset_A", "Vmax_V", "V_V", "I_A", "P_W", "Mode"]

    def __init__(self, folder, model=MODEL):
        self.folder, self.f, self.day, self.model = folder, None, None, model.lower()

    def write(self, t, r):
        day = time.strftime("%Y-%m-%d", time.localtime(t))
        if day != self.day:
            self.close()
            path = os.path.join(self.folder, f"{self.model}_log_{day}.csv")
            new = not os.path.isfile(path)
            self.f = open(path, "a", newline="", encoding="utf-8")
            self.w = csv.writer(self.f)
            if new:
                self.w.writerow(self.header)
            self.day = day
        self.w.writerow([time.strftime("%H:%M:%S", time.localtime(t)), int(r["on"]), f"{r['vset']:.3f}",
                         f"{r['iset']:.4f}", f"{r['vmax']:.3f}", f"{r['v']:.4f}", f"{r['i']:.4f}",
                         f"{r['v'] * r['i']:.3f}", r["mode"]])
        self.f.flush()

    def close(self):
        if self.f:
            self.f.close()
            self.f = None
            self.day = None


# --------------------------------------------------------------------------- #
#  GUI                                                                         #
# --------------------------------------------------------------------------- #
class IT6800Tab(QtCore.QObject):
    """IT6800-series (IT6832) tab: `settings_panel` (left) and `page`. Read by the Main tab / BOS station:
    ok, channels ([1]), last (read() dict), hist, comm_error_t, status_lbl, worker."""
    log = QtCore.pyqtSignal(str, str)

    def __init__(self, parent=None, folder_fn=None):
        super().__init__(parent)
        self.folder_fn = folder_fn or bm.script_dir
        self.ok = False
        self.channels = [1]
        self.last: dict = {}
        self.hist = ([], [], [])
        self.comm_error_t = 0.0
        self.csv: IT68Csv | None = None
        self._next_log = 0.0
        self.worker = IT68Worker()
        self.worker.log.connect(self.log)
        self.worker.connected.connect(self._on_connected)
        self.worker.telemetry.connect(self._on_tel)
        self.worker.console.connect(lambda t: self.con_out.appendPlainText(t))
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
        g = QtWidgets.QGroupBox("ITECH IT6800 (rear DB9, TTL serial)")
        f = QtWidgets.QFormLayout(g)
        self.model_cb = QtWidgets.QComboBox()
        for m, (vm, im, pm) in MODELS.items():
            self.model_cb.addItem(f"{m}  ({vm:g} V / {im:g} A / {pm:g} W)", m)
        self.model_cb.currentIndexChanged.connect(self._model_changed)
        self.port = QtWidgets.QComboBox()
        self.port.setEditable(True)
        self.port.setToolTip("COM port of the Arduino bridge / ITECH IT-E121, IT-E122 adapter")
        ref = QtWidgets.QPushButton("⟳")
        ref.setFixedWidth(34)
        ref.setToolTip("Rescan serial ports")
        ref.clicked.connect(self.refresh_ports)
        prow = QtWidgets.QHBoxLayout()
        prow.addWidget(self.port, 1)
        prow.addWidget(ref)
        self.baud = QtWidgets.QComboBox()
        for b in BAUDS:
            self.baud.addItem(str(b), b)
        self.baud.setCurrentIndex(BAUDS.index(9600))
        self.baud.setToolTip("Same as on the front panel (menu: baud rate); 8 data bits, no parity, 1 stop bit")
        self.addr_sp = QtWidgets.QSpinBox()
        self.addr_sp.setRange(0, 254)
        self.addr_sp.setToolTip("Communication address set on the front panel (>ADDR, 0...30, default 0); "
                                "frame protocol")
        self.proto = QtWidgets.QComboBox()
        self.proto.addItem("Frame (IT6800, IT6821-IT6834)", "frame")
        self.proto.addItem("SCPI (IT6800A/B, SCPI firmware)", "scpi")
        self.ard = QtWidgets.QCheckBox("Arduino bridge (wait 2 s after opening)")
        self.ard.setChecked(True)
        self.ard.setToolTip("Arduino Uno / Nano reset when the COM port is opened")
        self.conn_btn = QtWidgets.QPushButton("Connect")
        self.conn_btn.clicked.connect(self.on_connect)
        self.status_lbl = QtWidgets.QLabel("not connected")
        self.status_lbl.setWordWrap(True)
        f.addRow("Model", self.model_cb)
        f.addRow("Port", prow)
        f.addRow("Baud rate", self.baud)
        f.addRow("Address", self.addr_sp)
        f.addRow("Protocol", self.proto)
        f.addRow(self.ard)
        f.addRow(self.conn_btn)
        f.addRow(self.status_lbl)
        lv.addWidget(g)
        g = QtWidgets.QGroupBox("Logging")
        f = QtWidgets.QFormLayout(g)
        self.log_chk = QtWidgets.QCheckBox("Write daily log")
        self.log_chk.setToolTip("<model>_log_YYYY-MM-DD.csv (e.g. it6832_log_…) in the log folder of the HMP4040 tab")
        self.log_chk.setChecked(True)
        self.log_chk.toggled.connect(lambda *_: self._reopen_log())
        self.log_int = bm._spin(0.5, 3600, 1, " s", 1.0)
        f.addRow(self.log_chk)
        f.addRow("Log every", self.log_int)
        lv.addWidget(g)
        note = QtWidgets.QLabel("The DB9 carries TTL levels, not RS-232: never connect it to a PC's RS-232 port. "
                                "Use the Arduino bridge (sketch in the arduino folder) or the ITECH adapter.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#8a96a3;font-size:11px;")
        lv.addWidget(note)
        lv.addStretch(1)
        self.refresh_ports()
        return w

    def refresh_ports(self):
        cur = self.port.currentText()
        self.port.clear()
        for dev, desc in list_ports():
            self.port.addItem(dev, desc)
            self.port.setItemData(self.port.count() - 1, desc, QtCore.Qt.ToolTipRole)
        if cur:
            self.port.setEditText(cur)

    def _build_page(self):
        page = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(page)
        root.setContentsMargins(4, 4, 4, 4)
        self.g_ctrl = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(self.g_ctrl)
        row.setContentsMargins(0, 0, 0, 0)
        self.out_box = box = QtWidgets.QGroupBox()
        f = QtWidgets.QFormLayout(box)
        self.v = bm._spin(0, V_MAX, 2, " V", 12.0, 0.1)
        self.v.setToolTip("Output voltage (frame 23h / SCPI VOLTage)")
        self.i = bm._spin(0, I_MAX, 2, " A", 0.5, 0.01)
        self.i.setToolTip("Current limit, 10 mA steps (frame 24h / SCPI CURRent)")
        self.vmax = bm._spin(0, V_MAX, 2, " V", V_MAX, 1.0)
        self.vmax.setToolTip("Frame protocol: upper limit of the voltage setting (22h); SCPI: over-voltage protection")
        self.on = QtWidgets.QCheckBox("Output on")
        set_btn = QtWidgets.QPushButton("Set")
        set_btn.setToolTip("Send max. voltage, voltage, current and output state")
        set_btn.clicked.connect(self.on_set)
        for lbl, wd in (("Voltage", self.v), ("Current limit", self.i), ("Max. voltage", self.vmax)):
            f.addRow(lbl, wd)
        f.addRow(self.on)
        f.addRow(set_btn)
        row.addWidget(box)
        mbox = QtWidgets.QGroupBox("Measured")
        mf = QtWidgets.QVBoxLayout(mbox)
        self.meas = QtWidgets.QLabel("—")
        self.meas.setStyleSheet("font-size:22px;font-weight:700;color:#4fd18b;")
        self.mode = QtWidgets.QLabel("")
        self.mode.setStyleSheet("font-weight:700;")
        self.sets = QtWidgets.QLabel("")
        self.sets.setStyleSheet("color:#8a96a3;")
        for wd in (self.meas, self.mode, self.sets):
            mf.addWidget(wd)
        mf.addStretch(1)
        row.addWidget(mbox, 1)
        side = QtWidgets.QVBoxLayout()
        for txt, on in (("OUTPUT ON", True), ("OUTPUT OFF", False)):
            b = QtWidgets.QPushButton(txt)
            b.setMinimumHeight(36)
            b.setStyleSheet("font-weight:700;")
            b.clicked.connect(lambda _, on=on: self.worker.submit(lambda: self.worker.set_output(on)))
            side.addWidget(b)
        lk = QtWidgets.QPushButton("Enable local key")
        lk.setToolTip("Frame 37h: allow the front-panel Local key while in remote mode")
        lk.clicked.connect(lambda: self.worker.submit(lambda: self.worker.set_local_key(True)))
        side.addWidget(lk)
        side.addStretch(1)
        row.addLayout(side)
        root.addWidget(self.g_ctrl)
        # console
        self.g_con = QtWidgets.QGroupBox("Command console")
        cv = QtWidgets.QVBoxLayout(self.g_con)
        crow = QtWidgets.QHBoxLayout()
        self.con_in = QtWidgets.QLineEdit()
        self.con_in.setPlaceholderText("frame: 26  |  23 E0 2E 00 00  |  AA 00 26 …   SCPI: MEAS:VOLT?")
        self.con_in.setToolTip("Frame protocol: hex bytes 'command data…' (header, address and checksum are "
                               "added) or a whole frame starting with AA (checksum added if left out).\n"
                               "SCPI: a command line; a '?' query shows the reply.")
        self.con_in.returnPressed.connect(self.on_console)
        send = QtWidgets.QPushButton("Send")
        send.clicked.connect(self.on_console)
        crow.addWidget(self.con_in, 1)
        crow.addWidget(send)
        cv.addLayout(crow)
        self.con_out = QtWidgets.QPlainTextEdit()
        self.con_out.setReadOnly(True)
        self.con_out.setMaximumBlockCount(500)
        self.con_out.setMaximumHeight(110)
        self.con_out.setStyleSheet("font-family:Consolas,'DejaVu Sans Mono',monospace;")
        cv.addWidget(self.con_out)
        root.addWidget(self.g_con)
        # plot
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
        for wd in (QtWidgets.QLabel("Plot"), self.plot_what, self.follow, self.window_s, clear):
            bar.addWidget(wd)
        bar.addStretch(1)
        root.addLayout(bar)
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.getPlotItem().setMenuEnabled(False)
        self.plot.getViewBox().sigRangeChangedManually.connect(lambda *_: self.follow.setChecked(False))
        self.curve = self.plot.plot([], [], pen=pg.mkPen("#e3b341", width=1.5))
        root.addWidget(self.plot, 1)
        self.timer = QtCore.QTimer(self, interval=500, timeout=self._redraw)
        self.timer.start()
        self._model_changed()
        return page

    @property
    def model(self) -> str:
        return self.model_cb.currentData() if hasattr(self, "model_cb") else MODEL

    def _model_changed(self, *_):
        m = self.model
        vm, im, pm = MODELS[m]
        self.worker.model = m
        if hasattr(self, "out_box"):
            self.out_box.setTitle(f"{m} output  (0-{vm:g} V, 0-{im:g} A, {pm:g} W)")
            self.v.setMaximum(vm)
            self.i.setMaximum(im)
            self.vmax.setMaximum(lvp_max(m))
            self.vmax.setValue(lvp_max(m))

    # ---------------------------------------------------------------- actions
    def _set_connected(self, on):
        self.ok = on
        self.conn_btn.setText("Disconnect" if on else "Connect")
        for wd in (self.model_cb, self.port, self.baud, self.addr_sp, self.proto, self.ard, self.demo_chk):
            wd.setEnabled(not on)
        self.g_ctrl.setEnabled(on)
        self.g_con.setEnabled(on)
        if not on:
            self.meas.setText("—")
            self.mode.setText("")

    def on_connect(self):
        if self.ok:
            self.worker.submit(self.worker.disconnect_dev)
            return
        demo, port = self.demo_chk.isChecked(), self.port.currentText().strip()
        if not demo and not port:
            self.log.emit(f"{self.model}: choose the COM port", "warn")
            return
        args = (demo, port, self.baud.currentData(), self.addr_sp.value(), self.proto.currentData(),
                self.ard.isChecked())
        self.status_lbl.setText("connecting…")
        self.conn_btn.setEnabled(False)

        def job():
            try:
                self.worker.connect_dev(*args)
            except Exception as e:
                self.worker.connected.emit(False, f"{type(e).__name__}: {e}")
        self.worker.submit(job)

    def on_set(self):
        args = (round(self.v.value(), 3), round(self.i.value(), 4), round(self.vmax.value(), 3), self.on.isChecked())

        def job():
            try:
                self.worker.apply(*args)
            except RuntimeError:
                pass                                     # already logged
        self.worker.submit(job)

    def on_console(self):
        t = self.con_in.text().strip()
        if t:
            self.worker.submit(lambda: self.worker.command(t))

    def on_clear(self):
        for x in self.hist:
            x.clear()
        self.follow.setChecked(True)

    def _reopen_log(self):
        if self.csv:
            self.csv.close()
        self.csv = IT68Csv(self.folder_fn(), self.model) if self.log_chk.isChecked() and self.ok else None
        self._next_log = 0.0

    # --------------------------------------------------------------- callbacks
    def _on_connected(self, ok, info):
        self.conn_btn.setEnabled(True)
        self._set_connected(ok)
        if ok:
            self.status_lbl.setText(f"✓ {info}")
            self.log.emit(f"{self.model} connected: {info}", "ok")
            self._first = True
            self._reopen_log()
        else:
            if info == "error":
                self.comm_error_t = time.time()
            self.status_lbl.setText(info or "not connected")
            if info and info != "error":
                self.log.emit(f"{self.model}: {info}", "err")
            self.last = {}

    def _on_tel(self, r):
        self.last = dict(r)
        if getattr(self, "_first", False):                 # show the instrument's settings once
            self._first = False
            for wd, k in ((self.v, "vset"), (self.i, "iset"), (self.vmax, "vmax")):
                wd.blockSignals(True)
                wd.setValue(r[k])
                wd.blockSignals(False)
            self.on.setChecked(r["on"])
        now = time.time()
        self.meas.setText(f"{r['v']:.3f} V    {r['i']:.4f} A    {r['v'] * r['i']:.2f} W")
        mode = r["mode"] + (" · OVER-TEMP" if r.get("ot") else "")
        color = "#ff6b6b" if r["mode"] == "CC" or r.get("ot") else ("#4fd18b" if r["mode"] == "CV" else "#8a96a3")
        self.mode.setText(mode)
        self.mode.setStyleSheet(f"font-weight:700;font-size:15px;color:{color};")
        self.sets.setText(f"set {r['vset']:.2f} V / {r['iset']:.2f} A, max. {r['vmax']:.2f} V"
                          + (f"  ·  fan {r['fan']}/5" if "fan" in r else "")
                          + ("  ·  remote" if r.get("remote") else ""))
        ts, vs, is_ = self.hist
        ts.append(now)
        vs.append(r["v"])
        is_.append(r["i"])
        if len(ts) > 200_000:
            for x in self.hist:
                del x[:50_000]
        if self.csv and now >= self._next_log:
            self._next_log = now + self.log_int.value()
            try:
                self.csv.write(now, r)
            except OSError as e:
                self.log.emit(f"{self.model} log stopped: {e}", "err")
                self.log_chk.setChecked(False)

    # interface used by the Main tab / BOS station (like the N6701C tab, one channel)
    @property
    def last_meas(self):
        return {1: (self.last["v"], self.last["i"])} if self.last else {}

    def is_cc(self, ch=1) -> bool:
        return self.last.get("mode") == "CC"

    def tripped(self, ch=1) -> int:
        return int(bool(self.last.get("ot")))

    def _redraw(self):
        k = self.plot_what.currentIndex()
        self.plot.setLabel("left", ("Current", "Voltage", "Power")[k], units=("A", "V", "W")[k])
        ts, vs, is_ = self.hist
        n = min(len(ts), len(vs), len(is_))
        v, i = np.asarray(vs[:n], float), np.asarray(is_[:n], float)
        self.curve.setData(np.asarray(ts[:n], float), i if k == 0 else v if k == 1 else v * i)
        if self.follow.isChecked():
            now = time.time()
            self.plot.setXRange(now - self.window_s.value(), now, padding=0)

    def shutdown(self):
        self.timer.stop()
        self.worker.submit(self.worker.disconnect_dev)
        time.sleep(0.05)
        self.worker.shutdown()
        if self.csv:
            self.csv.close()


def main():
    import argparse
    ap = argparse.ArgumentParser(description=f"ITECH {MODEL} control")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    pg.setConfigOptions(background="#0f1216", foreground="#c9d1d9")
    win = QtWidgets.QMainWindow()
    tab = IT6800Tab(win)
    tab.log.connect(lambda t, l: print(f"[{l}] {t}"))
    split = QtWidgets.QSplitter()
    split.addWidget(tab.settings_panel)
    split.addWidget(tab.page)
    win.setCentralWidget(split)
    win.setStyleSheet(bm.STYLE)
    win.resize(1300, 800)
    if args.demo:
        tab.demo_chk.setChecked(True)
    win.show()
    app.aboutToQuit.connect(tab.shutdown)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
