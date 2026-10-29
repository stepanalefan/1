# -*- coding: utf-8 -*-
"""
visa_rm.py
==========
One VISA ResourceManager for the whole program.

Order:
  1. the installed VISA library (Keysight IO Libraries Suite or NI-VISA) - pyvisa's default lookup,
     then the usual DLL locations explicitly (visa64.dll / visa32.dll in System32, IVI folder, Keysight)
  2. only if none loads: pyvisa-py (pure Python). It reaches LAN instruments, but USB needs
     pyusb + a libusb backend AND a libusb-compatible driver for the device - an instrument that
     uses Keysight's USB driver is NOT reachable through it.

info() says which one is in use; explain(e) turns backend errors into an actionable message.
"""
from __future__ import annotations

import os
import struct
import sys

try:
    import pyvisa
except ImportError:  # pragma: no cover
    pyvisa = None

_rm = None
_info = ""
_failures: list[str] = []
_dll_dirs_added = False


def _vendor_dirs() -> list[str]:
    """Folders holding the VISA implementation and its dependencies (Keysight, IVI, NI)."""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    bits64 = struct.calcsize("P") == 8
    cands = [
        os.path.join(pf, "Keysight", "IO Libraries Suite", "bin"),
        os.path.join(pf86, "Keysight", "IO Libraries Suite", "bin"),
        os.path.join(pf, "IVI Foundation", "VISA", "Win64" if bits64 else "WinNT", "Bin"),
        os.path.join(pf, "IVI Foundation", "VISA", "Win64", "ktvisa"),
        os.path.join(pf, "IVI Foundation", "VISA", "Win64", "agvisa"),
        os.path.join(pf86, "IVI Foundation", "VISA", "WinNT", "Bin"),
        os.path.join(pf86, "IVI Foundation", "VISA", "WinNT", "ktvisa"),
        os.path.join(pf, "IVI Foundation", "IVI", "Bin"),
        os.path.join(pf86, "IVI Foundation", "IVI", "Bin"),
        os.path.join(pf, "National Instruments", "Shared", "NI-VISA") if bits64 else "",
    ]
    return [d for d in cands if d and os.path.isdir(d)]


def _add_dll_dirs():
    """Python 3.8+ no longer searches PATH for the dependencies of a DLL loaded through ctypes,
    so visa64.dll loads but cannot find the vendor's VISA DLLs (VI_ERROR_LIBRARY_NFOUND).
    Register the vendor folders for the DLL search and for the VISA router's own LoadLibrary."""
    global _dll_dirs_added
    if _dll_dirs_added or sys.platform != "win32":
        return
    _dll_dirs_added = True
    for d in _vendor_dirs():
        try:
            os.add_dll_directory(d)
        except (OSError, AttributeError):
            pass
        if d.lower() not in os.environ.get("PATH", "").lower():
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def _load_error(path: str) -> str:
    """The Windows loader's own reason (missing dependency, wrong bitness, ...)."""
    if sys.platform != "win32" or not path:
        return ""
    import ctypes
    try:
        ctypes.WinDLL(path)
        return ""
    except OSError as e:
        code = getattr(e, "winerror", None)
        hint = {126: "a DLL it depends on was not found", 193: "32/64-bit mismatch between Python and VISA",
                127: "a required function is missing (old / mixed VISA versions)"}.get(code, "")
        return f" [Windows: {e}{' - ' + hint if hint else ''}]"


def _candidates() -> list[str]:
    out = [""]                                       # pyvisa default: the IVI VISA shared library
    if sys.platform == "win32":
        bits64 = struct.calcsize("P") == 8
        dll = "visa64.dll" if bits64 else "visa32.dll"
        sysroot = os.environ.get("SystemRoot", r"C:\Windows")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        out += [
            os.path.join(sysroot, "System32" if bits64 else "SysWOW64", dll),
            os.path.join(sysroot, "System32", dll),
            os.path.join(pf, "IVI Foundation", "VISA", "Win64" if bits64 else "WinNT", "Bin", dll),
            os.path.join(pf86, "IVI Foundation", "VISA", "WinNT", "Bin", "visa32.dll"),
            os.path.join(pf, "Keysight", "IO Libraries Suite", "bin", "ktvisa32.dll"),
            os.path.join(pf86, "Keysight", "IO Libraries Suite", "bin", "ktvisa32.dll"),
            os.path.join(pf, "Keysight", "IO Libraries Suite", "bin", "agvisa32.dll"),
        ]
        out += _vendor_visa_dlls(bits64)
    seen, uniq = set(), []
    for c in out:
        if c not in seen and (c == "" or os.path.isfile(c)):
            uniq.append(c)
            seen.add(c)
    return uniq


def _is_pyvisa_py(rm) -> bool:
    return type(rm.visalib).__module__.startswith("pyvisa_py")


