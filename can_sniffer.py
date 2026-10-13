#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
can_sniffer.py
==============
CAN 2.0A/2.0B bus sniffer with GUI for the CANable USB-CAN adapter (STM32F072).

    python can_sniffer.py                       # pick port / bitrate in the GUI
    python can_sniffer.py --port COM7 --bitrate 500000 --connect
    python can_sniffer.py --demo                # simulated bus, no hardware

Adapter firmware
----------------
The CANable is shipped with one of two firmwares, select the matching interface:

* slcan (default "canable-fw"): the board is a virtual COM port
  (VID:PID 16D0:117E, on Windows "USB Serial Device (COMx)", on Linux
  /dev/ttyACMx). Spoken directly over pyserial, LAWICEL ASCII protocol:
      C            close channel          O        open channel
      S0..S8       10k 20k 50k 100k 125k 250k 500k 750k 1M
      M0 / M1      normal / silent (listen-only, no ACK) - canable-fw only
      V            firmware version
      tIIILDD..    11-bit frame           TIIIIIIIILDD..  29-bit frame (2.0B)
      rIIIL        11-bit remote frame    RIIIIIIIIL      29-bit remote frame
  Replies: CR = OK, BEL (0x07) = error.
* candleLight (gs_usb): native USB device 1D50:606F, opened via python-can
  ("gs_usb" interface, needs pyusb + libusb; on Windows the device uses WinUSB
  out of the box, libusb-1.0.dll must be next to python.exe or on PATH).
  On Linux candleLight is also a SocketCAN device:
      sudo ip link set can0 up type can bitrate 500000 [listen-only on]
  then use the "SocketCAN" interface (bitrate is set by ip link, not here).

Listen-only: the adapter does not ACK frames, so the sniffer is invisible on the
bus. If the sniffer is connected to a single node (nobody else to ACK), turn
listen-only OFF, otherwise the node will retransmit the first frame forever.

Views
-----
* Trace   - every frame in arrival order (time, delta-t to previous frame with
            the same ID, direction, ID, type, DLC, data, ASCII).
* Monitor - one row per ID: latest data, count, period; bytes that just
            changed are highlighted, bytes that never change are dimmed. This
            is the quickest way to find which byte reacts to a button / command.
* Filter  - "100-1FF, 7E8, !7DF, 18FEF100, 700/700" : hex IDs, ranges,
            id/mask and !exclusions. Applies to both views.
* Record  - writes every received frame to a candump-format .log (can-utils,
            python-can, SavvyCAN can open it). Open log... loads such a file.
* Transmit - single frames or periodic, standard or extended ID, RTR.

Install:  pip install PyQt5 pyserial
          pip install python-can gs_usb pyusb   (only for candleLight / SocketCAN)
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as _dt
import math
import os
import random
import re
import sys
import threading
import time
from collections import deque

from PyQt5 import QtCore, QtGui, QtWidgets

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # pragma: no cover - demo mode still works
    serial = None


# --------------------------------------------------------------------------- #
#  Constants                                                                   #
# --------------------------------------------------------------------------- #
BITRATES = [  # bit/s, slcan command
    (10_000, "S0"), (20_000, "S1"), (50_000, "S2"), (100_000, "S3"),
    (125_000, "S4"), (250_000, "S5"), (500_000, "S6"), (750_000, "S7"),
    (1_000_000, "S8"),
]
DEFAULT_BITRATE = 500_000
CANABLE_USB_IDS = [(0x16D0, 0x117E), (0x0483, 0x5740)]   # slcan firmware / generic STM32 VCP
TRACE_MAX = 100_000      # rows kept in the Trace view
RAW_MAX = 300_000        # frames kept in memory (Trace is rebuilt from these on filter change)
HL_TIME = 2.0            # s, a changed byte stays highlighted in the Monitor
APP_ID = "CANable.Sniffer.1"
ICON_FILE = "app.ico"

INTERFACES = [  # key, label
    ("slcan", "CANable — slcan firmware (COM port)"),
    ("gs_usb", "CANable — candleLight firmware (gs_usb)"),
    ("socketcan", "SocketCAN (Linux, e.g. can0)"),
    ("demo", "Demo — simulated bus"),
]


def resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def script_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def set_windows_app_id():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  Frame + text formats                                                        #
# --------------------------------------------------------------------------- #
class Frame:
    __slots__ = ("ts", "can_id", "ext", "rtr", "dlc", "data", "tx", "dt", "seq")

    def __init__(self, ts, can_id, ext=False, rtr=False, dlc=None, data=b"", tx=False):
        self.ts = ts
        self.can_id = can_id
        self.ext = ext
        self.rtr = rtr
        self.data = bytes(data)
        self.dlc = len(self.data) if dlc is None else dlc
        self.tx = tx
        self.dt = None      # s since previous frame with the same ID
        self.seq = 0

    @property
    def key(self):
        return (self.ext, self.can_id)

    def id_str(self) -> str:
        return f"{self.can_id:08X}" if self.ext else f"{self.can_id:03X}"

    def type_str(self) -> str:
        return ("EXT" if self.ext else "STD") + (" RTR" if self.rtr else "")

    def data_str(self) -> str:
        return " ".join(f"{b:02X}" for b in self.data)

    def ascii_str(self) -> str:
        return "".join(chr(b) if 32 <= b < 127 else "." for b in self.data)

    def bits(self) -> float:
        """Approximate bits on the wire incl. ~10% stuffing and 3-bit IFS."""
        n = (67 if self.ext else 47) + (0 if self.rtr else 8 * self.dlc)
        return n * 1.1


def parse_slcan(line: str, ts: float) -> Frame | None:
    if not line:
        return None
    c = line[0]
    if c in "tr":
        idlen, ext = 3, False
    elif c in "TR":
        idlen, ext = 8, True
    else:
        return None
    rtr = c in "rR"
    try:
        can_id = int(line[1:1 + idlen], 16)
        dlc = int(line[1 + idlen], 16)
    except (ValueError, IndexError):
        return None
    if dlc > 8 or (not ext and can_id > 0x7FF) or can_id > 0x1FFFFFFF:
        return None
    data = b""
    if not rtr:
        hexdata = line[2 + idlen:2 + idlen + 2 * dlc]   # anything after = optional timestamp
        if len(hexdata) != 2 * dlc:
            return None
        try:
            data = bytes.fromhex(hexdata)
        except ValueError:
            return None
    return Frame(ts, can_id, ext, rtr, dlc, data)


def encode_slcan(f: Frame) -> str:
    if f.ext:
        s = ("R" if f.rtr else "T") + f"{f.can_id:08X}"
    else:
        s = ("r" if f.rtr else "t") + f"{f.can_id:03X}"
    s += f"{f.dlc:X}"
    if not f.rtr:
        s += f.data.hex().upper()
    return s + "\r"


