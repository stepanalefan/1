# -*- coding: utf-8 -*-
"""
PyInstaller runtime hook - used only for the EXE (build_exe.bat: --runtime-hook rthook_visa.py).

Custom runtime hooks run before PyInstaller's own ones. PyInstaller's PyQt5 hook imports
PyQt5.QtCore, which loads Qt's bundled (older) MSVCP140.dll. Keysight's VISA (ktvisa32.dll) needs the
newer MSVCP140.dll from System32 and cannot load next to the old one, so VISA then fails with
VI_ERROR_LIBRARY_NFOUND. Opening VISA here, before Qt, with Windows' normal DLL search (PyInstaller
has set SetDllDirectory to its unpack folder) loads the System32 runtime first; Qt then reuses it.
fsw_vsa_monitor.py picks up the ResourceManager from sys._fsw_early_visa.
"""
import sys


def _open_visa_first():
    if sys.platform != "win32":
        return
    import ctypes
    k32 = ctypes.windll.kernel32
    unpack_dir = getattr(sys, "_MEIPASS", None)
    try:
        k32.SetDllDirectoryW(None)
        import pyvisa
        rm = pyvisa.ResourceManager()
        if type(rm.visalib).__module__.startswith("pyvisa_py"):
            sys._fsw_early_visa_err = "runtime hook: default lookup gave pyvisa-py"
        else:
            sys._fsw_early_visa = rm
    except Exception as e:                  # no VISA installed: the program falls back later
        sys._fsw_early_visa_err = f"runtime hook: {type(e).__name__}: {e}"
    finally:
        if unpack_dir:
            k32.SetDllDirectoryW(unpack_dir)


_open_visa_first()
del _open_visa_first
