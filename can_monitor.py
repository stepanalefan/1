#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
can_monitor.py
==============
CAN bus monitor for Marathon CAN-bus-USB / CAN-bus-USBnp adapters (CHAI library).

    python can_monitor.py              # real adapter
    python can_monitor.py --demo       # simulated bus, no hardware needed
    python can_monitor.py --lib "C:\\Program Files (x86)\\CHAI-2.14.0\\...\\chai.dll" --connect

Driver
------
* Install Marathon's CHAI package (CHAI 2.x) and update the adapter's driver in
  Device Manager as the CHAI docs describe. The installer puts chai.dll into the
  Windows system folder (libchai.so on Linux). The library is searched for in
  this script's folder, the system folders and C:\\Program Files*\\CHAI*\\...;
  or give it with --lib / the CHAI_LIB environment variable.
* Python and chai.dll must have the same bitness: 64-bit Python needs the x64
  DLL, otherwise Windows says "not a valid Win32 application".
* The channel list (CHAI channel numbers) is read from the boards at start-up.
  If no board is found you can type the channel number into the box.

CHAI calls used (chai.h): CiInit, CiGetLibVer, CiBoardInfo, CiOpen, CiSetBaud,
CiSetLom, CiStart, CiWaitEvent, CiRead, CiWrite, CiErrsGetClear, CiStop, CiClose.
Bit rates are SJA1000 BTR0/BTR1 pairs (16 MHz), the same values as chai.h's
BCI_xxx macros; "Custom" takes any pair.

Window
------
* Monitor: one row per CAN ID with the latest data (changed bytes light up),
  cycle time and count. Double-click a row to copy it into the Transmit list.
* Trace: every frame in arrival order (last 50 000 kept). Ctrl+C copies rows.
* Filter: hex IDs and ranges, "!" excludes, e.g.  100-1FF, 7E8, !123
* Transmit: frames to send once ("Send selected") or cyclically (tick "Cyclic").
* Record: writes every received and sent frame to a CSV file.
* Listen-only: the adapter neither ACKs nor sends error frames. Use it on a live
  network, especially while you are not sure about the bit rate.

