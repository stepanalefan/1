"""Chat panel shared by the host window and the viewer window."""
import html
import time

from PyQt5 import QtCore, QtGui, QtWidgets

from . import theme


class ChatPanel(QtWidgets.QWidget):
    """Message list with bubbles and an input line. Emits send_text(str)."""
    send_text = QtCore.pyqtSignal(str)

    def __init__(self, parent=None, placeholder="Type a message…"):
        super().__init__(parent)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)
        self.view = QtWidgets.QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self.view.setStyleSheet("QTextBrowser { background: #F7F8FA; border: 1px solid #E3E5E8;"
                                " border-radius: 8px; padding: 6px; }")
        v.addWidget(self.view, 1)
        row = QtWidgets.QHBoxLayout()
        self.input = QtWidgets.QLineEdit(placeholderText=placeholder)
        self.input.returnPressed.connect(self._send)
        row.addWidget(self.input, 1)
        b = QtWidgets.QPushButton("Send", objectName="Primary")
        b.clicked.connect(self._send)
        row.addWidget(b)
        v.addLayout(row)
        self._html = []
        self.info("Messages are end-to-end encrypted.")

    def _send(self):
        t = self.input.text().strip()
        if t:
            self.input.clear()
            self.send_text.emit(t)

    def _render(self):
        self.view.setHtml("".join(self._html))
        self.view.moveCursor(QtGui.QTextCursor.End)

    def add(self, sender, text, ts=None, mine=False):
        when = time.strftime("%H:%M", time.localtime(ts or time.time()))
        body = html.escape(text).replace("\n", "<br>")
        color, align, bg = ("#ffffff", "right", theme.ACCENT) if mine else ("#1f2328", "left", "#ffffff")
        name = "You" if mine else html.escape(sender or "Remote user")
        self._html.append(
            f'<table width="100%" cellspacing="0" cellpadding="0" style="margin:4px 0"><tr>'
            f'<td align="{align}"><div style="color:#6b7178;font-size:8pt">{name} · {when}</div>'
            f'<table cellpadding="8" style="background:{bg};border:1px solid #E3E5E8">'
            f'<tr><td style="color:{color}">{body}</td></tr></table></td></tr></table>')
        self._render()

    def info(self, text):
        self._html.append(f'<p align="center" style="color:#9AA0A6;font-size:8pt">{html.escape(text)}</p>')
        self._render()


class ChatWindow(QtWidgets.QDialog):
    """Stand-alone chat window (used on the shared desk)."""

    def __init__(self, parent, title="Chat"):
        super().__init__(parent, QtCore.Qt.Window)
        self.setWindowTitle(title)
        self.resize(380, 480)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.panel = ChatPanel(self)
        lay.addWidget(self.panel)
