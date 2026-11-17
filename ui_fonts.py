# -*- coding: utf-8 -*-
"""
ui_fonts.py
===========
Bundled interface fonts (folder fonts/ next to the program): Inter for the interface, DejaVu Sans Mono for frames,
hex dumps and logs - the same look on Windows and Linux. Both are free fonts (SIL Open Font License / Bitstream Vera
license, see fonts/LICENSE-*.txt). Start with --system-font to keep the Windows fonts (Segoe UI / Consolas).
"""

from __future__ import annotations

import os
import sys

from PyQt5 import QtGui, QtWidgets

UI_FAMILY = "Inter"
MONO_FAMILY = "DejaVu Sans Mono"


def _font_dir() -> str:
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.join(base, "fonts"), os.path.join(os.path.dirname(sys.executable), "fonts")):
        if os.path.isdir(d):
            return d
    return os.path.join(base, "fonts")


def install(app: QtWidgets.QApplication) -> list[str]:
    """Load the bundled fonts and make Inter the application font. Returns the families loaded."""
    loaded = set()
    d = _font_dir()
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            if name.lower().endswith((".otf", ".ttf")):
                fid = QtGui.QFontDatabase.addApplicationFont(os.path.join(d, name))
                if fid >= 0:
                    loaded.update(QtGui.QFontDatabase.applicationFontFamilies(fid))
    if UI_FAMILY in loaded or UI_FAMILY in QtGui.QFontDatabase().families():
        f = QtGui.QFont(UI_FAMILY, app.font().pointSize())
        f.setHintingPreference(QtGui.QFont.PreferNoHinting)        # Linux-like (FreeType) shapes on Windows too
        app.setFont(f)
    return sorted(loaded)


def mono(size: int | None = None) -> QtGui.QFont:
    """Monospaced font: DejaVu Sans Mono (bundled), else the system fixed font."""
    if MONO_FAMILY in QtGui.QFontDatabase().families():
        f = QtGui.QFont(MONO_FAMILY)
        f.setStyleHint(QtGui.QFont.TypeWriter)
        if size:
            f.setPointSize(size)
        return f
    return QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