def to_candump(f: Frame, iface="can0") -> str:
    body = ("R" + (str(f.dlc) if f.dlc else "")) if f.rtr else f.data.hex().upper()
    return f"({f.ts:.6f}) {iface} {f.id_str()}#{body}\n"


_CANDUMP_RE = re.compile(r"^\((\d+(?:\.\d+)?)\)\s+\S+\s+([0-9A-Fa-f]{1,8})#([0-9A-Fa-f.Rr]*)")


def parse_candump(line: str) -> Frame | None:
    m = _CANDUMP_RE.match(line.strip())
    if not m:
        return None
    ts, ids, body = float(m.group(1)), m.group(2), m.group(3).replace(".", "")
    ext = len(ids) > 3
    if body[:1] in ("R", "r"):
        dlc = int(body[1:]) if body[1:].isdigit() else 0
        return Frame(ts, int(ids, 16), ext, True, min(dlc, 8))
    try:
        data = bytes.fromhex(body)
    except ValueError:
        return None
    if len(data) > 8:
        return None
    return Frame(ts, int(ids, 16), ext, False, None, data)


def parse_filter(text: str):
    """'100-1FF, 7E8, !7DF, 700/700' -> predicate(can_id) or None (= pass all)."""
    inc, exc = [], []
    for tok in re.split(r"[,;\s]+", text.strip()):
        if not tok:
            continue
        neg = tok.startswith("!")
        tok = tok.lstrip("!").upper().removeprefix("0X")
        if "-" in tok:
            a, b = tok.split("-", 1)
            rule = ("r", int(a, 16), int(b, 16))
        elif "/" in tok:
            a, b = tok.split("/", 1)
            rule = ("m", int(a, 16), int(b, 16))
        else:
            v = int(tok, 16)
            rule = ("r", v, v)
        (exc if neg else inc).append(rule)
    if not inc and not exc:
        return None

    def hit(rule, i):
        kind, a, b = rule
        return a <= i <= b if kind == "r" else (i & b) == (a & b)

    def pred(i):
        if inc and not any(hit(r, i) for r in inc):
            return False
        return not any(hit(r, i) for r in exc)
    return pred


def parse_hex_bytes(text: str) -> bytes:
    s = re.sub(r"[\s,:;]|0x", "", text.strip(), flags=re.I)
    if len(s) % 2:
        raise ValueError("odd number of hex digits")
    data = bytes.fromhex(s)
    if len(data) > 8:
        raise ValueError("CAN 2.0 frame carries at most 8 bytes")
    return data


# --------------------------------------------------------------------------- #
#  Backends: recv() -> list[Frame] (returns within ~50 ms), send(), close()   #
# --------------------------------------------------------------------------- #
class SlcanBackend:
    """CANable with the stock slcan firmware, spoken directly over pyserial."""

    def __init__(self, port: str, bitrate: int, listen_only: bool):
        if serial is None:
            raise RuntimeError("pyserial is not installed:  pip install pyserial")
        code = dict(BITRATES).get(bitrate)
        if code is None:
            raise ValueError(f"slcan supports only {[b for b, _ in BITRATES]} bit/s")
        self.errors = 0
        self._buf = bytearray()
        self._wlock = threading.Lock()
        self.s = serial.Serial(port, 115200, timeout=0.05, write_timeout=0.5)
        try:
            self._write("C\r")             # channel may still be open after a crash
            time.sleep(0.1)
            self.s.reset_input_buffer()
            version = self._cmd("V", want_text=True)
            if self._cmd(code) is None:
                raise RuntimeError(f"adapter did not accept bitrate command {code}")
            # canable-fw: M1 = silent mode. Firmwares without it answer BEL - harmless.
            mode_ok = self._cmd("M1" if listen_only else "M0") is not None
            if self._cmd("O") is None:
                raise RuntimeError("adapter did not accept open command 'O'")
        except Exception:
            self.s.close()
            raise
        lo = ("listen-only" if mode_ok else "listen-only NOT supported by firmware") if listen_only else "normal"
        self.info = f"slcan {port}, {bitrate // 1000} kbit/s, {lo}" + (f", fw {version}" if version else "")

    def _write(self, text: str):
        with self._wlock:
            self.s.write(text.encode("ascii"))

    def _cmd(self, cmd: str, want_text=False, timeout=0.5):
        """Send a command, wait for CR (ok) / BEL (error). Returns reply text or None."""
        self._write(cmd + "\r")
        reply = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            b = self.s.read(1)
            if not b:
                continue
            if b == b"\x07":
                return None
            if b == b"\r":
                return reply.decode("ascii", "replace")
            reply += b
        return "" if want_text else None

    def recv(self) -> list[Frame]:
        chunk = self.s.read(self.s.in_waiting or 1)
        if not chunk:
            return []
        ts = time.time()
        if 7 in chunk:                      # BEL: error reply (e.g. tx failed / buffer full)
            self.errors += chunk.count(7)
            chunk = chunk.replace(b"\x07", b"")
        self._buf += chunk
        parts = self._buf.split(b"\r")
        self._buf = bytearray(parts[-1][-64:])
        out = []
        for p in parts[:-1]:
            f = parse_slcan(p.decode("ascii", "replace").strip("\n"), ts)
            if f is not None:
                out.append(f)
        return out

    def send(self, f: Frame):
        self._write(encode_slcan(f))

    def close(self):
        try:
            self._write("C\r")
        except Exception:
            pass
        self.s.close()


class PyCanBackend:
    """Any python-can interface (gs_usb = candleLight firmware, socketcan, ...)."""

    def __init__(self, interface: str, channel: str, bitrate: int, listen_only: bool):
        try:
            import can
        except ImportError:
            raise RuntimeError("python-can is not installed:  pip install python-can gs_usb pyusb")
        self._can = can
        self.errors = 0
        lo = ""
        if interface == "gs_usb":
            self.bus = can.Bus(interface="gs_usb", channel=int(channel or 0), bitrate=bitrate)
            if listen_only:
                try:
                    from gs_usb.constants import GS_CAN_MODE_LISTEN_ONLY
                    self.bus.gs_usb.stop()
                    self.bus.gs_usb.start(GS_CAN_MODE_LISTEN_ONLY)
                    lo = ", listen-only"
                except Exception as e:
                    lo = f", listen-only failed ({e})"
            self.info = f"gs_usb #{channel or 0}, {bitrate // 1000} kbit/s{lo}"
        elif interface == "socketcan":
            self.bus = can.Bus(interface="socketcan", channel=channel or "can0")
            self.info = f"socketcan {channel or 'can0'} (bitrate / listen-only set by 'ip link')"
        else:
            self.bus = can.Bus(interface=interface, channel=channel, bitrate=bitrate)
            self.info = f"{interface} {channel}, {bitrate // 1000} kbit/s"

    def _conv(self, m) -> Frame | None:
        if m.is_error_frame:
            self.errors += 1
            return None
        return Frame(time.time(), m.arbitration_id, m.is_extended_id, m.is_remote_frame,
                     m.dlc, bytes(m.data[:8]) if not m.is_remote_frame else b"")

    def recv(self) -> list[Frame]:
        out = []
        m = self.bus.recv(0.05)
        while m is not None:
            f = self._conv(m)
            if f is not None:
                out.append(f)
            if len(out) >= 1000:
                break
            m = self.bus.recv(0)
        return out

    def send(self, f: Frame):
        self.bus.send(self._can.Message(
            arbitration_id=f.can_id, is_extended_id=f.ext, is_remote_frame=f.rtr,
            dlc=f.dlc, data=f.data))

    def close(self):
        self.bus.shutdown()


