"""Viewer side: connects to a host and shows its screen."""
import json, os, socket, struct, threading

from PyQt5 import QtCore, QtGui, QtWidgets

from . import protocol as P

QT_KEYS = {
    QtCore.Qt.Key_Return: "enter", QtCore.Qt.Key_Enter: "enter", QtCore.Qt.Key_Tab: "tab",
    QtCore.Qt.Key_Backspace: "backspace", QtCore.Qt.Key_Escape: "escape",
    QtCore.Qt.Key_Delete: "delete", QtCore.Qt.Key_Insert: "insert",
    QtCore.Qt.Key_Home: "home", QtCore.Qt.Key_End: "end",
    QtCore.Qt.Key_PageUp: "pageup", QtCore.Qt.Key_PageDown: "pagedown",
    QtCore.Qt.Key_Left: "left", QtCore.Qt.Key_Right: "right", QtCore.Qt.Key_Up: "up",
    QtCore.Qt.Key_Down: "down", QtCore.Qt.Key_Shift: "shift",
    QtCore.Qt.Key_Control: "control", QtCore.Qt.Key_Alt: "alt", QtCore.Qt.Key_Meta: "meta",
    QtCore.Qt.Key_Space: "space", QtCore.Qt.Key_CapsLock: "capslock",
    QtCore.Qt.Key_Print: "printscreen", QtCore.Qt.Key_Menu: "menu",
    **{getattr(QtCore.Qt, f"Key_F{i}"): f"f{i}" for i in range(1, 21)},
}


def open_channel(target, password, relay):
    """target: 9-digit ID (via relay) or host[:port] (direct)."""
    t = target.replace(" ", "")
    if t.isdigit() and len(t) == 9:
        if not relay:
            raise ConnectionError("no relay server configured")
        s = socket.create_connection(relay, timeout=15)
        P.send_line(s, {"role": "viewer", "id": t})
        r = P.recv_line(s)
        if not r.get("ok"):
            raise ConnectionError(r.get("error", "relay refused"))
    else:
        host, _, port = t.partition(":")
        s = socket.create_connection((host, int(port or 7071)), timeout=15)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.settimeout(120)  # host user may need time to accept
    chan = P.viewer_handshake(s, password)
    s.settimeout(None)
    return chan


