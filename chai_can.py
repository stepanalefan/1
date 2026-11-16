# -*- coding: utf-8 -*-
"""
chai_can.py
===========
Marathon CAN adapters (CAN-bus-USB, 2 channel, and others) through Marathon's CHAI library
(CAN Hardware Abstraction Interface, chai.dll on Windows / libchai.so on Linux, installed with the
Marathon driver package from can.marathon.ru). ctypes only, no extra Python package.

CHAI 2.x calls used:
    CiInit()                                   once per process
    CiOpen(chan, CIO_CAN11 | CIO_CAN29)        open a channel for 11- and 29-bit frames
    CiSetBaud(chan, bt0, bt1)                  SJA1000-style bit timing (16 MHz clock, see BAUD)
    CiStart(chan) / CiStop(chan) / CiClose(chan)
    CiTransmit(chan, canmsg_t *)               queue one frame
    CiRead(chan, canmsg_t *buf, count)         non-blocking: number of frames read, < 0 = error
    CiSetLom(chan, 1)                          listen-only mode, if the library has it
    CiGetLibVer()                              library version (u32)
canmsg_t:  u32 id; u8 data[8]; u8 len; u16 flags (bit 0 RTR, bit 2 EFF = 29-bit id); u32 ts (µs)
Channels are numbered over all Marathon adapters: first adapter CAN1 = 0, CAN2 = 1, second adapter 2, 3 ...
Set the environment variable CHAI_LIB to the full path of chai.dll / libchai.so if it is not found.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time

CIO_CAN11 = 0x2
CIO_CAN29 = 0x4
FRAME_RTR = 0x1
FRAME_EFF = 0x4

# bit rate -> (bt0, bt1), SJA1000 bit timing at 16 MHz (tq = 2·(BRP+1)/16 MHz):
# bt1 0x1C = TSEG1 13 + TSEG2 2 + sync = 16 tq, 0x16 = 10 tq, 0x14 = 8 tq; bt0 = BRP
BAUD = {
    1_000_000: (0x00, 0x14), 800_000: (0x00, 0x16), 500_000: (0x00, 0x1C), 250_000: (0x01, 0x1C),
    125_000: (0x03, 0x1C), 100_000: (0x04, 0x1C), 50_000: (0x09, 0x1C), 20_000: (0x18, 0x1C),
    10_000: (0x31, 0x1C),
}


class canmsg_t(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("data", ctypes.c_uint8 * 8), ("len", ctypes.c_uint8),
                ("flags", ctypes.c_uint16), ("ts", ctypes.c_uint32)]


_lib = None
_lock = threading.Lock()
_inited = False


def lib_candidates() -> list[str]:
    env = os.environ.get("CHAI_LIB")
    if env:
        return [env]
    if sys.platform.startswith("win"):
        names = ["chai.dll"]
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            for sub in ("CHAI", "Marathon\\CHAI", "chai-2.14.0", "CHAI-2.14.0"):
                names.append(os.path.join(base, sub, "chai.dll"))
        for here in (os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else "",
                     os.path.dirname(os.path.abspath(__file__))):
            if here:
                names.append(os.path.join(here, "chai.dll"))     # next to the program / KTR_TEST.exe
        return names
    return ["libchai.so", "libchai.so.2", "/usr/local/lib/libchai.so", "/usr/lib/libchai.so"]


def load():
    """The CHAI library (loaded and initialised once)."""
    global _lib, _inited
    with _lock:
        if _lib is None:
            err = None
            for name in lib_candidates():
                try:
                    _lib = ctypes.CDLL(name)
                    break
                except OSError as e:
                    err = e
            if _lib is None:
                raise RuntimeError("Marathon CHAI library not found (chai.dll / libchai.so): install the Marathon "
                                   "driver package, or set CHAI_LIB to its full path" + (f" ({err})" if err else ""))
            for fn, res, args in (("CiInit", ctypes.c_int16, []),
                                  ("CiOpen", ctypes.c_int16, [ctypes.c_uint8, ctypes.c_uint8]),
                                  ("CiClose", ctypes.c_int16, [ctypes.c_uint8]),
                                  ("CiStart", ctypes.c_int16, [ctypes.c_uint8]),
                                  ("CiStop", ctypes.c_int16, [ctypes.c_uint8]),
                                  ("CiSetBaud", ctypes.c_int16, [ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint8]),
                                  ("CiTransmit", ctypes.c_int16, [ctypes.c_uint8, ctypes.POINTER(canmsg_t)]),
                                  ("CiRead", ctypes.c_int16, [ctypes.c_uint8, ctypes.POINTER(canmsg_t),
                                                              ctypes.c_int16])):
                f = getattr(_lib, fn)
                f.restype, f.argtypes = res, args
            if hasattr(_lib, "CiGetLibVer"):
                _lib.CiGetLibVer.restype, _lib.CiGetLibVer.argtypes = ctypes.c_uint32, []
            if hasattr(_lib, "CiSetLom"):
                _lib.CiSetLom.restype, _lib.CiSetLom.argtypes = ctypes.c_int16, [ctypes.c_uint8, ctypes.c_uint8]
        if not _inited:
            r = _lib.CiInit()
            if r < 0:
                raise RuntimeError(f"CiInit failed ({r})")
            _inited = True
    return _lib


def lib_version() -> str:
    lib = load()
    if not hasattr(lib, "CiGetLibVer"):
        return "?"
    v = lib.CiGetLibVer()
    return f"{(v >> 16) & 0xFF}.{(v >> 8) & 0xFF}.{v & 0xFF}"


def _check(r, what):
    if r < 0:
        raise RuntimeError(f"{what} failed (CHAI error {r})")
    return r


class ChaiChannel:
    """One CHAI channel; recv() / send() / close() like the other CAN backends. `make_frame(ts, id, ext, rtr,
    dlc, data)` builds the caller's frame object."""

    def __init__(self, chan: int, bitrate: int, listen_only: bool, make_frame):
        if bitrate not in BAUD:
            raise ValueError(f"bit rate {bitrate} not supported by CHAI (use {', '.join(str(b) for b in BAUD)})")
        self.lib = load()
        self.chan = int(chan)
        self.make_frame = make_frame
        self.errors = 0
        _check(self.lib.CiOpen(self.chan, CIO_CAN11 | CIO_CAN29), f"CiOpen({self.chan})")
        try:
            bt0, bt1 = BAUD[bitrate]
            _check(self.lib.CiSetBaud(self.chan, bt0, bt1), "CiSetBaud")
            lo = ""
            if listen_only:
                if hasattr(self.lib, "CiSetLom"):
                    lo = ", listen-only" if self.lib.CiSetLom(self.chan, 1) >= 0 else ", listen-only failed"
                else:
                    lo = ", listen-only not available in this CHAI version"
            _check(self.lib.CiStart(self.chan), "CiStart")
        except Exception:
            self.lib.CiClose(self.chan)
            raise
        self.buf = (canmsg_t * 64)()
        self.info = f"Marathon CHAI {lib_version()} channel {self.chan} (CAN{self.chan % 2 + 1}), " \
                    f"{bitrate // 1000} kbit/s{lo}"

    def recv(self) -> list:
        """Frames received within ~50 ms (CiRead does not block)."""
        out = []
        t_end = time.monotonic() + 0.05
        while True:
            n = self.lib.CiRead(self.chan, self.buf, len(self.buf))
            if n < 0:
                self.errors += 1
                raise RuntimeError(f"CiRead failed (CHAI error {n})")
            now = time.time()
            for k in range(n):
                m = self.buf[k]
                rtr = bool(m.flags & FRAME_RTR)
                dlc = min(int(m.len), 8)
                out.append(self.make_frame(now, int(m.id), bool(m.flags & FRAME_EFF), rtr, dlc,
                                           b"" if rtr else bytes(m.data[:dlc])))
            if out or time.monotonic() >= t_end:
                return out
            time.sleep(0.002)

    def send(self, can_id: int, ext: bool, rtr: bool, dlc: int, data: bytes):
        m = canmsg_t()
        m.id = can_id
        m.len = dlc if dlc is not None else len(data)
        m.flags = (FRAME_EFF if ext else 0) | (FRAME_RTR if rtr else 0)
        for k, b in enumerate(bytes(data)[:8]):
            m.data[k] = b
        _check(self.lib.CiTransmit(self.chan, ctypes.byref(m)), "CiTransmit")

    def close(self):
        try:
            self.lib.CiStop(self.chan)
        finally:
            self.lib.CiClose(self.chan)