Install:  pip install PyQt5
"""

from __future__ import annotations

import argparse
import bisect
import csv
import ctypes
import ctypes.util
import datetime as _dt
import glob
import json
import math
import os
import re
import sys
import time
from collections import deque
from typing import NamedTuple

from PyQt5 import QtCore, QtGui, QtWidgets


# --------------------------------------------------------------------------- #
#  CHAI (chai.h)                                                               #
# --------------------------------------------------------------------------- #
CIO_CAN11 = 0x2         # CiOpen flags: 11-bit (standard) frames
CIO_CAN29 = 0x4         #               29-bit (extended) frames
CI_WAIT_RC = 0x1        # CiWaitEvent: frame received
CI_WAIT_ER = 0x4        #              CAN error
FRAME_RTR = 0x1         # canmsg_t.flags bit 0
FRAME_EFF = 0x4         # canmsg_t.flags bit 2: 29-bit identifier
CI_BRD_NUMS = 8
ECIINTR = 11

# name, BTR0, BTR1  (= chai.h BCI_xxx_bt0 / BCI_xxx_bt1)
BITRATES = [
    ("1 Mbit/s", 0x00, 0x14),
    ("800 kbit/s", 0x00, 0x16),
    ("500 kbit/s", 0x00, 0x1C),
    ("250 kbit/s", 0x01, 0x1C),
    ("125 kbit/s", 0x03, 0x1C),
    ("100 kbit/s", 0x04, 0x1C),
    ("50 kbit/s", 0x09, 0x1C),
    ("20 kbit/s", 0x18, 0x1C),
    ("10 kbit/s", 0x31, 0x1C),
]
DEFAULT_BITRATE = 2     # 500 kbit/s

CI_ERRORS = {
    1: "ECIGEN (generic error)", 2: "ECIBUSY (device or resource busy)",
    3: "ECIMFAULT (memory fault)", 4: "ECISTATE (not allowed in the current state)",
    5: "ECIINCALL (invalid call)", 6: "ECIINVAL (invalid argument)",
    7: "ECIACCES (access denied)", 8: "ECINOSYS (not supported)",
    9: "ECIIO (input/output error)", 10: "ECINODEV (no such device)",
    11: "ECIINTR (interrupted)", 12: "ECINORES (no resources)", 13: "ECITOUT (timeout)",
}

# canerrs_t field -> label
ERR_FIELDS = [("ewl", "EWL"), ("boff", "Bus-off"), ("hwovr", "HW ovr"),
              ("swovr", "SW ovr"), ("wtout", "Wr tout")]


class canmsg_t(ctypes.Structure):
    # ts: microseconds since the channel was opened, wraps after ~71.6 min
    _fields_ = [("id", ctypes.c_uint32), ("data", ctypes.c_uint8 * 8), ("len", ctypes.c_uint8),
                ("flags", ctypes.c_uint16), ("ts", ctypes.c_uint32)]


class canwait_t(ctypes.Structure):
    _fields_ = [("chan", ctypes.c_uint8), ("wflags", ctypes.c_uint8), ("rflags", ctypes.c_uint8)]


class canerrs_t(ctypes.Structure):
    _fields_ = [("ewl", ctypes.c_uint16), ("boff", ctypes.c_uint16), ("hwovr", ctypes.c_uint16),
                ("swovr", ctypes.c_uint16), ("wtout", ctypes.c_uint16),
                ("_spare", ctypes.c_uint16 * 16)]       # headroom in case a CHAI version adds fields


class canboard_t(ctypes.Structure):
    _fields_ = [("brdnum", ctypes.c_int16), ("hwver", ctypes.c_uint32), ("chip", ctypes.c_int16 * 4),
                ("name", ctypes.c_char * 64), ("manufact", ctypes.c_char * 64),
                ("_spare", ctypes.c_uint8 * 64)]


class ChaiError(RuntimeError):
    pass


def ci_strerror(rc: int) -> str:
    return CI_ERRORS.get(-rc, f"error {rc}")


def btr_bitrate(bt0: int, bt1: int, fosc: float = 16e6) -> float:
    """Bit rate of an SJA1000 BTR0/BTR1 pair (CHAI's convention: 16 MHz clock)."""
    brp = (bt0 & 0x3F) + 1
    tq = 1 + ((bt1 & 0x0F) + 1) + (((bt1 >> 4) & 0x07) + 1)
    return fosc / (2 * brp * tq)


def script_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _pe_bits(path: str):
    """32 or 64 for a Windows DLL, None if it can't be told."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        pe = int.from_bytes(head[0x3C:0x40], "little")
        if head[pe:pe + 4] != b"PE\0\0":
            return None
        machine = int.from_bytes(head[pe + 4:pe + 6], "little")
    except OSError:
        return None
    return {0x14C: 32, 0x8664: 64, 0xAA64: 64}.get(machine)


def chai_candidates(explicit=None) -> list[str]:
    if explicit:
        return [explicit]
    names = []
    if os.environ.get("CHAI_LIB"):
        names.append(os.environ["CHAI_LIB"])
    if sys.platform == "win32":
        names += [os.path.join(script_dir(), "chai.dll"), "chai.dll"]
        found = ctypes.util.find_library("chai")
        if found:
            names.append(found)
        roots = {os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")}
        for root in sorted(r for r in roots if r):
            names += sorted(glob.glob(os.path.join(root, "CHAI*", "**", "chai*.dll"), recursive=True),
                            reverse=True)
    else:
        names += [os.path.join(script_dir(), "libchai.so"), "libchai.so",
                  "/usr/local/lib/libchai.so", "/usr/lib/libchai.so"]
    return list(dict.fromkeys(names))


class Chai:
    """The CHAI calls the monitor needs, via ctypes."""

    def __init__(self, path=None):
        bits = 8 * ctypes.sizeof(ctypes.c_void_p)
        tried = []
        for name in chai_candidates(path):
            if sys.platform == "win32" and os.path.isfile(name):
                b = _pe_bits(name)
                if b and b != bits:
                    tried.append(f"{name}: {b}-bit DLL, but Python is {bits}-bit")
                    continue
            try:
                dll = ctypes.CDLL(name)
                dll.CiInit
            except (OSError, AttributeError) as e:
                tried.append(f"{name}: {e}")
                continue
            self.path = name
            break
        else:
            raise ChaiError("CHAI library not found - install Marathon's CHAI driver package "
                            "or give the DLL with --lib.\n  " + "\n  ".join(tried))
        self._bind(dll)
        rc = self.CiInit()
        if rc < 0:
            raise ChaiError(f"CiInit: {ci_strerror(rc)}")
        if sys.platform == "win32" and bits == 32 and self.CiBoardInfo is not None:
            # chai.dll's calling convention isn't documented. On 32-bit Windows
            # ctypes reports a mismatch as ValueError, so probe a harmless call.
            try:
                self.CiBoardInfo(ctypes.byref(canboard_t()))
            except ValueError:
                self._bind(ctypes.WinDLL(self.path))

    def _bind(self, dll):
        u8, s16, u32, P = ctypes.c_uint8, ctypes.c_int16, ctypes.c_uint32, ctypes.POINTER
        for name, res, args in (
            ("CiInit", s16, []),
            ("CiGetLibVer", u32, []),
            ("CiBoardInfo", s16, [P(canboard_t)]),
            ("CiOpen", s16, [u8, u8]),
            ("CiClose", s16, [u8]),
            ("CiSetBaud", s16, [u8, u8, u8]),
            ("CiSetLom", s16, [u8, u8]),
            ("CiStart", s16, [u8]),
            ("CiStop", s16, [u8]),
            ("CiWaitEvent", s16, [P(canwait_t), ctypes.c_int, ctypes.c_int]),
            ("CiRead", s16, [u8, P(canmsg_t), s16]),
            ("CiWrite", s16, [u8, P(canmsg_t), s16]),
            ("CiErrsGetClear", s16, [u8, P(canerrs_t)]),
        ):
            fn = getattr(dll, name, None)       # None: not exported by this CHAI version
            if fn is not None:
                fn.restype, fn.argtypes = res, args
            setattr(self, name, fn)

    def check(self, name: str, *args) -> int:
        fn = getattr(self, name)
        if fn is None:
            raise ChaiError(f"{name} is not exported by {self.path} (CHAI too old?)")
        rc = fn(*args)
        if rc < 0:
            raise ChaiError(f"{name}: {ci_strerror(rc)}")
        return rc

    def version(self) -> str:
        if self.CiGetLibVer is None:
            return "?"
        v = self.CiGetLibVer()
        return f"{v >> 16}.{(v >> 8) & 0xFF}.{v & 0xFF}"

    def channels(self) -> list[tuple[int, str]]:
        """[(CHAI channel number, label)] for every board CiBoardInfo knows."""
        out = []
        if self.CiBoardInfo is None:
            return out
        for b in range(CI_BRD_NUMS):
            info = canboard_t(brdnum=b)
            if self.CiBoardInfo(ctypes.byref(info)) < 0:
                continue
            name = info.name.decode("cp1251", "replace").strip() or "CAN board"
            for chip in info.chip:
                if 0 <= chip < 64:
                    out.append((chip, f"{chip}: {name} (board {b})"))
        return out


# --------------------------------------------------------------------------- #
#  Frames and buses                                                            #
# --------------------------------------------------------------------------- #
class Frame(NamedTuple):
    t: float            # seconds since connect
    rx: bool            # False: sent by us
    id: int
    ext: bool           # 29-bit identifier
    rtr: bool
    dlc: int
    data: bytes


def fmt_id(f: Frame) -> str:
    return f"{f.id:08X}" if f.ext else f"{f.id:03X}"


def fmt_type(f: Frame) -> str:
    return ("EXT" if f.ext else "STD") + (" RTR" if f.rtr else "")


def fmt_data(f: Frame) -> str:
    return f.data.hex(" ").upper()


def fmt_ascii(f: Frame) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in f.data)


def frame_bits(f: Frame) -> int:
    """Bits on the wire without stuffing (incl. 3-bit interframe space)."""
    return (67 if f.ext else 47) + (0 if f.rtr else 8 * f.dlc)


def frame_row(f: Frame) -> list:
    return [f"{f.t:.6f}", "Rx" if f.rx else "Tx", fmt_id(f), fmt_type(f), f.dlc, fmt_data(f)]


class ChaiBus:
    """One CHAI channel. All calls are made from the worker thread."""

    def __init__(self, lib: Chai, chan: int, bt0: int, bt1: int,
                 std=True, ext=True, listen_only=False):
        self.lib, self.chan, self.bt0, self.bt1 = lib, chan, bt0, bt1
        self.std, self.ext, self.listen_only = std, ext, listen_only
        self.t0 = 0.0

    def open(self) -> str:
        lib, ch = self.lib, self.chan
        lib.check("CiOpen", ch, (CIO_CAN11 if self.std else 0) | (CIO_CAN29 if self.ext else 0))
        self.t0 = time.perf_counter()
        try:
            lib.check("CiSetBaud", ch, self.bt0, self.bt1)
            if self.listen_only:
                lib.check("CiSetLom", ch, 1)
            lib.check("CiStart", ch)
        except Exception:
            lib.CiClose(ch)
            raise
        self.buf = (canmsg_t * 64)()
        self.cw = canwait_t(chan=ch, wflags=CI_WAIT_RC | CI_WAIT_ER)
        self._ts_prev = 0
        self._ts_base = 0
        self._ts_offset = None
        return f"channel {ch} open" + (" (listen-only)" if self.listen_only else "")

    def close(self):
        for fn in (self.lib.CiStop, self.lib.CiClose):
            try:
                fn(self.chan)
            except Exception:
                pass

    def wait(self, timeout_ms: int) -> int:
        """Block until a frame/error arrives or timeout; returns CI_WAIT_* flags."""
        if self.lib.CiWaitEvent is None:
            time.sleep(timeout_ms / 1000)
            return 0
        self.cw.rflags = 0
        rc = self.lib.CiWaitEvent(ctypes.byref(self.cw), 1, timeout_ms)
        if rc < 0 and rc != -ECIINTR:
            raise ChaiError(f"CiWaitEvent: {ci_strerror(rc)}")
        return self.cw.rflags if rc > 0 else 0

    def read(self) -> list[Frame]:
        out = []
        while True:
            n = self.lib.CiRead(self.chan, self.buf, len(self.buf))
            if n < 0:
                raise ChaiError(f"CiRead: {ci_strerror(n)}")
            for m in self.buf[:n]:
                if m.ts or self._ts_offset is not None:
                    if self._ts_offset is None:
                        # Hardware clock (us, wraps at 2^32) anchored to the host clock
                        # at the first frame, so Rx and Tx share one time axis.
                        self._ts_offset = time.perf_counter() - self.t0 - m.ts * 1e-6
                    elif m.ts < self._ts_prev and self._ts_prev - m.ts > 0x80000000:
                        self._ts_base += 1 << 32
                    self._ts_prev = m.ts
                    t = (self._ts_base + m.ts) * 1e-6 + self._ts_offset
                else:               # adapter without timestamps
                    t = time.perf_counter() - self.t0
                ext, rtr = bool(m.flags & FRAME_EFF), bool(m.flags & FRAME_RTR)
                dlc = min(m.len, 8)
                out.append(Frame(t, True, m.id & (0x1FFFFFFF if ext else 0x7FF), ext, rtr, dlc,
                                 b"" if rtr else bytes(m.data)[:dlc]))
            if n < len(self.buf):
                return out

    def write(self, f: Frame):
        m = canmsg_t()
        m.id = f.id
        m.flags = (FRAME_EFF if f.ext else 0) | (FRAME_RTR if f.rtr else 0)
        m.len = f.dlc
        m.data[:len(f.data)] = f.data
        rc = self.lib.CiWrite(self.chan, ctypes.byref(m), 1)
        if rc < 0:
            raise ChaiError(f"CiWrite: {ci_strerror(rc)}")
        if rc == 0:
            raise ChaiError("CiWrite: transmit queue full (no ACK on the bus?)")

    def errors(self) -> dict:
        """Error counters since the last call (CiErrsGetClear clears them)."""
        if self.lib.CiErrsGetClear is None:
            return {}
        e = canerrs_t()
        if self.lib.CiErrsGetClear(self.chan, ctypes.byref(e)) < 0:
            return {}
        return {k: getattr(e, k) for k, _ in ERR_FIELDS}


class _SimNode:
    __slots__ = ("id", "ext", "period", "due", "fn", "n")

    def __init__(self, cid, ext, period, fn):
        self.id, self.ext, self.period, self.fn = cid, ext, period, fn
        self.due, self.n = 0.0, 0


class DemoBus:
    """Simulated bus: a few periodic ECUs plus an OBD-II responder (0x7DF -> 0x7E8)."""

    def __init__(self, listen_only=False):
        self.listen_only = listen_only
        self.t0 = 0.0

    def open(self) -> str:
        self.t0 = time.perf_counter()
        self.nodes = [
            _SimNode(0x0C0, False, 0.010, self._engine),
            _SimNode(0x1A0, False, 0.020, self._wheels),
            _SimNode(0x3E8, False, 0.100, self._temps),
            _SimNode(0x18FEF100, True, 0.100, self._ccvs),
            _SimNode(0x501, False, 1.000, self._status),
        ]
        self.pending: list[Frame] = []
        return "demo bus (simulated traffic; try sending 7DF: 02 01 0C)"

    def close(self):
        pass

    def wait(self, timeout_ms: int) -> int:
        now = time.perf_counter() - self.t0
        due = min([n.due for n in self.nodes] + [f.t for f in self.pending])
        delay = min(timeout_ms / 1000, due - now)
        if delay > 0:
            time.sleep(delay)
        return CI_WAIT_RC

    def read(self) -> list[Frame]:
        now = time.perf_counter() - self.t0
        out = [f for f in self.pending if f.t <= now]
        self.pending = [f for f in self.pending if f.t > now]
        for nd in self.nodes:
            while nd.due <= now:
                data = nd.fn(nd.due, nd.n)
                out.append(Frame(nd.due, True, nd.id, nd.ext, False, len(data), data))
                nd.due += nd.period
                nd.n += 1
        out.sort(key=lambda f: f.t)
        return out

    def write(self, f: Frame):
        if f.id == 0x7DF and not f.ext and len(f.data) >= 3 and f.data[1] == 0x01:
            pid = f.data[2]
            vals = {0x05: [125], 0x0C: list((2400 * 4).to_bytes(2, "big")), 0x0D: [72]}.get(pid)
            data = bytes([2 + len(vals), 0x41, pid, *vals]) if vals else bytes([3, 0x7F, 0x01, 0x12])
            t = time.perf_counter() - self.t0 + 0.003
            self.pending.append(Frame(t, True, 0x7E8, False, False, 8, data.ljust(8, b"\x55")))

    def errors(self) -> dict:
        return {}

    @staticmethod
    def _engine(t, n):
        rpm = int(1800 + 1500 * math.sin(t / 3))
        thr = int(50 + 45 * math.sin(t / 3 + 0.5))
        return bytes([rpm & 0xFF, rpm >> 8, thr, 0, 0, 0, 0, n & 0x0F])

    @staticmethod
    def _wheels(t, n):
        v = int(100 * (60 + 40 * math.sin(t / 7)))         # km/h x 100
        return b"".join((v + d).to_bytes(2, "little") for d in (0, 3, -2, 1))

    @staticmethod
    def _temps(t, n):
        return bytes([int(120 + 10 * math.sin(t / 30)), int(130 + 5 * math.sin(t / 40)), 62, 0])

    @staticmethod
    def _ccvs(t, n):                                        # J1939 PGN 65265, 1/256 km/h
        v = int(256 * (60 + 40 * math.sin(t / 7)))
        return b"\xFF" + v.to_bytes(2, "little") + b"\xFF" * 5

    @staticmethod
    def _status(t, n):
        return bytes([n & 1, 0x5A if n % 5 == 0 else 0x00])


# --------------------------------------------------------------------------- #
#  Worker                                                                      #
# --------------------------------------------------------------------------- #
RX_QUEUE_MAX = 500_000


class BusWorker(QtCore.QThread):
    """Owns the bus: opens it, reads frames into self.rx, sends self.tx and the
    cyclic frames, closes it. The GUI only touches the deques/dicts below."""
    opened = QtCore.pyqtSignal(str)
    message = QtCore.pyqtSignal(str, str)

    def __init__(self, bus):
        super().__init__()
        self.bus = bus
        self.rx: deque = deque(maxlen=RX_QUEUE_MAX)     # Frames for the GUI
        self.tx: deque = deque()                        # one-shot Frames from the GUI
        self.cyclic: dict = {}      # key -> (Frame, period s); the GUI replaces it as a whole
        self.errs = dict.fromkeys((k for k, _ in ERR_FIELDS), 0)
        self.tx_failed = 0
        self._quit = False

    def stop(self):
        self._quit = True

    def run(self):
        bus = self.bus
        try:
            info = bus.open()
        except Exception as e:
            self.message.emit(f"Open failed: {e}", "err")
            return
        self.opened.emit(info)
        due: dict = {}
        next_err_poll = 0.0
        last_tx_msg = 0.0
        try:
            while not self._quit:
                now = time.perf_counter()
                cyclic = self.cyclic
                for k in [k for k in due if k not in cyclic]:
                    del due[k]
                for k in cyclic:
                    due.setdefault(k, now)
                wait = min([0.02] + [d - now for d in due.values()])
                flags = bus.wait(max(1, int(wait * 1000))) if wait > 0 else 0
                frames = bus.read()
                if frames:
                    self.rx.extend(frames)

                now = time.perf_counter()
                if flags & CI_WAIT_ER or now >= next_err_poll:
                    next_err_poll = now + 1.0
                    for k, v in bus.errors().items():
                        if v:
                            self.errs[k] += v
                            if k == "boff":
                                self.message.emit("Bus-off! Wrong bit rate or wiring? "
                                                  "Reconnect to restart the controller.", "err")

                out = []
                while self.tx:
                    out.append(self.tx.popleft())
                for k, (f, period) in cyclic.items():
                    if due[k] <= now:
                        out.append(f)
                        due[k] += period
                        if due[k] < now:            # fell behind: don't burst
                            due[k] = now + period
                for f in out:
                    try:
                        bus.write(f)
                    except ChaiError as e:
                        self.tx_failed += 1
                        if now - last_tx_msg > 1.0:
                            last_tx_msg = now
                            self.message.emit(str(e), "warn")
                        continue
                    self.rx.append(f._replace(t=time.perf_counter() - bus.t0, rx=False))
        except Exception as e:
            self.message.emit(f"{type(e).__name__}: {e}", "err")
        finally:
            bus.close()


# --------------------------------------------------------------------------- #
#  Models                                                                      #
# --------------------------------------------------------------------------- #
TRACE_MAX = 50_000
CHANGE_HL_S = 1.5           # changed bytes stay highlighted this long

TEXT = QtGui.QColor("#c9d1d9")
TX_COLOR = QtGui.QColor("#f0b03e")
STALE_COLOR = QtGui.QColor("#5c6570")
CHANGED_COLOR = QtGui.QColor("#ff6b6b")
ERR_BG = QtGui.QColor("#5a1d1d")


def parse_id_filter(text: str):
    """'100-1FF, 7E8, !123' -> predicate on CAN ID, or None (= everything)."""
    inc, exc = [], []
    for tok in re.split(r"[\s,;]+", text.strip()):
        if not tok:
            continue
        lo, _, hi = tok.lstrip("!").partition("-")
        rng = (int(lo, 16), int(hi or lo, 16))          # ValueError on bad input
        (exc if tok.startswith("!") else inc).append(rng)
    if not inc and not exc:
        return None

    def accept(cid):
        return ((not inc or any(a <= cid <= b for a, b in inc))
                and not any(a <= cid <= b for a, b in exc))
    return accept


class TraceModel(QtCore.QAbstractTableModel):
    COLS = ["Time, s", "Dir", "ID", "Type", "DLC", "Data", "ASCII"]
    CELL = [lambda f: f"{f.t:.6f}", lambda f: "Rx" if f.rx else "Tx", fmt_id, fmt_type,
            lambda f: str(f.dlc), fmt_data, fmt_ascii]

    def __init__(self):
        super().__init__()
        self.all: deque = deque(maxlen=TRACE_MAX)   # unfiltered, for re-filtering
        self.rows: list[Frame] = []
        self.accept = None

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.COLS)

    def headerData(self, sec, orient, role=QtCore.Qt.DisplayRole):
        if role == QtCore.Qt.DisplayRole and orient == QtCore.Qt.Horizontal:
            return self.COLS[sec]
        return None

    def data(self, idx, role=QtCore.Qt.DisplayRole):
        f = self.rows[idx.row()]
        if role == QtCore.Qt.DisplayRole:
            return self.CELL[idx.column()](f)
        if role == QtCore.Qt.ForegroundRole and not f.rx:
            return TX_COLOR
        return None

    def add(self, frames: list[Frame]):
        self.all.extend(frames)
        new = frames if self.accept is None else [f for f in frames if self.accept(f.id)]
        new = new[-TRACE_MAX:]
        if not new:
            return
        drop = len(self.rows) + len(new) - TRACE_MAX
        if drop > 0:
            self.beginRemoveRows(QtCore.QModelIndex(), 0, drop - 1)
            del self.rows[:drop]
            self.endRemoveRows()
        n = len(self.rows)
        self.beginInsertRows(QtCore.QModelIndex(), n, n + len(new) - 1)
        self.rows.extend(new)
        self.endInsertRows()

    def set_filter(self, accept):
        self.beginResetModel()
        self.accept = accept
        self.rows = [f for f in self.all if accept is None or accept(f.id)]
        self.endResetModel()

    def clear(self):
        self.beginResetModel()
        self.all.clear()
        self.rows = []
        self.endResetModel()


class _IdRow:
    __slots__ = ("frame", "count", "cycle", "seen", "changed")


class MonitorModel(QtCore.QAbstractTableModel):
    """One row per (ID, format, direction), sorted by ID."""
    COLS = ["Dir", "ID", "Type", "DLC", "Data", "Cycle, ms", "Count"]
    DATA_COL = 4

    def __init__(self):
        super().__init__()
        self.keys: list[tuple] = []          # sorted (ext, id, is_tx)
        self.rows: dict = {}
        self.accept = None

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.keys)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.COLS)

    def headerData(self, sec, orient, role=QtCore.Qt.DisplayRole):
        if role == QtCore.Qt.DisplayRole and orient == QtCore.Qt.Horizontal:
            return self.COLS[sec]
        return None

    def frame_at(self, row: int) -> Frame:
        return self.rows[self.keys[row]].frame

    def data(self, idx, role=QtCore.Qt.DisplayRole):
        r = self.rows[self.keys[idx.row()]]
        f, c = r.frame, idx.column()
        if role == QtCore.Qt.DisplayRole:
            if c == 0:
                return "Rx" if f.rx else "Tx"
            if c == 1:
                return fmt_id(f)
            if c == 2:
                return fmt_type(f)
            if c == 3:
                return str(f.dlc)
            if c == 4:
                return fmt_data(f)
            if c == 5:
                return f"{r.cycle * 1000:.1f}" if r.cycle else ""
            return str(r.count)
        if role == QtCore.Qt.UserRole and c == self.DATA_COL:
            return f.data, r.changed
        if role == QtCore.Qt.ForegroundRole:
            if time.monotonic() - r.seen > max(2.0, 3 * (r.cycle or 0)):
                return STALE_COLOR
            return None if f.rx else TX_COLOR
        return None

    def add(self, frames: list[Frame]):
        now = time.monotonic()
        for f in frames:
            if self.accept is not None and not self.accept(f.id):
                continue
            key = (f.ext, f.id, not f.rx)
            r = self.rows.get(key)
            if r is None:
                r = _IdRow()
                r.frame, r.count, r.cycle, r.seen, r.changed = f, 1, None, now, [0.0] * 8
                i = bisect.bisect(self.keys, key)
                self.beginInsertRows(QtCore.QModelIndex(), i, i)
                self.keys.insert(i, key)
                self.rows[key] = r
                self.endInsertRows()
                continue
            old = r.frame
            dt = f.t - old.t
            if dt > 0:
                r.cycle = dt if r.cycle is None else r.cycle + 0.25 * (dt - r.cycle)
            if f.data != old.data:
                for i, b in enumerate(f.data):
                    if i >= len(old.data) or b != old.data[i]:
                        r.changed[i] = now
            r.frame, r.seen = f, now
            r.count += 1

    def refresh(self):
        if self.keys:
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(self.keys) - 1, len(self.COLS) - 1))

    def set_filter(self, accept):
        self.beginResetModel()
        self.accept = accept
        if accept is not None:
            self.keys = [k for k in self.keys if accept(k[1])]
            self.rows = {k: self.rows[k] for k in self.keys}
        self.endResetModel()

    def clear(self):
        self.beginResetModel()
        self.keys, self.rows = [], {}
        self.endResetModel()


def _blend(c1: QtGui.QColor, c2: QtGui.QColor, k: float) -> QtGui.QColor:
    return QtGui.QColor(int(c1.red() + (c2.red() - c1.red()) * k),
                        int(c1.green() + (c2.green() - c1.green()) * k),
                        int(c1.blue() + (c2.blue() - c1.blue()) * k))


class BytesDelegate(QtWidgets.QStyledItemDelegate):
    """Monitor's Data column: recently changed bytes are drawn red, fading out."""

    def paint(self, painter, option, index):
        val = index.data(QtCore.Qt.UserRole)
        if val is None:
            return super().paint(painter, option, index)
        data, changed = val
        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else QtWidgets.QApplication.style()
        style.drawControl(QtWidgets.QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        fg = index.data(QtCore.Qt.ForegroundRole)
        normal = fg if fg is not None else TEXT
        painter.save()
        painter.setFont(opt.font)
        step = QtGui.QFontMetrics(opt.font).horizontalAdvance("00 ")
        rect = opt.rect.adjusted(4, 0, 0, 0)
        now = time.monotonic()
        for i, b in enumerate(data):
            k = (now - changed[i]) / CHANGE_HL_S
            painter.setPen(normal if k >= 1 else _blend(CHANGED_COLOR, normal, max(k, 0.0)))
            painter.drawText(QtCore.QRect(rect.left() + i * step, rect.top(), step, rect.height()),
                             QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, f"{b:02X}")
        painter.restore()


class CsvRecorder:
    HEADER = ["time_s", "dir", "id", "type", "dlc", "data"]

    def __init__(self, path: str):
        self.path = path
        self.f = open(path, "w", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        self.w.writerow(self.HEADER)
        self.n = 0

    def write(self, frames):
        self.w.writerows(frame_row(f) for f in frames)
        self.n += len(frames)

    def flush(self):
        self.f.flush()

    def close(self):
        self.f.close()


# --------------------------------------------------------------------------- #
#  Window                                                                      #
# --------------------------------------------------------------------------- #
STYLE = """
    QWidget{background:#0f1216;color:#c9d1d9;}
    QGroupBox{border:1px solid #2a3038;border-radius:6px;margin-top:14px;padding-top:6px;font-weight:600;}
    QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 4px;color:#8a96a3;}
    QLineEdit,QSpinBox,QComboBox,QPlainTextEdit,QTableView{background:#161b22;
        border:1px solid #30363d;border-radius:4px;padding:3px;}
    QTableView{gridline-color:#21262d;selection-background-color:#1f3a5f;selection-color:#e6edf3;}
    QHeaderView::section{background:#161b22;color:#8a96a3;border:0;padding:3px;}
    QTabWidget::pane{border:1px solid #2a3038;border-radius:4px;}
    QTabBar::tab{background:#161b22;border:1px solid #30363d;padding:5px 16px;margin-right:2px;}
    QTabBar::tab:selected{background:#21262d;color:#e6edf3;}
    QPushButton{background:#21262d;border:1px solid #30363d;border-radius:5px;padding:5px 10px;}
    QPushButton:hover{background:#2d333b;}
    QPushButton:checked{background:#1f6f43;border-color:#2ea043;}
    QPushButton#rec:checked{background:#8b1e1e;border-color:#f85149;}
    QPushButton:disabled,QLineEdit:disabled,QComboBox:disabled,QCheckBox:disabled{color:#4d5560;}
    QStatusBar QLabel{padding:0 8px;}
"""


def mono_font() -> QtGui.QFont:
    if sys.platform == "win32":
        f = QtGui.QFont("Consolas", 10)
    else:
        f = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
    f.setStyleHint(QtGui.QFont.TypeWriter)
    return f


def make_view(model, widths) -> QtWidgets.QTableView:
    v = QtWidgets.QTableView()
    v.setModel(model)
    v.setFont(mono_font())
    v.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
    v.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
    v.setWordWrap(False)
    v.verticalHeader().hide()
    v.verticalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Fixed)
    v.verticalHeader().setDefaultSectionSize(QtGui.QFontMetrics(v.font()).height() + 6)
    h = v.horizontalHeader()
    h.setDefaultAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
    h.setStretchLastSection(True)
    for c, w in enumerate(widths):
        h.resizeSection(c, w)
    return v


class MainWindow(QtWidgets.QMainWindow):
    TX_COLS = ["Cyclic", "Period, ms", "ID (hex)", "Ext", "RTR", "DLC", "Data (hex)"]

    def __init__(self, args):
        super().__init__()
        self.setWindowTitle("CAN monitor - Marathon CHAI" + (" [DEMO]" if args.demo else ""))
        self.resize(1250, 860)
        self.args = args
        self.lib: Chai | None = None
        self.worker: BusWorker | None = None
        self.recorder: CsvRecorder | None = None
        self.lom_active = False
        self.bitrate = 500e3
        self._tx_key = 0
        self.errs = dict.fromkeys((k for k, _ in ERR_FIELDS), 0)
        self._reset_counters()
        self.settings = QtCore.QSettings("CanMonitor", "CanMonitor")
        self._build()
        self.setStyleSheet(STYLE)
        self._load_settings()
        self._init_lib()
        self.timer = QtCore.QTimer(self, interval=40, timeout=self._tick)
        self.timer.start()
        if args.connect:
            QtCore.QTimer.singleShot(0, self.connect_btn.click)

    # ---- layout ----------------------------------------------------------- #
    def _build(self):
        # connection bar
        self.chan_combo = QtWidgets.QComboBox()
        self.chan_combo.setEditable(True)
        self.chan_combo.setMinimumWidth(280)
        self.chan_combo.setToolTip("CHAI channel number (you can type one)")
        refresh = QtWidgets.QPushButton("⟳")
        refresh.setToolTip("Read the board list again")
        refresh.clicked.connect(self._init_lib)
        self.rate_combo = QtWidgets.QComboBox()
        self.rate_combo.addItems([b[0] for b in BITRATES] + ["Custom BTR0/BTR1"])
        self.bt0_edit = QtWidgets.QLineEdit()
        self.bt1_edit = QtWidgets.QLineEdit()
        for e in (self.bt0_edit, self.bt1_edit):
            e.setMaximumWidth(42)
            e.setInputMask(">HH")
            e.textChanged.connect(self._btr_changed)
        self.rate_lbl = QtWidgets.QLabel()
        self.rate_lbl.setMinimumWidth(110)
        self.rate_combo.currentIndexChanged.connect(self._rate_changed)
        self.std_chk = QtWidgets.QCheckBox("11-bit")
        self.ext_chk = QtWidgets.QCheckBox("29-bit")
        self.std_chk.setChecked(True)
        self.ext_chk.setChecked(True)
        self.lom_chk = QtWidgets.QCheckBox("Listen-only")
        self.lom_chk.setToolTip("Don't ACK and don't send error frames (CiSetLom).\n"
                                "Safe on a live network; transmitting is disabled.\n"
                                "Note: if the adapter is the only other node, the sender gets no ACK.")
        self.connect_btn = QtWidgets.QPushButton("Connect")
        self.connect_btn.setCheckable(True)
        self.connect_btn.setMinimumWidth(110)
        self.connect_btn.toggled.connect(self._toggle_connect)
        self.param_widgets = [self.chan_combo, refresh, self.rate_combo, self.bt0_edit,
                              self.bt1_edit, self.std_chk, self.ext_chk, self.lom_chk]

        bar = QtWidgets.QHBoxLayout()
        for w in (QtWidgets.QLabel("Channel"), self.chan_combo, refresh, QtWidgets.QLabel("  Bit rate"),
                  self.rate_combo, QtWidgets.QLabel("BTR0"), self.bt0_edit, QtWidgets.QLabel("BTR1"),
                  self.bt1_edit, self.rate_lbl, self.std_chk, self.ext_chk, self.lom_chk):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(self.connect_btn)

        # view bar
        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText("ID filter, e.g.  100-1FF, 7E8, !123")
        self.filter_edit.setToolTip("Hex IDs or ranges, separated by commas/spaces.\n"
                                    "!ID or !LO-HI hides. Empty shows everything.")
        self._filter_timer = QtCore.QTimer(self, singleShot=True, interval=300,
                                           timeout=self._apply_filter)
        self.filter_edit.textChanged.connect(lambda _: self._filter_timer.start())
        self.pause_btn = QtWidgets.QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.setToolTip("Freeze the views (counting and recording go on)")
        self.autoscroll_chk = QtWidgets.QCheckBox("Auto-scroll")
        self.autoscroll_chk.setChecked(True)
        clear_btn = QtWidgets.QPushButton("Clear")
        clear_btn.clicked.connect(self._clear)
        self.rec_btn = QtWidgets.QPushButton("● Record…")
        self.rec_btn.setObjectName("rec")
        self.rec_btn.setCheckable(True)
        self.rec_btn.setToolTip("Write every frame (unfiltered) to a CSV file")
        self.rec_btn.toggled.connect(self._toggle_record)
        save_btn = QtWidgets.QPushButton("Save trace…")
        save_btn.setToolTip("Save the frames shown in the Trace tab to CSV")
        save_btn.clicked.connect(self._save_trace)
        vbar = QtWidgets.QHBoxLayout()
        vbar.addWidget(QtWidgets.QLabel("Filter"))
        vbar.addWidget(self.filter_edit, 1)
        for w in (self.pause_btn, self.autoscroll_chk, clear_btn, self.rec_btn, save_btn):
            vbar.addWidget(w)

        # views
        self.monitor = MonitorModel()
        self.trace = TraceModel()
        self.monitor_view = make_view(self.monitor, [45, 95, 80, 45, 250, 90, 80])
        self.monitor_view.setItemDelegateForColumn(MonitorModel.DATA_COL, BytesDelegate(self.monitor_view))
        self.monitor_view.doubleClicked.connect(self._monitor_to_tx)
        self.trace_view = make_view(self.trace, [115, 45, 95, 80, 45, 250, 90])
        for v in (self.monitor_view, self.trace_view):
            act = QtWidgets.QAction(v, shortcut=QtGui.QKeySequence.Copy,
                                    shortcutContext=QtCore.Qt.WidgetShortcut)
            act.triggered.connect(lambda _=False, v=v: self._copy_rows(v))
            v.addAction(act)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.monitor_view, "Monitor (by ID)")
        self.tabs.addTab(self.trace_view, "Trace")
        self.tabs.currentChanged.connect(self._tab_changed)

        # transmit
        self.tx_table = QtWidgets.QTableWidget(0, len(self.TX_COLS))
        self.tx_table.setHorizontalHeaderLabels(self.TX_COLS)
        self.tx_table.setFont(mono_font())
        self.tx_table.verticalHeader().hide()
        self.tx_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.tx_table.horizontalHeader().setStretchLastSection(True)
        self.tx_table.horizontalHeader().setDefaultAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        for c, w in enumerate([65, 100, 100, 45, 45, 45]):
            self.tx_table.horizontalHeader().resizeSection(c, w)
        self.tx_table.itemChanged.connect(lambda _: self._publish_cyclic())
        add_btn = QtWidgets.QPushButton("Add")
        add_btn.clicked.connect(lambda: self._add_tx_row())
        del_btn = QtWidgets.QPushButton("Remove")
        del_btn.clicked.connect(self._remove_tx_rows)
        send_btn = QtWidgets.QPushButton("Send selected")
        send_btn.clicked.connect(self._send_selected)
        txb = QtWidgets.QVBoxLayout()
        for w in (send_btn, add_btn, del_btn):
            txb.addWidget(w)
        txb.addStretch(1)
        tx_box = QtWidgets.QGroupBox("Transmit  (DLC empty = number of data bytes)")
        txl = QtWidgets.QHBoxLayout(tx_box)
        txl.addWidget(self.tx_table, 1)
        txl.addLayout(txb)

        self.logbox = QtWidgets.QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumBlockCount(2000)

        self.split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.split.addWidget(self.tabs)
        self.split.addWidget(tx_box)
        self.split.addWidget(self.logbox)
        self.split.setSizes([560, 170, 90])

        central = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(central)
        v.addLayout(bar)
        v.addLayout(vbar)
        v.addWidget(self.split, 1)
        self.setCentralWidget(central)

        self.state_lbl = QtWidgets.QLabel("Disconnected")
        self.count_lbl = QtWidgets.QLabel()
        self.err_lbl = QtWidgets.QLabel()
        sb = self.statusBar()
        sb.addWidget(self.state_lbl)
        sb.addWidget(self.count_lbl)
        sb.addPermanentWidget(self.err_lbl)

    # ---- settings --------------------------------------------------------- #
    def _load_settings(self):
        s = self.settings
        if s.value("geometry"):
            self.restoreGeometry(s.value("geometry"))
        if s.value("split"):
            self.split.restoreState(s.value("split"))
        rate = min(max(int(s.value("rate", DEFAULT_BITRATE)), 0), len(BITRATES))
        self.rate_combo.setCurrentIndex(rate)
        self._rate_changed(rate)
        if rate == len(BITRATES):
            self.bt0_edit.setText(s.value("bt0", "00"))
            self.bt1_edit.setText(s.value("bt1", "1C"))
        self.std_chk.setChecked(s.value("std", "true") == "true")
        self.ext_chk.setChecked(s.value("ext", "true") == "true")
        self.lom_chk.setChecked(s.value("lom", "false") == "true")
        self.filter_edit.setText(s.value("filter", ""))
        self._apply_filter()
        try:
            rows = json.loads(s.value("tx_rows", "null"))
        except (TypeError, ValueError):
            rows = None
        if not isinstance(rows, list):
            rows = [None, [False, "", "7DF", False, False, "", "02 01 0C 00 00 00 00 00"]]
        for r in rows:
            self._add_tx_row(r)

    def _save_settings(self):
        s = self.settings
        s.setValue("geometry", self.saveGeometry())
        s.setValue("split", self.split.saveState())
        if not self.args.demo:
            s.setValue("chan", self._channel())
        s.setValue("rate", self.rate_combo.currentIndex())
        s.setValue("bt0", self.bt0_edit.text())
        s.setValue("bt1", self.bt1_edit.text())
        s.setValue("std", "true" if self.std_chk.isChecked() else "false")
        s.setValue("ext", "true" if self.ext_chk.isChecked() else "false")
        s.setValue("lom", "true" if self.lom_chk.isChecked() else "false")
        s.setValue("filter", self.filter_edit.text())
        t = self.tx_table
        rows = []
        for r in range(t.rowCount()):
            rows.append([t.item(r, c).checkState() == QtCore.Qt.Checked if c in (0, 3, 4)
                         else t.item(r, c).text() for c in range(len(self.TX_COLS))])
        s.setValue("tx_rows", json.dumps(rows))

    # ---- library / channels ----------------------------------------------- #
    def _init_lib(self):
        prev = self._channel() if self.chan_combo.count() else self.settings.value("chan", 0)
        self.chan_combo.clear()
        if self.args.demo:
            self.chan_combo.addItem("Demo bus", 0)
            return
        if self.lib is None:
            try:
                self.lib = Chai(self.args.lib)
                self.logmsg(f"CHAI {self.lib.version()} loaded: {self.lib.path}", "ok")
            except ChaiError as e:
                self.logmsg(str(e).replace("\n", "<br>"), "err")
        chans = self.lib.channels() if self.lib else []
        if chans:
            self.logmsg("Channels: " + ", ".join(label for _, label in chans))
        else:
            if self.lib:
                self.logmsg("No CAN boards found (adapter plugged in, driver updated?). "
                            "You can still type a channel number.", "warn")
            chans = [(c, str(c)) for c in range(8)]
        for ch, label in chans:
            self.chan_combo.addItem(label, ch)
        i = self.chan_combo.findData(int(prev or 0))
        self.chan_combo.setCurrentIndex(max(i, 0))
        self.chan_combo.lineEdit().setCursorPosition(0)

    def _channel(self) -> int:
        i = self.chan_combo.currentIndex()
        text = self.chan_combo.currentText()
        if i >= 0 and text == self.chan_combo.itemText(i):
            return int(self.chan_combo.itemData(i))
        m = re.match(r"\s*(\d+)", text)
        return int(m.group(1)) if m else 0

    def _rate_changed(self, i):
        custom = i >= len(BITRATES)
        if not custom:
            self.bt0_edit.setText(f"{BITRATES[i][1]:02X}")
            self.bt1_edit.setText(f"{BITRATES[i][2]:02X}")
        self.bt0_edit.setEnabled(custom and self.worker is None)
        self.bt1_edit.setEnabled(custom and self.worker is None)

    def _btr(self):
        try:
            return int(self.bt0_edit.text(), 16), int(self.bt1_edit.text(), 16)
        except ValueError:
            return None

    def _btr_changed(self):
        btr = self._btr()
        self.rate_lbl.setText(f"= {btr_bitrate(*btr) / 1e3:g} kbit/s" if btr else "")

    # ---- connect ---------------------------------------------------------- #
    def _toggle_connect(self, on):
        if not on:
            if self.worker is not None:
                self.worker.stop()          # UI is reset in _worker_finished
            return
        btr = self._btr()
        std, ext, lom = self.std_chk.isChecked(), self.ext_chk.isChecked(), self.lom_chk.isChecked()
        err = None
        if btr is None:
            err = "BTR0/BTR1 must be two hex bytes"
        elif not (std or ext):
            err = "Tick 11-bit and/or 29-bit"
        elif not self.args.demo and self.lib is None:
            err = "CHAI library not loaded (see above)"
        if err:
            self.logmsg(err, "err")
            self.connect_btn.setChecked(False)
            return
        if self.args.demo:
            bus = DemoBus(listen_only=lom)
            where = "demo"
        else:
            chan = self._channel()
            bus = ChaiBus(self.lib, chan, btr[0], btr[1], std, ext, lom)
            where = f"ch {chan}"
        self.bitrate = btr_bitrate(*btr)
        self.lom_active = lom
        self.errs = dict.fromkeys((k for k, _ in ERR_FIELDS), 0)
        self._reset_counters()
        self.worker = BusWorker(bus)
        self.worker.opened.connect(lambda info: self._worker_opened(info, where))
        self.worker.message.connect(self.logmsg)
        self.worker.finished.connect(self._worker_finished)
        for w in self.param_widgets:
            w.setEnabled(False)
        self.connect_btn.setText("Connecting…")
        self.worker.start()

    def _worker_opened(self, info, where):
        self.logmsg(f"Connected: {info}, {self.bitrate / 1e3:g} kbit/s", "ok")
        self.connect_btn.setText("Disconnect")
        self.state_lbl.setText(f'<span style="color:#4fd18b">●</span> {where}, '
                               f'{self.bitrate / 1e3:g} kbit/s' + (", listen-only" if self.lom_active else ""))
        self._publish_cyclic()

    def _worker_finished(self):
        w = self.worker
        if w is None:
            return
        w.wait()
        self._tick()                        # take the last frames
        self.errs = dict(w.errs)
        self.worker = None
        self.connect_btn.blockSignals(True)
        self.connect_btn.setChecked(False)
        self.connect_btn.blockSignals(False)
        self.connect_btn.setText("Connect")
        for wd in self.param_widgets:
            wd.setEnabled(True)
        self._rate_changed(self.rate_combo.currentIndex())
        self.state_lbl.setText("Disconnected")
        self.logmsg("Disconnected")

    # ---- periodic GUI update ---------------------------------------------- #
    def _reset_counters(self):
        self.n_rx = self.n_tx = 0
        self._win_frames = self._win_bits = 0
        self._win_start = time.monotonic()
        self.fps = self.load = 0.0

    def _tick(self):
        w = self.worker
        if w is not None and w.rx:
            dq = w.rx
            batch = [dq.popleft() for _ in range(len(dq))]
            ntx = sum(1 for f in batch if not f.rx)
            self.n_tx += ntx
            self.n_rx += len(batch) - ntx
            self._win_frames += len(batch)
            self._win_bits += sum(frame_bits(f) for f in batch)
            if self.recorder:
                self.recorder.write(batch)
            if not self.pause_btn.isChecked():
                self.trace.add(batch)
                self.monitor.add(batch)
                if self.autoscroll_chk.isChecked() and self.tabs.currentIndex() == 1:
                    self.trace_view.scrollToBottom()
        if self.tabs.currentIndex() == 0 and not self.pause_btn.isChecked():
            self.monitor.refresh()
        now = time.monotonic()
        if now - self._win_start >= 0.5:
            dt = now - self._win_start
            self.fps = self._win_frames / dt
            self.load = 100.0 * self._win_bits / dt / self.bitrate
            self._win_frames = self._win_bits = 0
            self._win_start = now
            self._update_status()

    def _update_status(self):
        errs = dict(self.worker.errs) if self.worker else self.errs
        tx_failed = self.worker.tx_failed if self.worker else 0
        self.count_lbl.setText(f"Rx {self.n_rx}   Tx {self.n_tx}   {self.fps:.0f} frames/s   "
                               f"load ≈ {self.load:.1f} %")
        parts = []
        for k, label in ERR_FIELDS + [("txfail", "Tx failed")]:
            v = tx_failed if k == "txfail" else errs.get(k, 0)
            color = "#ff6b6b" if v else "#6e7681"
            parts.append(f'<span style="color:{color}">{label} {v}</span>')
        self.err_lbl.setText("  ".join(parts))
        if self.recorder:
            self.recorder.flush()
            self.rec_btn.setText(f"● Recording ({self.recorder.n})")

    def _tab_changed(self, i):
        if i == 1 and self.autoscroll_chk.isChecked():
            self.trace_view.scrollToBottom()

    # ---- view actions ----------------------------------------------------- #
    def _apply_filter(self):
        try:
            accept = parse_id_filter(self.filter_edit.text())
        except ValueError:
            self.filter_edit.setStyleSheet("border:1px solid #f85149;")
            return
        self.filter_edit.setStyleSheet("")
        self.trace.set_filter(accept)
        self.monitor.set_filter(accept)

    def _clear(self):
        self.trace.clear()
        self.monitor.clear()
        self.n_rx = self.n_tx = 0
        self._update_status()

    def _copy_rows(self, view):
        rows = sorted({i.row() for i in view.selectionModel().selectedIndexes()})
        m = view.model()
        text = "\n".join("\t".join(str(m.data(m.index(r, c))) for c in range(m.columnCount()))
                         for r in rows)
        QtWidgets.QApplication.clipboard().setText(text)

    def _default_csv(self, prefix):
        return os.path.join(script_dir(), f"{prefix}_{_dt.datetime.now():%Y-%m-%d_%H%M%S}.csv")

    def _toggle_record(self, on):
        if on:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Record to", self._default_csv("can_log"),
                                                            "CSV (*.csv)")
            if not path:
                self.rec_btn.setChecked(False)
                return
            try:
                self.recorder = CsvRecorder(path)
            except OSError as e:
                self.logmsg(f"Record: {e}", "err")
                self.rec_btn.setChecked(False)
                return
            self.logmsg(f"Recording to {path}", "ok")
        elif self.recorder:
            self.recorder.close()
            self.logmsg(f"Recorded {self.recorder.n} frames to {self.recorder.path}", "ok")
            self.recorder = None
            self.rec_btn.setText("● Record…")

    def _save_trace(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save trace", self._default_csv("can_trace"),
                                                        "CSV (*.csv)")
        if not path:
            return
        try:
            rec = CsvRecorder(path)
            rec.write(self.trace.rows)
            rec.close()
        except OSError as e:
            self.logmsg(f"Save: {e}", "err")
            return
        self.logmsg(f"Saved {len(self.trace.rows)} frames to {path}", "ok")

    # ---- transmit --------------------------------------------------------- #
    def _add_tx_row(self, vals=None):
        if not isinstance(vals, (list, tuple)) or len(vals) != len(self.TX_COLS):
            vals = [False, "100", "123", False, False, "", "11 22 33 44 55 66 77 88"]
        t = self.tx_table
        t.blockSignals(True)
        r = t.rowCount()
        t.insertRow(r)
        for c, v in enumerate(vals):
            it = QtWidgets.QTableWidgetItem()
            if c in (0, 3, 4):
                it.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsUserCheckable)
                it.setCheckState(QtCore.Qt.Checked if v is True else QtCore.Qt.Unchecked)
            else:
                it.setText(str(v))
            t.setItem(r, c, it)
        self._tx_key += 1
        t.item(r, 0).setData(QtCore.Qt.UserRole, self._tx_key)
        t.blockSignals(False)
        self._publish_cyclic()

    def _remove_tx_rows(self):
        rows = sorted({i.row() for i in self.tx_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.tx_table.removeRow(r)
        self._publish_cyclic()

    def _monitor_to_tx(self, idx):
        f = self.monitor.frame_at(idx.row())
        self._add_tx_row([False, "100", fmt_id(f), f.ext, f.rtr, str(f.dlc), fmt_data(f)])

    def _tx_frame(self, r) -> tuple[Frame, float]:
        """Transmit row -> (Frame, period s). ValueError with a message if invalid."""
        t = self.tx_table

        def text(c):
            return t.item(r, c).text().strip()

        def checked(c):
            return t.item(r, c).checkState() == QtCore.Qt.Checked

        try:
            cid = int(text(2), 16)
        except ValueError:
            raise ValueError("ID must be hex") from None
        ext, rtr = checked(3), checked(4)
        if cid > 0x1FFFFFFF or (cid > 0x7FF and not ext):
            raise ValueError("ID above 7FF needs Ext" if cid <= 0x1FFFFFFF else "ID above 1FFFFFFF")
        try:
            data = bytes.fromhex(text(6).replace(",", " "))
        except ValueError:
            raise ValueError("Data: hex bytes, e.g. 01 02 0A") from None
        if len(data) > 8:
            raise ValueError("more than 8 data bytes")
        try:
            dlc = int(text(5)) if text(5) else len(data)
        except ValueError:
            raise ValueError("DLC must be 0...8") from None
        if not 0 <= dlc <= 8:
            raise ValueError("DLC must be 0...8")
        try:
            period = float(text(1) or 0) / 1000
        except ValueError:
            raise ValueError("Period must be a number (ms)") from None
        data = b"" if rtr else data[:dlc].ljust(dlc, b"\0")
        return Frame(0.0, False, cid, ext, rtr, dlc, data), period

    def _publish_cyclic(self):
        t = self.tx_table
        cyclic = {}
        t.blockSignals(True)
        for r in range(t.rowCount()):
            try:
                f, period = self._tx_frame(r)
                err = ""
            except ValueError as e:
                f, period, err = None, 0, str(e)
            for c in range(t.columnCount()):
                it = t.item(r, c)
                it.setData(QtCore.Qt.BackgroundRole, ERR_BG if err else None)
                it.setToolTip(err)
            if f is not None and t.item(r, 0).checkState() == QtCore.Qt.Checked and period > 0:
                cyclic[t.item(r, 0).data(QtCore.Qt.UserRole)] = (f, max(period, 0.001))
        t.blockSignals(False)
        if self.worker is not None:
            if cyclic and self.lom_active:
                self.logmsg("Listen-only mode: cyclic transmit is off", "warn")
                cyclic = {}
            self.worker.cyclic = cyclic

    def _send_selected(self):
        t = self.tx_table
        rows = sorted({i.row() for i in t.selectedIndexes()}) or ([t.currentRow()] if t.currentRow() >= 0 else [])
        if not rows:
            self.logmsg("Select a row in the Transmit list first", "warn")
            return
        if self.worker is None:
            self.logmsg("Not connected", "warn")
            return
        if self.lom_active:
            self.logmsg("Listen-only mode: transmit is disabled", "warn")
            return
        for r in rows:
            try:
                f, _ = self._tx_frame(r)
            except ValueError as e:
                self.logmsg(f"Transmit row {r + 1}: {e}", "err")
                continue
            self.worker.tx.append(f)

    # ---- misc ------------------------------------------------------------- #
    def logmsg(self, text, level="info"):
        color = {"info": "#c9d1d9", "ok": "#4fd18b", "warn": "#f0b03e", "err": "#ff6b6b"}[level]
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        self.logbox.appendHtml(f'<span style="color:#6e7681">{ts}</span> <span style="color:{color}">{text}</span>')

    def closeEvent(self, ev):
        self._save_settings()
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(3000)
        if self.recorder:
            self.recorder.close()
        ev.accept()


def main():
    ap = argparse.ArgumentParser(description="CAN bus monitor for Marathon adapters (CHAI library)")
    ap.add_argument("--demo", action="store_true", help="simulated bus, no hardware needed")
    ap.add_argument("--lib", help="path to chai.dll / libchai.so (default: search for it)")
    ap.add_argument("--connect", action="store_true", help="connect on start-up")
    args = ap.parse_args()
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    w = MainWindow(args)
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
