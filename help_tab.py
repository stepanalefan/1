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

# ---------------------------------------------------------------- Markdown -> styled HTML
# Qt's own setMarkdown() renders plain text-like pages; this converter covers what the help files
# use (headings, paragraphs, lists, tables, code blocks, inline code, bold / italic, links, rules)
# and styles it like GitHub's dark theme, within what Qt's rich text supports.
import html as _html
import re as _re

C = {"text": "#c9d1d9", "head": "#e6edf3", "h3": "#79c0ff", "dim": "#8b949e", "line": "#30363d",
     "code_bg": "#2d333b", "code": "#ffa657", "pre_bg": "#161b22", "th_bg": "#21262d", "alt_bg": "#161b22",
     "link": "#58a6ff"}


def _inline(t: str) -> str:
    codes = []

    def keep(m):
        codes.append(f'<code style="background:{C["code_bg"]};color:{C["code"]};'
                     f'font-family:Consolas,monospace;">&nbsp;{_html.escape(m.group(1))}&nbsp;</code>')
        return f"\x00{len(codes) - 1}\x00"
    t = _re.sub(r"`([^`]+)`", keep, t)
    t = _html.escape(t, quote=False)
    t = _re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                lambda m: f'<a href="{m.group(2)}" style="color:{C["link"]};text-decoration:none;">{m.group(1)}</a>', t)
    t = _re.sub(r"\*\*(.+?)\*\*", rf'<b style="color:{C["head"]};">\1</b>', t)
    t = _re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", t)
    t = _re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], t)
    return t


def md_to_html(md: str) -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    out, i, para = [], 0, []

    def flush():
        if para:
            out.append(f'<p style="margin:6px 0 10px 0;">{_inline(" ".join(para))}</p>')
            para.clear()

    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if s.startswith("```"):                                   # fenced code block
            flush()
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            out.append(f'<table width="100%" cellpadding="10" cellspacing="0" style="margin:6px 0 12px 0;">'
                       f'<tr><td style="background:{C["pre_bg"]};border:1px solid {C["line"]};">'
                       f'<pre style="font-family:Consolas,monospace;color:{C["text"]};margin:0;white-space:pre-wrap;">'
                       f'{_html.escape(chr(10).join(code))}</pre></td></tr></table>')
            continue
        m = _re.match(r"(#{1,4})\s+(.*)", s)
        if m:                                                      # heading
            flush()
            lvl, txt = len(m.group(1)), _inline(m.group(2))
            size = {1: 26, 2: 20, 3: 16, 4: 14}[lvl]
            color = C["h3"] if lvl >= 3 else C["head"]
            out.append(f'<h{lvl} style="font-size:{size}px;color:{color};margin:{18 if lvl < 3 else 12}px 0 4px 0;">'
                       f'{txt}</h{lvl}>')
            if lvl <= 2:
                out.append(f'<hr style="background-color:{C["line"]};height:1px;border:0;"/>')
            i += 1
            continue
        if _re.fullmatch(r"-{3,}|\*{3,}", s):                      # rule
            flush()
            out.append(f'<hr style="background-color:{C["line"]};height:1px;border:0;margin:10px 0;"/>')
            i += 1
            continue
        if s.startswith("|"):                                      # table
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            cells = [[c.strip() for c in _re.split(r"(?<!\\)\|", r.strip("|"))] for r in rows]
            body = [r for k, r in enumerate(cells) if not (k == 1 and all(_re.fullmatch(r":?-+:?", c or "-") for c in r))]
            h = ['<table cellspacing="0" cellpadding="7" style="margin:6px 0 14px 0;border-collapse:collapse;">']
            for k, r in enumerate(body):
                bg = C["th_bg"] if k == 0 else (C["alt_bg"] if k % 2 == 0 else "transparent")
                tag = "th" if k == 0 else "td"
                h.append("<tr>" + "".join(
                    f'<{tag} style="border:1px solid {C["line"]};background:{bg};'
                    f'{"color:" + C["head"] + ";font-weight:600;" if k == 0 else ""}text-align:left;">'
                    f'{_inline(c.replace(chr(92) + "|", "|"))}</{tag}>' for c in r) + "</tr>")
            h.append("</table>")
            out.append("".join(h))
            continue
        m = _re.match(r"(\s*)([*-]|\d+\.)\s+(.*)", ln)
        if m:                                                      # list (one nesting level)
            flush()
            ordered = m.group(2)[0].isdigit()
            items = []
            while i < len(lines):
                mm = _re.match(r"(\s*)([*-]|\d+\.)\s+(.*)", lines[i])
                if mm:
                    items.append([len(mm.group(1)), mm.group(3)])
                elif lines[i].startswith("  ") and lines[i].strip() and items:
                    items[-1][1] += " " + lines[i].strip()     # continuation line
                else:
                    break
                i += 1
            tag = "ol" if ordered else "ul"
            html_items, nested = [], []
            for ind, txt in items:
                if ind >= 2:
                    nested.append(f"<li>{_inline(txt)}</li>")
                    continue
                if nested and html_items:
                    html_items[-1] = html_items[-1][:-5] + f"<ul>{''.join(nested)}</ul></li>"
                    nested = []
                html_items.append(f'<li style="margin:3px 0;">{_inline(txt)}</li>')
            if nested and html_items:
                html_items[-1] = html_items[-1][:-5] + f"<ul>{''.join(nested)}</ul></li>"
            out.append(f'<{tag} style="margin:4px 0 10px 0;">{"".join(html_items)}</{tag}>')
            continue
        if not s:
            flush()
        else:
            para.append(s)
        i += 1
    flush()
    return (f'<body style="color:{C["text"]};font-size:14px;">' + "\n".join(out) + "</body>")



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
            tb.document().setDocumentMargin(18)
            tb.setStyleSheet("QTextBrowser{background:#0d1117;border:1px solid #30363d;}")
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
                    tb.setHtml(md_to_html(f.read()))
                tb.document().setBaseUrl(QtCore.QUrl.fromLocalFile(os.path.dirname(p) + os.sep))
            else:
                tb.setHtml(md_to_html(f"# {fname} not found\n\nPut `{fname}` next to the program."))
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