class Canvas(QtWidgets.QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.img = QtGui.QImage()
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setCursor(QtCore.Qt.CrossCursor)

    def target_rect(self):
        if self.img.isNull():
            return QtCore.QRectF()
        s = self.img.size().scaled(self.size(), QtCore.Qt.KeepAspectRatio)
        return QtCore.QRectF((self.width() - s.width()) / 2, (self.height() - s.height()) / 2,
                             s.width(), s.height())

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor(30, 30, 30))
        if not self.img.isNull():
            p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
            p.drawImage(self.target_rect(), self.img)

    def _norm(self, pos):
        r = self.target_rect()
        if r.isEmpty():
            return None
        x, y = (pos.x() - r.x()) / r.width(), (pos.y() - r.y()) / r.height()
        return max(0.0, min(1.0, x)), max(0.0, min(1.0, y))

    def _mouse(self, e, action, btn=None):
        n = self._norm(e.pos())
        if n:
            self.win.send(P.MOUSE, {"x": n[0], "y": n[1], "action": action, "btn": btn})

    BTN = {QtCore.Qt.LeftButton: "left", QtCore.Qt.RightButton: "right",
           QtCore.Qt.MiddleButton: "middle"}

    def mouseMoveEvent(self, e): self._mouse(e, "move")
    def mousePressEvent(self, e): self._mouse(e, "down", self.BTN.get(e.button()))
    def mouseReleaseEvent(self, e): self._mouse(e, "up", self.BTN.get(e.button()))

    def wheelEvent(self, e):
        n = self._norm(e.pos())
        if n:
            d = e.angleDelta()
            self.win.send(P.MOUSE, {"x": n[0], "y": n[1], "action": "wheel",
                                    "dx": d.x() // 120, "dy": d.y() // 120})

    def _key(self, e, down):
        if e.isAutoRepeat() and not down:
            return
        name = QT_KEYS.get(e.key())
        text = e.text() if e.text() and e.text().isprintable() else ""
        if not name and not text and 0x20 <= e.key() < 0x7f:
            text = chr(e.key()).lower()   # e.g. Ctrl+C gives no text
        if name or text:
            self.win.send(P.KEY, {"key": name or "", "text": text, "down": down})

    def keyPressEvent(self, e): self._key(e, True)
    def keyReleaseEvent(self, e): self._key(e, False)

    def focusNextPrevChild(self, _):  # keep Tab for the remote side
        return False


class ViewerWindow(QtWidgets.QMainWindow):
    sig = QtCore.pyqtSignal(int, bytes)

    def __init__(self, chan, title):
        super().__init__()
        self.chan = chan
        self.setWindowTitle(f"RemoteDesk - {title}")
        self.resize(1280, 800)
        self.canvas = Canvas(self)
        self.setCentralWidget(self.canvas)
        self._pending = QtGui.QImage()
        self._out_file = None
        self._build_toolbar()
        self.sig.connect(self._on_msg)
        self.setAcceptDrops(True)
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def _build_toolbar(self):
        tb = self.addToolBar("Session")
        tb.setMovable(False)
        self.mon = QtWidgets.QComboBox()
        self.mon.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        self.mon.currentIndexChanged.connect(
            lambda i: i >= 0 and self.send(P.SET_MONITOR, {"index": i}))
        tb.addWidget(QtWidgets.QLabel(" Monitor: "))
        tb.addWidget(self.mon)
        self.qual = QtWidgets.QComboBox()
        self.qual.addItems(["Best quality", "Balanced", "Fast"])
        self.qual.setCurrentIndex(1)
        self.qual.currentIndexChanged.connect(self._quality)
        tb.addWidget(QtWidgets.QLabel("  Mode: "))
        tb.addWidget(self.qual)
        tb.addSeparator()
        tb.addAction("Ctrl+Alt+Del", self._cad)
        tb.addAction("Send clipboard", lambda: self.send(
            P.CLIPBOARD, QtWidgets.QApplication.clipboard().text()))
        tb.addAction("Send file...", self._pick_file)
        tb.addAction("Chat...", self._chat)
        tb.addAction("Fullscreen", lambda: self.showNormal() if self.isFullScreen()
                     else self.showFullScreen())
        tb.addSeparator()
        tb.addAction("Disconnect", self.close)
        self.status = self.statusBar()

    def send(self, t, payload=b""):
        try:
            self.chan.send(t, payload)
        except Exception:
            pass

    def _quality(self, i):
        self.send(P.SET_QUALITY, [{"quality": 85, "fps": 20}, {"quality": 60, "fps": 15},
                                  {"quality": 35, "fps": 12}][i])

    def _cad(self):
        for k in ("control", "alt", "delete"):
            self.send(P.KEY, {"key": k, "down": True})
        for k in ("delete", "alt", "control"):
            self.send(P.KEY, {"key": k, "down": False})

    def _chat(self):
        text, ok = QtWidgets.QInputDialog.getText(self, "Chat", "Message to remote user:")
        if ok and text:
            self.send(P.CHAT, text)

    def _pick_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Send file")
        if path:
            self.send_file(path)

    def send_file(self, path):
        def work():
            size = os.path.getsize(path)
            self.send(P.FILE_BEGIN, {"name": os.path.basename(path), "size": size})
            with open(path, "rb") as f:
                while chunk := f.read(256 * 1024):
                    self.send(P.FILE_DATA, chunk)
            self.send(P.FILE_END)
        threading.Thread(target=work, daemon=True).start()
        self.status.showMessage(f"Sending {os.path.basename(path)}...", 4000)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            if u.isLocalFile() and os.path.isfile(u.toLocalFile()):
                self.send_file(u.toLocalFile())

    def _recv_loop(self):
        try:
            while True:
                t, data = self.chan.recv()
                self.sig.emit(t, data)
                if t == P.BYE:
                    break
        except Exception:
            self.sig.emit(P.BYE, b"")

    def _on_msg(self, t, data):
        if t == P.FRAME_INFO:
            info = json.loads(data)
            self._pending = QtGui.QImage(info["w"], info["h"], QtGui.QImage.Format_RGB32)
            self._pending.fill(QtGui.QColor(0, 0, 0))
            self.mon.blockSignals(True)
            self.mon.clear()
            self.mon.addItems([f"{i + 1}: {m['w']}x{m['h']}" for i, m in enumerate(info["monitors"])])
            self.mon.setCurrentIndex(info["monitor"])
            self.mon.blockSignals(False)
            self.status.showMessage(f"Remote screen {info['w']}x{info['h']}")
        elif t == P.TILE:
            x, y = struct.unpack(">HH", data[:4])
            tile = QtGui.QImage.fromData(data[4:], "JPEG")
            p = QtGui.QPainter(self._pending)
            p.drawImage(x, y, tile)
            p.end()
        elif t == P.FRAME_END:
            self.canvas.img = self._pending.copy()
            self.canvas.update()
        elif t == P.BYE:
            self.status.showMessage("Disconnected")
            self.chan.closed = True
            QtWidgets.QMessageBox.information(self, "RemoteDesk", "The session has ended.")
            self.close()

    def closeEvent(self, e):
        self.chan.close()
        super().closeEvent(e)