class DemoBackend:
    """Simulated vehicle-like bus; answers OBD-II requests sent to 7DF / 7E0."""

    def __init__(self, bitrate: int, listen_only: bool):
        self.errors = 0
        self.info = f"demo bus, {bitrate // 1000} kbit/s"
        self._t0 = time.time()
        self._lock = threading.Lock()
        self._pending: list[Frame] = []
        self._cnt = 0
        self._rpm = 800.0
        self._speed = 0.0
        self._doors = 0
        # id, ext, period s, next due
        self._sched = [[0x0C0, False, 0.010, 0], [0x1A0, False, 0.020, 0], [0x3E8, False, 0.100, 0],
                       [0x2F0, False, 0.050, 0], [0x18FEF100, True, 0.100, 0],
                       [0x0CF00400, True, 0.050, 0], [0x5A1, False, 1.000, 0]]

    def _payload(self, can_id: int, t: float) -> bytes:
        c = self._cnt & 0xFF
        if can_id == 0x0C0:        # engine: rpm, throttle, rolling counter, checksum
            self._rpm = 800 + 2500 * (0.5 + 0.5 * math.sin(t / 4))
            r = int(self._rpm * 4)
            b = bytes([r >> 8 & 0xFF, r & 0xFF, int(self._rpm / 40) & 0xFF, 0, 0, 0, c & 0x0F])
            return b + bytes([sum(b) & 0xFF])
        if can_id == 0x1A0:        # vehicle speed km/h * 100
            self._speed = max(0.0, 60 + 40 * math.sin(t / 7))
            v = int(self._speed * 100)
            return bytes([v >> 8, v & 0xFF, 0, 0, 0, 0])
        if can_id == 0x3E8:        # body: doors bitfield toggles now and then, lights
            if random.random() < 0.02:
                self._doors ^= 1 << random.randrange(4)
            return bytes([self._doors, 0x00, 0x80, 0x00])
        if can_id == 0x2F0:        # temperatures, slowly changing
            return bytes([90 + int(t / 10) % 5, 40, 25, 0xFF, 0xFF])
        if can_id == 0x18FEF100:   # J1939 CCVS: wheel speed
            v = int(self._speed * 256)
            return bytes([0xFF, v & 0xFF, v >> 8, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
        if can_id == 0x0CF00400:   # J1939 EEC1: engine speed 0.125 rpm/bit
            r = int(self._rpm * 8)
            return bytes([0xF0, 0x7D, 0x7D, r & 0xFF, r >> 8, 0xFF, 0xF0, 0xFF])
        return b"ALIVE" + bytes([c])

    def recv(self) -> list[Frame]:
        time.sleep(0.01)
        now = time.time()
        t = now - self._t0
        out = []
        for e in self._sched:
            if e[3] == 0:
                e[3] = now + random.random() * e[2]
            while e[3] <= now:
                self._cnt += 1
                out.append(Frame(e[3], e[0], e[1], False, None, self._payload(e[0], t)))
                e[3] += e[2]
        with self._lock:
            out += self._pending
            self._pending = []
        out.sort(key=lambda f: f.ts)
        return out

    def send(self, f: Frame):
        d = f.data
        if f.ext or f.can_id not in (0x7DF, 0x7E0) or len(d) < 3 or d[1] != 0x01:
            return
        pid = d[2]
        val = {0x0C: [int(self._rpm * 4) >> 8, int(self._rpm * 4) & 0xFF],
               0x0D: [int(self._speed)], 0x05: [90 + 40], 0x00: [0x18, 0x3B, 0x80, 0x00]}.get(pid)
        if val is None:
            resp = bytes([0x03, 0x7F, 0x01, 0x12, 0x55, 0x55, 0x55, 0x55])   # negative response
        else:
            resp = bytes([2 + len(val), 0x41, pid] + val).ljust(8, b"\x55")
        with self._lock:
            self._pending.append(Frame(time.time() + 0.002, 0x7E8, False, False, None, resp))

    def close(self):
        pass


def make_backend(kind: str, channel: str, bitrate: int, listen_only: bool):
    if kind == "slcan":
        if not channel:
            raise ValueError("no COM port selected")
        return SlcanBackend(channel, bitrate, listen_only)
    if kind in ("gs_usb", "socketcan"):
        return PyCanBackend(kind, channel, bitrate, listen_only)
    return DemoBackend(bitrate, listen_only)


def list_channels(kind: str) -> list[tuple[str, str]]:
    """(value, description) for the port/channel combo; CANable ports first."""
    if kind == "slcan":
        if serial is None:
            return []
        out = []
        for p in serial.tools.list_ports.comports():
            known = (p.vid, p.pid) in CANABLE_USB_IDS
            desc = p.description or ""
            if p.vid is not None:
                desc += f"  [{p.vid:04X}:{p.pid:04X}]"
            out.append((not known, p.device, ("★ " if known else "") + desc))
        return [(d, s) for _, d, s in sorted(out)]
    if kind == "gs_usb":
        try:
            from gs_usb.gs_usb import GsUsb
            devs = GsUsb.scan()
            return [(str(i), str(d)) for i, d in enumerate(devs)] or [("0", "no gs_usb device found")]
        except Exception as e:
            return [("0", f"scan failed: {e}")]
    if kind == "socketcan":
        try:
            names = sorted(n for n in os.listdir("/sys/class/net") if "can" in n)
        except OSError:
            names = []
        return [(n, "SocketCAN") for n in names] or [("can0", "SocketCAN")]
    return [("simulated", "demo")]


# --------------------------------------------------------------------------- #
#  Worker thread: owns the backend                                             #
# --------------------------------------------------------------------------- #
class CanWorker(QtCore.QThread):
    opened = QtCore.pyqtSignal(str)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, kind, channel, bitrate, listen_only):
        super().__init__()
        self.args = (kind, channel, bitrate, listen_only)
        self.backend = None
        self.frames: deque = deque()       # thread-safe append / popleft
        self._quit = False

    @property
    def errors(self) -> int:
        return self.backend.errors if self.backend else 0

    def run(self):
        try:
            self.backend = make_backend(*self.args)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")
            return
        self.opened.emit(self.backend.info)
        try:
            while not self._quit:
                frames = self.backend.recv()
                if frames:
                    self.frames.extend(frames)
        except Exception as e:
            if not self._quit:
                self.failed.emit(f"connection lost: {type(e).__name__}: {e}")
        finally:
            b, self.backend = self.backend, None
            try:
                b.close()
            except Exception:
                pass

    def send(self, f: Frame):
        b = self.backend
        if b is None:
            raise RuntimeError("not connected")
        b.send(f)
        f.tx = True
        f.ts = time.time()
        self.frames.append(f)

    def stop(self):
        self._quit = True
        self.wait(3000)


# --------------------------------------------------------------------------- #
#  Table models                                                                #
# --------------------------------------------------------------------------- #
C_TEXT = QtGui.QColor("#c9d1d9")
C_DIM = QtGui.QColor("#5c6570")
C_TX = QtGui.QColor("#58a6ff")
C_EXT = QtGui.QColor("#d2a8ff")
C_HL = QtGui.QColor("#c2410c")
C_BASE = QtGui.QColor("#161b22")
ALIGN_R = int(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
ALIGN_C = int(QtCore.Qt.AlignCenter)


class TraceModel(QtCore.QAbstractTableModel):
    COLS = ["#", "Time, s", "Δt, ms", "Dir", "ID", "Type", "DLC", "Data", "ASCII"]

    def __init__(self):
        super().__init__()
        self.rows: list[Frame] = []
        self.t0 = None
        self.abs_time = False

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return len(self.COLS)

    def headerData(self, s, orient, role=QtCore.Qt.DisplayRole):
        if role == QtCore.Qt.DisplayRole and orient == QtCore.Qt.Horizontal:
            return self.COLS[s]
        return None

    def time_str(self, f: Frame) -> str:
        if self.abs_time:
            return _dt.datetime.fromtimestamp(f.ts).strftime("%H:%M:%S.%f")[:-3]
        return f"{f.ts - (self.t0 or f.ts):.6f}"

    def text(self, f: Frame, c: int) -> str:
        if c == 0:
            return str(f.seq)
        if c == 1:
            return self.time_str(f)
        if c == 2:
            return "" if f.dt is None else f"{f.dt * 1000:.3f}"
        if c == 3:
            return "Tx" if f.tx else "Rx"
        if c == 4:
            return f.id_str()
        if c == 5:
            return f.type_str()
        if c == 6:
            return str(f.dlc)
        if c == 7:
            return f.data_str()
        return f.ascii_str()

    def data(self, idx, role=QtCore.Qt.DisplayRole):
        if not idx.isValid():
            return None
        f = self.rows[idx.row()]
        c = idx.column()
        if role == QtCore.Qt.DisplayRole:
            return self.text(f, c)
        if role == QtCore.Qt.ForegroundRole:
            if f.tx:
                return C_TX
            if c == 4 and f.ext:
                return C_EXT
            return None
        if role == QtCore.Qt.TextAlignmentRole and c in (0, 1, 2, 6):
            return ALIGN_R
        return None

    def append(self, frames: list[Frame]):
        if not frames:
            return
        if len(frames) >= TRACE_MAX:
            self.set_rows(frames[-TRACE_MAX:])
            return
        overflow = len(self.rows) + len(frames) - TRACE_MAX
        if overflow > 0:
            self.beginRemoveRows(QtCore.QModelIndex(), 0, overflow - 1)
            del self.rows[:overflow]
            self.endRemoveRows()
        n = len(self.rows)
        self.beginInsertRows(QtCore.QModelIndex(), n, n + len(frames) - 1)
        self.rows.extend(frames)
        self.endInsertRows()

    def set_rows(self, frames: list[Frame]):
        self.beginResetModel()
        self.rows = list(frames[-TRACE_MAX:])
        self.endResetModel()

    def refresh(self):
        if self.rows:
            self.dataChanged.emit(self.index(0, 1), self.index(len(self.rows) - 1, 1))


class MonEntry:
    __slots__ = ("frame", "count", "first_ts", "period", "changed", "ever_changed")

    def __init__(self, f: Frame):
        self.frame = f
        self.count = 1
        self.first_ts = f.ts
        self.period = None
        self.changed = [0.0] * 8
        self.ever_changed = [False] * 8


class MonitorModel(QtCore.QAbstractTableModel):
    COLS = ["ID", "Type", "DLC"] + [f"D{i}" for i in range(8)] + ["ASCII", "Count", "Period, ms", "Age, s"]
    BYTE0 = 3

    def __init__(self):
        super().__init__()
        self.keys: list[tuple] = []          # sorted (ext, id)
        self.entries: dict[tuple, MonEntry] = {}

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.keys)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return len(self.COLS)

    def headerData(self, s, orient, role=QtCore.Qt.DisplayRole):
        if role == QtCore.Qt.DisplayRole and orient == QtCore.Qt.Horizontal:
            return self.COLS[s]
        return None

    def entry(self, row: int) -> MonEntry:
        return self.entries[self.keys[row]]

    def data(self, idx, role=QtCore.Qt.DisplayRole):
        if not idx.isValid():
            return None
        e = self.entry(idx.row())
        f = e.frame
        c = idx.column()
        bi = c - self.BYTE0
        is_byte = 0 <= bi < 8
        if role == QtCore.Qt.DisplayRole:
            if c == 0:
                return f.id_str()
            if c == 1:
                return f.type_str()
            if c == 2:
                return str(f.dlc)
            if is_byte:
                return f"{f.data[bi]:02X}" if bi < len(f.data) else ""
            if c == 11:
                return f.ascii_str()
            if c == 12:
                return str(e.count)
            if c == 13:
                return "" if e.period is None else f"{e.period * 1000:.1f}"
            return f"{max(0.0, time.time() - f.ts):.1f}"
        if role == QtCore.Qt.BackgroundRole and is_byte and bi < len(f.data):
            age = time.time() - e.changed[bi]
            if age < HL_TIME:
                k = age / HL_TIME
                return QtGui.QColor(int(C_HL.red() * (1 - k) + C_BASE.red() * k),
                                    int(C_HL.green() * (1 - k) + C_BASE.green() * k),
                                    int(C_HL.blue() * (1 - k) + C_BASE.blue() * k))
            return None
        if role == QtCore.Qt.ForegroundRole:
            if is_byte and e.count > 1 and bi < 8 and not e.ever_changed[bi]:
                return C_DIM
            if c == 0:
                return C_TX if f.tx else (C_EXT if f.ext else None)
            if c == 14 and time.time() - f.ts > 2.0:
                return C_DIM
            return None
        if role == QtCore.Qt.TextAlignmentRole:
            if is_byte:
                return ALIGN_C
            if c in (2, 12, 13, 14):
                return ALIGN_R
        return None

    def update(self, frames: list[Frame]) -> bool:
        """Returns True if new IDs were added (rows inserted)."""
        added = False
        for f in frames:
            k = f.key
            e = self.entries.get(k)
            if e is None:
                pos = bisect.bisect_left(self.keys, k)
                self.beginInsertRows(QtCore.QModelIndex(), pos, pos)
                self.keys.insert(pos, k)
                self.entries[k] = MonEntry(f)
                self.endInsertRows()
                added = True
                continue
            old = e.frame
            if not f.rtr and not old.rtr:
                for i in range(min(8, len(f.data))):
                    if i >= len(old.data) or f.data[i] != old.data[i]:
                        e.changed[i] = f.ts
                        e.ever_changed[i] = True
            if f.ts > old.ts:
                dt = f.ts - old.ts
                e.period = dt if e.period is None else 0.9 * e.period + 0.1 * dt
            e.count += 1
            e.frame = f
        return added

    def refresh(self):
        if self.keys:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.keys) - 1, len(self.COLS) - 1))

    def clear(self):
        self.beginResetModel()
        self.keys, self.entries = [], {}
        self.endResetModel()