def _vendor_visa_dlls(bits64: bool) -> list[str]:
    """The vendors' own VISA libraries behind the IVI router (System32\\visa64.dll only forwards to
    the VISA registered as primary; if that registration is broken it fails with
    VI_ERROR_LIBRARY_NFOUND although Keysight Connection Expert works). Keysight first."""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    names = ({"visa64.dll", "ktvisa64.dll", "agvisa64.dll"} if bits64 else
             {"visa32.dll", "ktvisa32.dll", "agvisa32.dll"})
    roots = ([os.path.join(pf, "IVI Foundation", "VISA", "Win64"), os.path.join(pf, "Keysight", "IO Libraries Suite")]
             if bits64 else
             [os.path.join(pf86, "IVI Foundation", "VISA", "WinNT"), os.path.join(pf86, "Keysight", "IO Libraries Suite")])
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip("\\/").count(os.sep)
        for dirpath, dirnames, files in os.walk(root):
            if dirpath.count(os.sep) - base_depth >= 3:
                dirnames[:] = []                      # vendor DLLs sit at most 3 levels down
            for f in files:
                if f.lower() in names:
                    found.append(os.path.join(dirpath, f))
    rank = lambda p: (0 if "ktvisa" in p.lower() or "keysight" in p.lower() else
                      1 if "agvisa" in p.lower() else 2)
    return sorted(found, key=rank)


def resource_manager():
    """Shared ResourceManager (created on first use). An installed VISA library always wins:
    pyvisa's default lookup silently returns pyvisa-py when it finds none, so that case keeps
    searching the usual DLL locations before settling for pyvisa-py."""
    global _rm, _info
    if _rm is not None:
        return _rm
    if pyvisa is None:
        raise RuntimeError("pyvisa is not installed:  pip install pyvisa pyvisa-py")
    _failures.clear()
    _add_dll_dirs()
    fallback = None
    for lib in _candidates():
        if lib and sys.platform == "win32":
            try:
                os.add_dll_directory(os.path.dirname(lib))     # its dependencies sit next to it
            except (OSError, AttributeError):
                pass
        try:
            rm = pyvisa.ResourceManager(lib)
        except Exception as e:
            _failures.append(f"{lib or 'default VISA'}: {type(e).__name__}: {e}{_load_error(lib)}")
            continue
        if _is_pyvisa_py(rm):
            _failures.append("default lookup found no installed VISA library")
            fallback = rm
            continue
        _rm = rm
        _info = f"VISA: {getattr(rm.visalib, 'library_path', rm.visalib)}"
        return _rm
    _rm = fallback or pyvisa.ResourceManager("@py")
    _info = ("VISA: pyvisa-py (pure Python) - no installed VISA library could be loaded"
             + (f" ({' | '.join(_failures)})" if _failures else ""))
    return _rm


def info() -> str:
    try:
        resource_manager()
    except Exception as e:
        return f"VISA not available: {e}"
    return _info


def using_pyvisa_py() -> bool:
    return "pyvisa-py" in info()


def open_resource(resource: str, **kw):
    """rm.open_resource; backend problems (missing libusb, no VISA) become an explanatory RuntimeError,
    genuine VISA errors (e.g. resource not found) pass through unchanged."""
    try:
        return resource_manager().open_resource(resource, **kw)
    except Exception as e:
        if pyvisa is not None and isinstance(e, pyvisa.errors.VisaIOError) and not using_pyvisa_py():
            raise
        raise RuntimeError(explain(e, resource)) from e


def explain(e: Exception, resource: str = "") -> str:
    msg = f"{type(e).__name__}: {e}" if not str(e).startswith(type(e).__name__) else str(e)
    usb = resource.upper().startswith("USB")
    if using_pyvisa_py() and (usb or "pyusb" in msg.lower() or "no backend" in msg.lower()):
        why = ("\n  why the installed VISA was not used: " + " | ".join(_failures)) if _failures else ""
        if any("LIBRARY_NFOUND" in f and "[Windows" not in f for f in _failures):
            why += ("\n  visa64.dll itself loads, but it cannot open a VISA session - the IVI VISA router points "
                    "to a missing / non-primary VISA. In Keysight Connection Expert: Settings (gear) -> "
                    "'Keysight VISA is primary VISA' (or re-run the IO Libraries installer -> Repair).")
        bits = struct.calcsize("P") * 8
        return (f"This PC's VISA library (Keysight IO Libraries / NI-VISA) could not be loaded into {bits}-bit "
                f"Python ({sys.executable}), so the "
                "pure-Python fallback was used - it cannot open USB instruments that use Keysight's driver."
                f"{why}\n  Fix: restart Windows after installing IO Libraries Suite, and check that Python "
                f"and VISA match (64-bit Python needs the 64-bit VISA - Keysight installs both). "
                f"In Keysight Connection Expert the instrument must be listed. (original error: {msg})")
    return msg
