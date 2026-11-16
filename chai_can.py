# -*- coding: utf-8 -*-
r"""
chai_can.py
===========
Marathon CAN adapters (CAN-bus-USB, 2 channel, and others) through Marathon's CHAI library
(CAN Hardware Abstraction Interface, chai.dll on Windows / libchai.so on Linux, installed with the
Marathon driver package from can.marathon.ru). ctypes only, no extra Python package.

Checked against the CHAI 2.14.0 software design guide (Marathon). CAN-bus-USB (2 channels): both channels on
ONE 9-pin D-sub (CAN1: L pin 2, H pin 7, GND 3 / 6; CAN2: L pin 4, H pin 8, GND 5) - use Marathon's branch
cable for two standard CiA DS 102 connectors; 120 Ohm terminators are jumpers P6 (CAN1) / P7 (CAN2) inside.

CHAI 2.x calls used:
    CiInit()                                   once per process
    CiOpen(chan, CIO_CAN11 | CIO_CAN29)        open a channel for 11- and 29-bit frames
    CiSetBaud(chan, bt0, bt1)                  SJA1000-style bit timing (16 MHz clock, see BAUD)
    CiStart(chan) / CiStop(chan) / CiClose(chan)
    CiTransmit(chan, canmsg_t *)               queue one frame (CHAI >= 2.10; not on CAN-bus-USBnp firmware
                                               <= 1.3 -> CiWrite(chan, canmsg_t *, 1) is used instead)
    CiRead(chan, canmsg_t *buf, count)         non-blocking: number of frames read, < 0 = error
    CiSetLom(chan, 1)                          listen-only mode, if the library has it
    CiGetLibVer()                              library version (u32: major byte 2, minor byte 1, sub byte 0)
    CiBoardInfo(canboard_t *)                  adapters and their channel numbers (brdnum 0...7)
    CiStrError(errno, buf, n)                  error text (errors are returned as -errno)
canmsg_t:  u32 id; u8 data[8]; u8 len; u16 flags (bit 0 RTR, bit 2 EFF = 29-bit id); u32 ts (µs)
Channels are numbered from 0 over all CAN controllers in the computer (up to 8): first adapter CAN1 = 0,
CAN2 = 1, ... - CiBoardInfo gives the real numbers. The numeric values of CIO_CAN11 / CIO_CAN29 / CI_LOM_ON are
not in the guide: the values below are from chai.h 2.14.x (CHAI_INSTALL_DIR\include\chai.h), as are the structures,
the error codes and the bit-timing table (BCI_*). No calling convention is declared there (cdecl -> ctypes.CDLL).
chai.dll: CHAI_INSTALL_DIR\x64 (64-bit Python) or \x32 (32-bit Python), also copied to the Windows system folder
by the installer. Set the environment variable CHAI_LIB to the full path if it is not found.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time

# chai.h 2.14.x
CIO_CAN11 = 0x2
CIO_CAN29 = 0x4
CI_LOM_ON = 0x1
FRAME_RTR = 0x1
FRAME_EFF = 0x4
CI_WAIT_RC, CI_WAIT_TR, CI_WAIT_ER = 0x1, 0x2, 0x4
ERRORS = {1: "ECIGEN generic error", 2: "ECIBUSY device or resource busy", 3: "ECIMFAULT memory fault",
          4: "ECISTATE wrong state for this call", 5: "ECIINCALL invalid call", 6: "ECIINVAL invalid parameter",
          7: "ECIACCES cannot access resource", 8: "ECINOSYS not implemented", 9: "ECIIO input/output error",
          10: "ECINODEV no such device", 11: "ECIINTR interrupted", 12: "ECINORES no resources",
          13: "ECITOUT time out"}

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


class canwait_t(ctypes.Structure):
    _fields_ = [("chan", ctypes.c_uint8), ("wflags", ctypes.c_uint8), ("rflags", ctypes.c_uint8)]


class canerrs_t(ctypes.Structure):
    _fields_ = [("ewl", ctypes.c_uint16), ("boff", ctypes.c_uint16), ("hwovr", ctypes.c_uint16),
                ("swovr", ctypes.c_uint16), ("wtout", ctypes.c_uint16)]


class canboard_t(ctypes.Structure):
    _fields_ = [("brdnum", ctypes.c_uint8), ("hwver", ctypes.c_uint32), ("chip", ctypes.c_int16 * 4),
                ("name", ctypes.c_char * 64), ("manufact", ctypes.c_char * 64)]


_lib = None
_lock = threading.Lock()
_inited = False


def lib_candidates() -> list[str]:
    env = os.environ.get("CHAI_LIB")
    if env:
        return [env]
    if sys.platform.startswith("win"):
        import glob
        import struct
        arch = "x64" if struct.calcsize("P") == 8 else "x32"     # must match the Python bitness
        names = ["chai.dll"]
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "C:\\"):
            for d in glob.glob(os.path.join(base, "*[Cc][Hh][Aa][Ii]*")) + \
                    glob.glob(os.path.join(base, "*[Mm]arathon*", "*[Cc][Hh][Aa][Ii]*")):
                names += [os.path.join(d, arch, "chai.dll"), os.path.join(d, "chai.dll")]
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
                    if getattr(e, "winerror", None) == 193:          # 32 / 64-bit mismatch
                        err = f"{name} does not match this {8 * ctypes.sizeof(ctypes.c_void_p)}-bit Python - use " \
                              f"the chai.dll from the x64 / x32 folder of the CHAI installation"
                    elif err is None or "193" not in str(err):
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
                                  ("CiRead", ctypes.c_int16, [ctypes.c_uint8, ctypes.POINTER(canmsg_t),
                                                              ctypes.c_int16])):
                f = getattr(_lib, fn)
                f.restype, f.argtypes = res, args
            if hasattr(_lib, "CiTransmit"):
                _lib.CiTransmit.restype = ctypes.c_int16
                _lib.CiTransmit.argtypes = [ctypes.c_uint8, ctypes.POINTER(canmsg_t)]
            if hasattr(_lib, "CiGetLibVer"):
                _lib.CiGetLibVer.restype, _lib.CiGetLibVer.argtypes = ctypes.c_uint32, []
            if hasattr(_lib, "CiSetLom"):
                _lib.CiSetLom.restype, _lib.CiSetLom.argtypes = ctypes.c_int16, [ctypes.c_uint8, ctypes.c_uint8]
            if hasattr(_lib, "CiWrite"):
                _lib.CiWrite.restype = ctypes.c_int16
                _lib.CiWrite.argtypes = [ctypes.c_uint8, ctypes.POINTER(canmsg_t), ctypes.c_int16]
            if hasattr(_lib, "CiBoardInfo"):
                _lib.CiBoardInfo.restype, _lib.CiBoardInfo.argtypes = ctypes.c_int16, [ctypes.POINTER(canboard_t)]
            if hasattr(_lib, "CiWaitEvent"):
                _lib.CiWaitEvent.restype = ctypes.c_int16
                _lib.CiWaitEvent.argtypes = [ctypes.POINTER(canwait_t), ctypes.c_int, ctypes.c_int]
            if hasattr(_lib, "CiErrsGetClear"):
                _lib.CiErrsGetClear.restype = ctypes.c_int16
                _lib.CiErrsGetClear.argtypes = [ctypes.c_uint8, ctypes.POINTER(canerrs_t)]
            if hasattr(_lib, "CiGetFirmwareVer"):
                _lib.CiGetFirmwareVer.restype = ctypes.c_int16
                _lib.CiGetFirmwareVer.argtypes = [ctypes.c_uint8, ctypes.POINTER(ctypes.c_uint32)]
            if hasattr(_lib, "CiStrError"):
                _lib.CiStrError.restype = None
                _lib.CiStrError.argtypes = [ctypes.c_int16, ctypes.c_char_p, ctypes.c_int16]
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


def err_text(r: int) -> str:
    """CHAI error text (functions return -errno)."""
    lib = _lib
    if lib is not None and hasattr(lib, "CiStrError"):
        buf = ctypes.create_string_buffer(128)
        try:
            lib.CiStrError(-r, buf, len(buf))
            if buf.value:
                return f"{buf.value.decode(errors='replace')} ({r})"
        except Exception:
            pass
    return f"{ERRORS.get(-r, 'CHAI error')} ({r})"


def _check(r, what):
    if r < 0:
        raise RuntimeError(f"{what} failed: {err_text(r)}")
    return r


def boards() -> list[dict]:
    """Connected Marathon adapters: [{board, name, manufacturer, hw, channels: [chan, ...]}]."""
    lib = load()
    out = []
    if not hasattr(lib, "CiBoardInfo"):
        return out
    for b in range(8):
        bi = canboard_t()
        bi.brdnum = b
        if lib.CiBoardInfo(ctypes.byref(bi)) < 0:
            continue
        v = bi.hwver
        out.append({"board": b, "name": bi.name.decode(errors="replace").strip(),
                    "manufacturer": bi.manufact.decode(errors="replace").strip(),
                    "hw": f"{(v >> 16) & 0xFF}.{(v >> 8) & 0xFF}.{v & 0xFF}",
                    "channels": [int(c) for c in bi.chip if c >= 0]})
    return out


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
                    r = self.lib.CiSetLom(self.chan, CI_LOM_ON)       # must be set before CiStart (CAN_INIT)
                    lo = ", listen-only" if r >= 0 else f", listen-only failed: {err_text(r)}"
                else:
                    lo = ", listen-only not available in this CHAI version"
            _check(self.lib.CiStart(self.chan), "CiStart")
        except Exception:
            self.lib.CiClose(self.chan)
            raise
        self.buf = (canmsg_t * 64)()
        self.use_write = not hasattr(self.lib, "CiTransmit")         # CHAI < 2.10
        fw = ""
        if hasattr(self.lib, "CiGetFirmwareVer"):
            v = ctypes.c_uint32(0)
            if self.lib.CiGetFirmwareVer(self.chan, ctypes.byref(v)) >= 0 and v.value:
                fw = f", firmware {(v.value >> 16) & 0xFF}.{(v.value >> 8) & 0xFF}.{v.value & 0xFF}"
        self.info = f"Marathon CHAI {lib_version()} channel {self.chan} (CAN{self.chan % 2 + 1}){fw}, " \
                    f"{bitrate // 1000} kbit/s{lo}"
        self.bus_errors = {"ewl": 0, "boff": 0, "hwovr": 0, "swovr": 0}
        self._next_err = 0.0

    def _errors_poll(self):
        """Once a second: CAN errors since the last call (error warning, bus-off, overruns) -> self.errors."""
        now = time.monotonic()
        if now < self._next_err or not hasattr(self.lib, "CiErrsGetClear"):
            return
        self._next_err = now + 1.0
        e = canerrs_t()
        if self.lib.CiErrsGetClear(self.chan, ctypes.byref(e)) >= 0:
            for k in self.bus_errors:
                v = getattr(e, k)
                self.bus_errors[k] += v
                self.errors += v

    def recv(self) -> list:
        """Frames received within ~50 ms. CiWaitEvent sleeps until frames arrive (no busy polling); without it
        CiRead (non-blocking) is polled."""
        out = []
        self._errors_poll()
        t_end = time.monotonic() + 0.05
        if hasattr(self.lib, "CiWaitEvent"):
            cw = canwait_t(self.chan, CI_WAIT_RC | CI_WAIT_ER, 0)
            r = self.lib.CiWaitEvent(ctypes.byref(cw), 1, 50)
            if r < 0:
                raise RuntimeError(f"CiWaitEvent failed: {err_text(r)}")
            if r == 0 or not (cw.rflags & CI_WAIT_RC):
                return out                                   # time out / only an error event
        while True:
            n = self.lib.CiRead(self.chan, self.buf, len(self.buf))
            if n < 0:
                self.errors += 1
                raise RuntimeError(f"CiRead failed: {err_text(n)}")
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
        if not self.use_write:
            r = self.lib.CiTransmit(self.chan, ctypes.byref(m))
            if r >= 0:
                return
            if not hasattr(self.lib, "CiWrite"):
                _check(r, "CiTransmit")
            self.use_write = True          # CAN-bus-USBnp firmware <= 1.3 has no CiTransmit: use CiWrite from now on
        _check(self.lib.CiWrite(self.chan, ctypes.byref(m), 1), "CiWrite")

    def close(self):
        try:
            self.lib.CiStop(self.chan)
        finally:
            self.lib.CiClose(self.chan)