# --------------------------------------------------------------------------- #
#  Main window                                                                 #
# --------------------------------------------------------------------------- #
STYLE = """
    QWidget{background:#0f1216;color:#c9d1d9;}
    QGroupBox{border:1px solid #2a3038;border-radius:6px;margin-top:14px;padding-top:6px;font-weight:600;}
    QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 4px;color:#8a96a3;}
    QLineEdit,QSpinBox,QDoubleSpinBox,QComboBox,QPlainTextEdit,QTableView{background:#161b22;
        border:1px solid #30363d;border-radius:4px;padding:3px;}
    QTableView{gridline-color:#22272e;selection-background-color:#1f4d7a;padding:0;}
    QHeaderView::section{background:#161b22;color:#8a96a3;border:0;border-right:1px solid #22272e;padding:3px;}
    QTabWidget::pane{border:1px solid #2a3038;}
    QTabBar::tab{background:#161b22;border:1px solid #2a3038;padding:5px 14px;}
    QTabBar::tab:selected{background:#21262d;color:#ffffff;}
    QPushButton{background:#21262d;border:1px solid #30363d;border-radius:5px;padding:5px 10px;}
    QPushButton:hover{background:#2d333b;}
    QPushButton:checked{background:#1f6f43;border-color:#2ea043;}
    QPushButton#rec:checked{background:#8b1d1d;border-color:#f85149;}
    QPushButton:disabled,QLineEdit:disabled,QSpinBox:disabled,QDoubleSpinBox:disabled,
    QComboBox:disabled,QCheckBox:disabled{color:#4d5560;}
    QCheckBox::indicator{width:13px;height:13px;border:1px solid #6e7681;border-radius:3px;background:#161b22;}
    QCheckBox::indicator:checked{background:#2ea043;border-color:#2ea043;}
"""


