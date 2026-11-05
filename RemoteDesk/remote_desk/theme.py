"""Shared look & feel for the RemoteDesk client and server apps."""
import os, sys

from PyQt5 import QtCore, QtGui, QtWidgets

ACCENT = "#E5483E"
ACCENT_DARK = "#C23A31"
SERVER_ACCENT = "#2F6FDB"
OK = "#2EA44F"
BAD = "#9AA0A6"

STYLE = """
QWidget { font-family: "Segoe UI", "Helvetica Neue", Arial; font-size: 10pt; color: #1f2328; }
QMainWindow, #Body { background: #F3F4F6; }
#Header { background: %(accent)s; }
#Header QLabel { color: white; }
#Logo { font-size: 15pt; font-weight: 600; }
#Card { background: white; border: 1px solid #E3E5E8; border-radius: 10px; }
#CardTitle { font-size: 11pt; font-weight: 600; color: #3a3f45; }
#Muted { color: #6b7178; }
#BigId { font-size: 24pt; font-weight: 600; color: #1f2328; letter-spacing: 1px; }
#StatValue { font-size: 20pt; font-weight: 600; }
#Address { background: white; border: none; border-radius: 6px; padding: 7px 10px;
           font-size: 12pt; min-width: 260px; }
QPushButton { background: white; border: 1px solid #D0D4D9; border-radius: 6px; padding: 6px 14px; }
QPushButton:hover { background: #F6F7F8; }
QPushButton:disabled { color: #A0A4A8; }
QPushButton#Primary { background: %(accent)s; color: white; border: none; font-weight: 600; }
QPushButton#Primary:hover { background: %(dark)s; }
QPushButton#HeaderBtn { background: rgba(255,255,255,0.18); color: white; border: none; font-weight: 600; }
QPushButton#HeaderBtn:hover { background: rgba(255,255,255,0.30); }
QPushButton#Icon { border: none; background: transparent; padding: 4px; color: #6b7178; }
QPushButton#Icon:hover { background: #EEF0F2; }
QPushButton#Tile { text-align: left; padding: 12px; border-radius: 8px; }
QPushButton#Danger { background: #C0392B; color: white; border: none; font-weight: 600; padding: 8px; }
QLineEdit, QSpinBox, QComboBox { background: white; border: 1px solid #D0D4D9; border-radius: 6px; padding: 5px 8px; }
QTableWidget { background: white; border: none; gridline-color: #EEF0F2; }
QHeaderView::section { background: #F7F8F9; border: none; border-bottom: 1px solid #E3E5E8; padding: 6px; font-weight: 600; }
QTabWidget::pane { border: 1px solid #E3E5E8; border-radius: 8px; background: white; top: -1px; }
QTabBar::tab { padding: 8px 18px; background: transparent; border: none; color: #6b7178; }
QTabBar::tab:selected { color: #1f2328; border-bottom: 2px solid %(accent)s; font-weight: 600; }
QPlainTextEdit { background: white; border: none; font-family: Consolas, monospace; font-size: 9pt; }
QCheckBox { spacing: 8px; }
QStatusBar { background: white; border-top: 1px solid #E3E5E8; }
"""


def apply(app, accent=ACCENT, dark=ACCENT_DARK):
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE % {"accent": accent, "dark": dark})


def asset_path(name):
    """assets/<name> next to the sources, or inside a PyInstaller bundle."""
    roots = [getattr(sys, "_MEIPASS", ""),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)]
    for root in roots:
        p = os.path.join(root, "assets", name)
        if root and os.path.exists(p):
            return p
    return None


def app_icon(name="RemoteDesk"):
    """Icon from assets/<name>.ico (all sizes); falls back to a drawn one."""
    path = asset_path(name + ".ico") or asset_path(name + ".png")
    if path:
        icon = QtGui.QIcon(path)
        if not icon.isNull():
            return icon
    if name == "RemoteDeskServer":
        return make_icon(SERVER_ACCENT, "S")
    return make_icon()


def set_windows_app_id(app_id):
    """Lets the Windows taskbar show our icon instead of python.exe's."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except Exception:
            pass


def make_icon(color=ACCENT, letter="R"):
    pm = QtGui.QPixmap(64, 64)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setBrush(QtGui.QColor(color))
    p.setPen(QtCore.Qt.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    p.setBrush(QtGui.QColor("white"))
    p.drawRoundedRect(14, 18, 36, 24, 4, 4)       # monitor
    p.drawRect(29, 42, 6, 6)
    p.drawRoundedRect(22, 47, 20, 4, 2, 2)
    p.setPen(QtGui.QColor(color))
    f = QtGui.QFont("Arial", 13, QtGui.QFont.Bold)
    p.setFont(f)
    p.drawText(QtCore.QRect(14, 18, 36, 24), QtCore.Qt.AlignCenter, letter)
    p.end()
    return QtGui.QIcon(pm)


def card(title=None):
    w = QtWidgets.QFrame()
    w.setObjectName("Card")
    lay = QtWidgets.QVBoxLayout(w)
    lay.setContentsMargins(18, 16, 18, 16)
    lay.setSpacing(10)
    if title:
        t = QtWidgets.QLabel(title)
        t.setObjectName("CardTitle")
        lay.addWidget(t)
    return w, lay


def dot(color):
    return f'<span style="color:{color}; font-size:14pt">●</span>'


def label(text, name=None):
    l = QtWidgets.QLabel(text)
    if name:
        l.setObjectName(name)
    return l
