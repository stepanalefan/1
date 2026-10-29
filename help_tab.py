# -*- coding: utf-8 -*-
"""
help_tab.py
===========
Help tab: sub-tabs FAQ, Readme and About showing FAQ.md, README.md and ABOUT.md.

The files are read from the program folder first (so they can be edited without rebuilding),
then from the EXE's bundled copies. "Reload" re-reads them.
"""
from __future__ import annotations

import os
import sys

from PyQt5 import QtCore, QtGui, QtWidgets

PAGES = [("FAQ", "FAQ.md"), ("Readme", "README.md"), ("About", "ABOUT.md")]


def _find(name: str) -> str | None:
    dirs = [os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))]
    if getattr(sys, "_MEIPASS", None):
        dirs.append(sys._MEIPASS)
    for d in dirs:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


class HelpTab(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        self.tabs = QtWidgets.QTabWidget()
        self.views = {}
        for title, fname in PAGES:
            tb = QtWidgets.QTextBrowser()
            tb.setOpenExternalLinks(True)
            tb.setOpenLinks(False)                       # .md links switch sub-tab, web links open browser
            tb.anchorClicked.connect(self._link)
            tb.document().setDefaultStyleSheet(
                "table{border-collapse:collapse;} th,td{border:1px solid #30363d;padding:3px 6px;}"
                "code,pre{background:#161b22;} a{color:#58a6ff;}")
            pal = tb.palette()
            pal.setColor(QtGui.QPalette.Link, QtGui.QColor("#58a6ff"))
            tb.setPalette(pal)
            self.views[fname] = tb
            self.tabs.addTab(tb, title)
        v.addWidget(self.tabs, 1)
        bar = QtWidgets.QHBoxLayout()
        self.src = QtWidgets.QLabel()
        self.src.setStyleSheet("color:#8a96a3;font-size:11px;")
        rel = QtWidgets.QPushButton("Reload")
        rel.clicked.connect(self.load)
        bar.addWidget(self.src, 1)
        bar.addWidget(rel)
        v.addLayout(bar)
        self.tabs.currentChanged.connect(self._show_src)
        self.load()

    def load(self):
        self.paths = {}
        for _, fname in PAGES:
            p = _find(fname)
            self.paths[fname] = p
            tb = self.views[fname]
            if p:
                with open(p, encoding="utf-8") as f:
                    tb.setMarkdown(f.read())
                tb.document().setBaseUrl(QtCore.QUrl.fromLocalFile(os.path.dirname(p) + os.sep))
            else:
                tb.setMarkdown(f"# {fname} not found\n\nPut `{fname}` next to the program.")
        self._show_src()

    def _show_src(self, *_):
        fname = PAGES[self.tabs.currentIndex()][1]
        self.src.setText(self.paths.get(fname) or f"{fname}: not found")

    def _link(self, url: QtCore.QUrl):
        name = os.path.basename(url.path()).upper()
        for i, (_, fname) in enumerate(PAGES):
            if name == fname.upper():
                self.tabs.setCurrentIndex(i)
                return
        if url.scheme() in ("http", "https", "mailto"):
            QtGui.QDesktopServices.openUrl(url)