def _mono() -> QtGui.QFont:
    f = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
    f.setPointSize(max(9, f.pointSize()))
    return f


def _table(model, widths) -> QtWidgets.QTableView:
    v = QtWidgets.QTableView()
    v.setModel(model)
    v.setFont(_mono())
    v.verticalHeader().hide()
    v.verticalHeader().setDefaultSectionSize(QtGui.QFontMetrics(_mono()).height() + 4)
    v.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
    v.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
    v.setWordWrap(False)
    v.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
    h = v.horizontalHeader()
    h.setStretchLastSection(True)
    h.setHighlightSections(False)
    for i, w in enumerate(widths):
        v.setColumnWidth(i, w)
    return v


class SnifferWindow(QtWidgets.QMainWindow):
    def __init__(self, args):
        super().__init__()
        self.setWindowTitle("CAN sniffer — CANable (CAN 2.0A/B)")
        self.resize(1400, 900)
        self.worker: CanWorker | None = None
        self.bitrate = DEFAULT_BITRATE
        self.raw: deque[Frame] = deque(maxlen=RAW_MAX)
        self.last_ts: dict[tuple, float] = {}
        self.seq = 0
        self.filter = None
        self.rec_file = None
        self.rec_count = 0
        self.n_rx = self.n_tx = 0
        self._win_bits = 0.0
        self._win_frames = 0
        self._win_t = time.monotonic()

        self._build_ui()
        self.setStyleSheet(STYLE)

        self.pump = QtCore.QTimer(self, interval=50, timeout=self._drain)
        self.pump.start()
        self.slow = QtCore.QTimer(self, interval=250, timeout=self._slow_tick)
        self.slow.start()
        self.periodic = QtCore.QTimer(self, timeout=self._send_periodic)

        if args.demo:
            self._select_iface("demo")
        if args.port:
            self._select_iface(args.interface or "slcan")
            self.port_cb.setEditText(args.port)
        elif args.interface:
            self._select_iface(args.interface)
        if args.bitrate:
            self.br_cb.setCurrentText(str(args.bitrate))
        if args.connect or args.demo:
            QtCore.QTimer.singleShot(100, self.toggle_connect)

    # ----------------------------------------------------------------- UI ---
    def _build_ui(self):
        central = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(central)

        # connection bar
        conn = QtWidgets.QGroupBox("Adapter")
        h = QtWidgets.QHBoxLayout(conn)
        self.iface_cb = QtWidgets.QComboBox()
        for key, label in INTERFACES:
            self.iface_cb.addItem(label, key)
        self.iface_cb.currentIndexChanged.connect(self.refresh_ports)
        self.port_cb = QtWidgets.QComboBox()
        self.port_cb.setEditable(True)
        self.port_cb.setMinimumWidth(150)
        self.port_cb.currentIndexChanged.connect(self._port_desc)
        self.port_desc = QtWidgets.QLabel()
        self.port_desc.setStyleSheet("color:#8a96a3")
        self.port_desc.setMinimumWidth(200)
        refresh = QtWidgets.QPushButton("⟳")
        refresh.setToolTip("Rescan ports / devices")
        refresh.setFixedWidth(34)
        refresh.clicked.connect(self.refresh_ports)
        self.br_cb = QtWidgets.QComboBox()
        self.br_cb.setEditable(True)
        for b, _ in BITRATES:
            self.br_cb.addItem(str(b))
        self.br_cb.setCurrentText(str(DEFAULT_BITRATE))
        self.listen_chk = QtWidgets.QCheckBox("Listen-only (no ACK)")
        self.listen_chk.setToolTip("Adapter stays passive on the bus.\n"
                                   "Turn OFF if the sniffer is the only other node on the bus.")
        self.conn_btn = QtWidgets.QPushButton("Connect")
        self.conn_btn.setCheckable(True)
        self.conn_btn.setMinimumWidth(110)
        self.conn_btn.clicked.connect(self.toggle_connect)
        self.status = QtWidgets.QLabel("● disconnected")
        self.status.setStyleSheet("color:#8a96a3")
        for w in (QtWidgets.QLabel("Interface"), self.iface_cb, QtWidgets.QLabel("Port"), self.port_cb,
                  refresh, self.port_desc, QtWidgets.QLabel("Bitrate"), self.br_cb, self.listen_chk,
                  self.conn_btn):
            h.addWidget(w)
        h.addWidget(self.status, 1)
        root.addWidget(conn)

        # view toolbar
        bar = QtWidgets.QHBoxLayout()
        self.filter_ed = QtWidgets.QLineEdit()
        self.filter_ed.setPlaceholderText("Filter IDs (hex):  100-1FF, 7E8, !7DF, 18FEF100, 700/700")
        self.filter_ed.setClearButtonEnabled(True)
        self.filter_ed.editingFinished.connect(self.apply_filter)
        self.filter_ed.textChanged.connect(lambda t: t or self.apply_filter())
        self.pause_btn = QtWidgets.QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.setToolTip("Freeze the Trace view; capture and recording continue")
        self.pause_btn.toggled.connect(self._pause_toggled)
        self.auto_chk = QtWidgets.QCheckBox("Autoscroll")
        self.auto_chk.setChecked(True)
        self.time_cb = QtWidgets.QComboBox()
        self.time_cb.addItems(["Relative time", "Absolute time"])
        self.time_cb.currentIndexChanged.connect(self._time_mode)
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(self.clear)
        self.rec_btn = QtWidgets.QPushButton("● Record")
        self.rec_btn.setObjectName("rec")
        self.rec_btn.setCheckable(True)
        self.rec_btn.setToolTip("Write all received/sent frames to a candump .log file")
        self.rec_btn.toggled.connect(self.toggle_record)
        save = QtWidgets.QPushButton("Save…")
        save.setToolTip("Save the frames currently in memory (CSV or candump .log)")
        save.clicked.connect(self.save_trace)
        load = QtWidgets.QPushButton("Open log…")
        load.clicked.connect(self.open_log)
        bar.addWidget(QtWidgets.QLabel("Filter"))
        bar.addWidget(self.filter_ed, 1)
        for w in (self.pause_btn, self.auto_chk, self.time_cb, clear, self.rec_btn, save, load):
            bar.addWidget(w)
        root.addLayout(bar)

        # views
        self.trace = TraceModel()
        self.mon = MonitorModel()
        self.trace_view = _table(self.trace, [80, 120, 90, 40, 100, 80, 40, 240])
        self.mon_view = _table(self.mon, [100, 70, 40] + [38] * 8 + [90, 80, 100, 70])
        self.mon_view.horizontalHeader().setStretchLastSection(False)
        self.mon_view.doubleClicked.connect(self._mon_to_tx)
        self.mon_view.setToolTip("Double-click a row to copy it into the Transmit panel")
        self.mon.rowsInserted.connect(lambda *_: self._apply_mon_filter())
        for v in (self.trace_view, self.mon_view):
            QtWidgets.QShortcut(QtGui.QKeySequence.Copy, v, activated=lambda v=v: self._copy_rows(v))
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.trace_view, "Trace")
        self.tabs.addTab(self.mon_view, "Monitor (by ID)")

        # bottom: transmit + stats + log
        tx = QtWidgets.QGroupBox("Transmit")
        g = QtWidgets.QGridLayout(tx)
        self.tx_id = QtWidgets.QLineEdit("7DF")
        self.tx_id.setFont(_mono())
        self.tx_id.setMaximumWidth(110)
        self.tx_ext = QtWidgets.QCheckBox("Extended (29-bit)")
        self.tx_rtr = QtWidgets.QCheckBox("RTR")
        self.tx_data = QtWidgets.QLineEdit("02 01 0C 00 00 00 00 00")
        self.tx_data.setFont(_mono())
        self.tx_dlc = QtWidgets.QSpinBox()
        self.tx_dlc.setRange(0, 8)
        self.tx_dlc.setToolTip("DLC for remote frames (data frames use the data length)")
        self.tx_send = QtWidgets.QPushButton("Send")
        self.tx_send.clicked.connect(self.send_once)
        self.tx_period = QtWidgets.QSpinBox()
        self.tx_period.setRange(1, 60000)
        self.tx_period.setValue(100)
        self.tx_period.setSuffix(" ms")
        self.tx_cyc = QtWidgets.QPushButton("Periodic")
        self.tx_cyc.setCheckable(True)
        self.tx_cyc.toggled.connect(self.toggle_periodic)
        self.tx_data.returnPressed.connect(self.send_once)
        g.addWidget(QtWidgets.QLabel("ID (hex)"), 0, 0)
        g.addWidget(self.tx_id, 0, 1)
        g.addWidget(self.tx_ext, 0, 2)
        g.addWidget(self.tx_rtr, 0, 3)
        g.addWidget(QtWidgets.QLabel("RTR DLC"), 0, 4)
        g.addWidget(self.tx_dlc, 0, 5)
        g.addWidget(QtWidgets.QLabel("Data (hex)"), 1, 0)
        g.addWidget(self.tx_data, 1, 1, 1, 5)
        g.addWidget(self.tx_send, 2, 0, 1, 2)
        g.addWidget(self.tx_period, 2, 2, 1, 2)
        g.addWidget(self.tx_cyc, 2, 4, 1, 2)
        tx.setMaximumWidth(560)

        st = QtWidgets.QGroupBox("Bus statistics")
        sg = QtWidgets.QFormLayout(st)
        self.st_lbl = {}
        for k in ("Rx frames", "Tx frames", "Frames/s", "Bus load", "IDs", "Errors", "Recorded"):
            lbl = QtWidgets.QLabel("0")
            lbl.setFont(_mono())
            self.st_lbl[k] = lbl
            sg.addRow(k, lbl)
        st.setMinimumWidth(220)

        self.logbox = QtWidgets.QPlainTextEdit()
        self.logbox.setReadOnly(True)
        bottom = QtWidgets.QWidget()
        bh = QtWidgets.QHBoxLayout(bottom)
        bh.setContentsMargins(0, 0, 0, 0)
        bh.addWidget(tx)
        bh.addWidget(st)
        bh.addWidget(self.logbox, 1)

        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        split.addWidget(self.tabs)
        split.addWidget(bottom)
        split.setStretchFactor(0, 1)
        split.setSizes([650, 210])
        root.addWidget(split, 1)
        self.setCentralWidget(central)
        self.refresh_ports()

    def _select_iface(self, key):
        i = self.iface_cb.findData(key)
        if i >= 0:
            self.iface_cb.setCurrentIndex(i)

    def refresh_ports(self):
        kind = self.iface_cb.currentData()
        cur = self.port_cb.currentText()
        self.port_cb.blockSignals(True)
        self.port_cb.clear()
        for val, desc in list_channels(kind):
            self.port_cb.addItem(val, desc)
        if cur and self.port_cb.findText(cur) >= 0:
            self.port_cb.setCurrentText(cur)
        self.port_cb.blockSignals(False)
        self.port_cb.setEnabled(kind != "demo")
        self.br_cb.setEnabled(kind != "socketcan")
        self._port_desc()

    def _port_desc(self, *_):
        i = self.port_cb.currentIndex()
        desc = self.port_cb.itemData(i) if i >= 0 else ""
        if not desc and self.iface_cb.currentData() == "slcan":
            desc = "no COM ports found — plug in the CANable"
        self.port_desc.setText(desc or "")

    def logmsg(self, text, level="info"):
        color = {"info": "#c9d1d9", "ok": "#4fd18b", "warn": "#f0b03e", "err": "#ff6b6b"}[level]
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        self.logbox.appendHtml(f'<span style="color:#6e7681">{ts}</span> <span style="color:{color}">{text}</span>')

    # --------------------------------------------------------- connection ---
    def toggle_connect(self):
        if self.worker is not None:
            self.disconnect_bus()
            return
        kind = self.iface_cb.currentData()
        try:
            self.bitrate = int(self.br_cb.currentText().replace("_", "").replace(" ", ""))
        except ValueError:
            self.logmsg("bad bitrate", "err")
            self.conn_btn.setChecked(False)
            return
        if kind == "slcan" and self.bitrate not in dict(BITRATES):
            self.logmsg(f"slcan firmware supports only {', '.join(str(b) for b, _ in BITRATES)} bit/s", "err")
            self.conn_btn.setChecked(False)
            return
        self.worker = CanWorker(kind, self.port_cb.currentText().strip(), self.bitrate, self.listen_chk.isChecked())
        self.worker.opened.connect(self._on_opened)
        self.worker.failed.connect(lambda m: self.logmsg(m, "err"))
        self.worker.finished.connect(self._on_worker_finished)
        self._set_inputs_enabled(False)
        self.conn_btn.setChecked(True)
        self.conn_btn.setText("Connecting…")
        self.status.setText("● connecting…")
        self.status.setStyleSheet("color:#f0b03e")
        if self.trace.t0 is None:
            self.trace.t0 = time.time()
        self.worker.start()

    def _on_opened(self, info):
        self.conn_btn.setText("Disconnect")
        self.status.setText(f"● {info}")
        self.status.setStyleSheet("color:#4fd18b")
        self.logmsg(f"connected: {info}", "ok")

    def _on_worker_finished(self):
        w, self.worker = self.worker, None
        if w is not None:
            self._drain(w)
            w.deleteLater()
        self.tx_cyc.setChecked(False)
        self._set_inputs_enabled(True)
        self.conn_btn.setChecked(False)
        self.conn_btn.setText("Connect")
        self.status.setText("● disconnected")
        self.status.setStyleSheet("color:#8a96a3")

    def disconnect_bus(self):
        w = self.worker
        if w is None:
            return
        self.tx_cyc.setChecked(False)
        w.stop()
        self.logmsg("disconnected")

    def _set_inputs_enabled(self, on):
        for w in (self.iface_cb, self.port_cb, self.br_cb, self.listen_chk):
            w.setEnabled(on)
        if on:
            self.refresh_ports()

    # ------------------------------------------------------------ ingest ---
    def _drain(self, worker=None):
        w = worker or self.worker
        if w is None:
            return
        q = w.frames
        frames = []
        for _ in range(min(len(q), 50_000)):
            frames.append(q.popleft())
        if frames:
            self.ingest(frames)

    def ingest(self, frames: list[Frame], live=True):
        for f in frames:
            self.seq += 1
            f.seq = self.seq
            k = f.key
            prev = self.last_ts.get(k)
            f.dt = None if prev is None else f.ts - prev
            self.last_ts[k] = f.ts
            if f.tx:
                self.n_tx += 1
            else:
                self.n_rx += 1
            self._win_bits += f.bits()
            self._win_frames += 1
        if self.trace.t0 is None:
            self.trace.t0 = frames[0].ts
        self.raw.extend(frames)
        if live and self.rec_file is not None:
            self.rec_file.write("".join(to_candump(f) for f in frames))
            self.rec_count += len(frames)
        self.mon.update(frames)
        if not self.pause_btn.isChecked():
            shown = frames if self.filter is None else [f for f in frames if self.filter(f.can_id)]
            self.trace.append(shown)
            if shown and self.auto_chk.isChecked():
                self.trace_view.scrollToBottom()

    def _slow_tick(self):
        now = time.monotonic()
        dt = max(1e-3, now - self._win_t)
        if dt >= 1.0:
            fps = self._win_frames / dt
            load = 100.0 * self._win_bits / dt / max(1, self.bitrate)
            self.st_lbl["Frames/s"].setText(f"{fps:,.0f}")
            self.st_lbl["Bus load"].setText(f"≈ {load:.1f} %" if self.worker else "—")
            self._win_bits, self._win_frames, self._win_t = 0.0, 0, now
        self.st_lbl["Rx frames"].setText(f"{self.n_rx:,}")
        self.st_lbl["Tx frames"].setText(f"{self.n_tx:,}")
        self.st_lbl["IDs"].setText(str(len(self.mon.keys)))
        self.st_lbl["Errors"].setText(str(self.worker.errors if self.worker else 0))
        self.st_lbl["Recorded"].setText(f"{self.rec_count:,}" if self.rec_file else "off")
        if self.rec_file is not None:
            self.rec_file.flush()
        if self.tabs.currentIndex() == 1:
            self.mon.refresh()

    # -------------------------------------------------------------- view ---
    def apply_filter(self):
        text = self.filter_ed.text()
        try:
            self.filter = parse_filter(text)
        except ValueError:
            self.filter_ed.setStyleSheet("border-color:#f85149")
            self.logmsg(f"bad filter: {text!r}", "err")
            return
        self.filter_ed.setStyleSheet("")
        self._rebuild_trace()
        self._apply_mon_filter()

    def _apply_mon_filter(self):
        for r, (_, cid) in enumerate(self.mon.keys):
            self.mon_view.setRowHidden(r, self.filter is not None and not self.filter(cid))

    def _rebuild_trace(self):
        frames = self.raw if self.filter is None else [f for f in self.raw if self.filter(f.can_id)]
        self.trace.set_rows(list(frames)[-TRACE_MAX:])
        if self.auto_chk.isChecked():
            self.trace_view.scrollToBottom()

    def _pause_toggled(self, on):
        self.pause_btn.setText("Resume" if on else "Pause")
        if not on:
            self._rebuild_trace()

    def _time_mode(self, i):
        self.trace.abs_time = i == 1
        self.trace.refresh()

    def clear(self):
        self.raw.clear()
        self.last_ts.clear()
        self.trace.set_rows([])
        self.trace.t0 = time.time() if self.worker else None
        self.mon.clear()
        self.seq = self.n_rx = self.n_tx = 0

    def _copy_rows(self, view):
        rows = sorted({i.row() for i in view.selectionModel().selectedRows()})
        if not rows:
            return
        m = view.model()
        lines = ["\t".join(m.COLS)]
        for r in rows:
            lines.append("\t".join(str(m.data(m.index(r, c)) or "") for c in range(m.columnCount())))
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))

    def _mon_to_tx(self, idx):
        f = self.mon.entry(idx.row()).frame
        self.tx_id.setText(f.id_str())
        self.tx_ext.setChecked(f.ext)
        self.tx_rtr.setChecked(f.rtr)
        self.tx_dlc.setValue(f.dlc)
        self.tx_data.setText(f.data_str())

    # ---------------------------------------------------------- transmit ---
    def _tx_frame(self) -> Frame:
        txt = self.tx_id.text().strip().upper().removeprefix("0X")
        can_id = int(txt, 16)
        ext = self.tx_ext.isChecked()
        if not ext and can_id > 0x7FF:
            ext = True
            self.tx_ext.setChecked(True)
        if can_id > 0x1FFFFFFF:
            raise ValueError("ID exceeds 29 bits")
        if self.tx_rtr.isChecked():
            return Frame(0, can_id, ext, True, self.tx_dlc.value())
        return Frame(0, can_id, ext, False, None, parse_hex_bytes(self.tx_data.text()))

    def send_once(self) -> bool:
        if self.worker is None or self.worker.backend is None:
            self.logmsg("not connected", "warn")
            return False
        try:
            self.worker.send(self._tx_frame())
            return True
        except Exception as e:
            self.logmsg(f"send failed: {type(e).__name__}: {e}", "err")
            return False

    def toggle_periodic(self, on):
        if on:
            try:
                self._tx_frame()
            except Exception as e:
                self.logmsg(f"bad frame: {e}", "err")
                self.tx_cyc.setChecked(False)
                return
            self.periodic.start(self.tx_period.value())
        else:
            self.periodic.stop()

    def _send_periodic(self):
        self.periodic.setInterval(self.tx_period.value())
        if not self.send_once():
            self.tx_cyc.setChecked(False)

    # ------------------------------------------------------------- files ---
    def toggle_record(self, on):
        if on:
            name = _dt.datetime.now().strftime("can_log_%Y%m%d_%H%M%S.log")
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Record to", os.path.join(script_dir(), name), "candump log (*.log);;All files (*)")
            if not path:
                self.rec_btn.setChecked(False)
                return
            try:
                self.rec_file = open(path, "w", encoding="ascii", newline="\n")
            except OSError as e:
                self.logmsg(f"cannot open {path}: {e}", "err")
                self.rec_btn.setChecked(False)
                return
            self.rec_count = 0
            self.rec_btn.setText("■ Stop rec")
            self.logmsg(f"recording to {path}", "ok")
        else:
            if self.rec_file is not None:
                self.rec_file.close()
                self.logmsg(f"recording stopped, {self.rec_count:,} frames written")
            self.rec_file = None
            self.rec_btn.setText("● Record")

    def save_trace(self):
        if not self.raw:
            self.logmsg("nothing to save", "warn")
            return
        name = _dt.datetime.now().strftime("can_trace_%Y%m%d_%H%M%S")
        path, flt = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save trace", os.path.join(script_dir(), name + ".csv"),
            "CSV (*.csv);;candump log (*.log)")
        if not path:
            return
        frames = [f for f in self.raw if self.filter is None or self.filter(f.can_id)]
        try:
            if path.lower().endswith(".log") or "candump" in flt:
                with open(path, "w", encoding="ascii", newline="\n") as fh:
                    fh.writelines(to_candump(f) for f in frames)
            else:
                t0 = self.trace.t0 or (frames[0].ts if frames else 0)
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    w = csv.writer(fh)
                    w.writerow(["seq", "time", "time_rel_s", "dt_ms", "dir", "id", "type", "dlc", "data", "ascii"])
                    for f in frames:
                        w.writerow([f.seq, _dt.datetime.fromtimestamp(f.ts).isoformat(timespec="microseconds"),
                                    f"{f.ts - t0:.6f}", "" if f.dt is None else f"{f.dt * 1000:.3f}",
                                    "Tx" if f.tx else "Rx", f.id_str(), f.type_str(), f.dlc,
                                    f.data_str(), f.ascii_str()])
        except OSError as e:
            self.logmsg(f"save failed: {e}", "err")
            return
        self.logmsg(f"saved {len(frames):,} frames to {path}", "ok")

    def open_log(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open candump log", script_dir(), "candump log (*.log *.txt);;All files (*)")
        if path:
            self.load_log(path)

    def load_log(self, path):
        frames, bad = [], 0
        try:
            with open(path, encoding="ascii", errors="replace") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    f = parse_candump(line)
                    if f is None:
                        bad += 1
                    else:
                        frames.append(f)
        except OSError as e:
            self.logmsg(f"cannot read {path}: {e}", "err")
            return
        if not frames:
            self.logmsg(f"no frames found in {path}", "warn")
            return
        self.clear()
        self.trace.t0 = frames[0].ts
        self.ingest(frames, live=False)
        self._rebuild_trace()
        self.logmsg(f"loaded {len(frames):,} frames from {os.path.basename(path)}"
                    + (f" ({bad} unparsable lines skipped)" if bad else ""), "ok")

    def closeEvent(self, ev):
        self.periodic.stop()
        if self.worker is not None:
            self.worker.stop()
        if self.rec_file is not None:
            self.rec_file.close()
            self.rec_file = None
        ev.accept()


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="CAN 2.0A/B sniffer for the CANable adapter")
    ap.add_argument("--interface", choices=[k for k, _ in INTERFACES], help="adapter interface")
    ap.add_argument("--port", help="COM port (slcan), device index (gs_usb) or can0 (socketcan)")
    ap.add_argument("--bitrate", type=int, help="bit/s, default 500000")
    ap.add_argument("--connect", action="store_true", help="connect on start-up")
    ap.add_argument("--demo", action="store_true", help="simulated bus, no hardware needed")
    ap.add_argument("--log", help="open a candump .log file on start-up")
    args = ap.parse_args()

    set_windows_app_id()
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    icon = QtGui.QIcon(resource_path(ICON_FILE))
    if not icon.isNull():
        app.setWindowIcon(icon)
    win = SnifferWindow(args)
    if args.log:
        win.load_log(args.log)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
