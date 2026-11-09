"""Viewer side: connects to a host and shows its screen."""
import json, os, socket, struct, threading

from PyQt5 import QtCore, QtGui, QtWidgets

from . import protocol as P
from .netutil import connect, parse_hostport

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
        s = connect(relay[0], relay[1], timeout=15)
        P.send_line(s, {"role": "viewer", "id": t})
        r = P.recv_line(s)
        if not r.get("ok"):
            raise ConnectionError(r.get("error", "relay refused"))
    else:
        host, port = parse_hostport(target.strip(), 7071)
        s = connect(host, port, timeout=15)
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
        self.setCursor(QtCore.Qt.ArrowCursor)    # normal pointer (the remote cursor is not captured)

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
    frame_sig = QtCore.pyqtSignal(object)          # decoded video frame (QImage)

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
        self.frame_sig.connect(self._on_video_frame)
        self.setAcceptDrops(True)
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def _build_toolbar(self):
        tb = self.addToolBar("Session")
        tb.setMovable(False)
        # monitor buttons: one numbered button per remote monitor
        self.mon_box = QtWidgets.QWidget()
        mb = QtWidgets.QHBoxLayout(self.mon_box)
        mb.setContentsMargins(4, 0, 8, 0)
        mb.setSpacing(3)
        mb.addWidget(QtWidgets.QLabel("Monitor:"))
        self.mon_btns = QtWidgets.QButtonGroup(self)
        self.mon_btns.setExclusive(True)
        self.mon_btns.idClicked.connect(lambda i: self.send(P.SET_MONITOR, {"index": i}))
        self.mon_layout = mb
        self.mon_action = tb.addWidget(self.mon_box)
        self.mon_action.setVisible(False)
        self._monitors = None
        # display menu: picture mode (JPEG tiles / H.264 video) and quality
        self.settings = QtCore.QSettings("RemoteDesk", "Viewer")
        self.mode = self.settings.value("mode", "jpeg")
        self.quality_idx = int(self.settings.value("quality", 1))
        menu = QtWidgets.QMenu(self)
        menu.addSection("Picture")
        self.mode_group = QtWidgets.QActionGroup(self)
        for key, label in (("jpeg", "JPEG tiles — sharp text, low traffic when idle"),
                           ("video", "Video stream (H.264) — smooth motion and video")):
            a = menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(key == self.mode)
            a.setData(key)
            self.mode_group.addAction(a)
        self.mode_group.triggered.connect(lambda a: self._set_mode(a.data()))
        menu.addSection("Quality")
        self.qual_group = QtWidgets.QActionGroup(self)
        for i, label in enumerate(("Best quality", "Balanced", "Fast (slow networks)")):
            a = menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(i == self.quality_idx)
            a.setData(i)
            self.qual_group.addAction(a)
        self.qual_group.triggered.connect(lambda a: self._quality(a.data()))
        disp = QtWidgets.QToolButton()
        disp.setText("Display")
        disp.setMenu(menu)
        disp.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        tb.addWidget(disp)
        self.sound_action = tb.addAction("Sound")
        self.sound_action.setCheckable(True)
        self.sound_action.setChecked(self.settings.value("sound", "true") == "true")
        self.sound_action.toggled.connect(self._set_sound)
        tb.addSeparator()
        tb.addAction("Ctrl+Alt+Del", self._cad)
        tb.addAction("Send clipboard", lambda: self.send(
            P.CLIPBOARD, QtWidgets.QApplication.clipboard().text()))
        tb.addAction("Send file...", self._pick_file)
        self.chat_action = tb.addAction("Chat", self._toggle_chat)
        tb.addAction("Fullscreen", lambda: self.showNormal() if self.isFullScreen()
                     else self.showFullScreen())
        tb.addSeparator()
        tb.addAction("Disconnect", self.close)
        self.status = self.statusBar()
        self.role_lbl = QtWidgets.QLabel()
        self.role_lbl.setContentsMargins(8, 0, 8, 0)
        self.status.addPermanentWidget(self.role_lbl)

        # chat dock
        from .chat import ChatPanel
        self.chat = ChatPanel()
        self.chat.send_text.connect(self._send_chat)
        self.chat_dock = QtWidgets.QDockWidget("Chat", self)
        self.chat_dock.setWidget(self.chat)
        self.chat_dock.setFeatures(QtWidgets.QDockWidget.DockWidgetClosable |
                                   QtWidgets.QDockWidget.DockWidgetMovable)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.chat_dock)
        self.chat_dock.hide()
        self.chat_dock.visibilityChanged.connect(
            lambda vis: vis and (setattr(self, "_unread", 0), self.chat_action.setText("Chat")))
        self._unread = 0
        self.control = True             # until the host says otherwise (older hosts never do)
        self.my_name = socket.gethostname()
        self.send(P.HELLO, {"name": self.my_name})
        self._player = None
        self._decoder = None
        self._send_display()
        self._set_sound(self.sound_action.isChecked())

    def _set_monitors(self, monitors, current):
        key = (tuple((m["w"], m["h"]) for m in monitors))
        if key != self._monitors:
            self._monitors = key
            for b in self.mon_btns.buttons():
                self.mon_btns.removeButton(b)
                b.deleteLater()
            for i, m in enumerate(monitors):
                b = QtWidgets.QToolButton()
                b.setText(str(i + 1))
                b.setCheckable(True)
                b.setToolTip(f"Monitor {i + 1}: {m['w']}×{m['h']}")
                b.setMinimumWidth(30)
                b.setStyleSheet("QToolButton { border: 1px solid #C8CCD1; border-radius: 4px; padding: 3px 8px;"
                                " background: white; font-weight: 600; }"
                                "QToolButton:checked { background: #E5483E; color: white; border-color: #E5483E; }")
                self.mon_btns.addButton(b, i)
                self.mon_layout.addWidget(b)
            self.mon_action.setVisible(len(monitors) > 1)
        b = self.mon_btns.button(current)
        if b:
            b.setChecked(True)

    def _toggle_chat(self):
        self.chat_dock.setVisible(not self.chat_dock.isVisible())
        if self.chat_dock.isVisible():
            self.chat.input.setFocus()

    def _send_chat(self, text):
        import time as _t
        msg = {"from": self.my_name, "text": text, "ts": _t.time()}
        self.send(P.CHAT, msg)
        self.chat.add(None, text, msg["ts"], mine=True)

    def _set_role(self, role):
        self.control = bool(role.get("control"))
        others = [v["name"] for v in role.get("viewers", []) if v.get("name") != self.my_name]
        watchers = f" · {len(role.get('viewers', []))} connected" if len(role.get("viewers", [])) > 1 else ""
        if self.control:
            self.role_lbl.setText(f"<span style='color:#2EA44F'>●</span> You have control{watchers}")
        else:
            who = role.get("controller")
            self.role_lbl.setText(f"<span style='color:#E0A100'>●</span> View only"
                                  f"{f' — {who} has control' if who else ''}{watchers}")
        self.canvas.setToolTip("" if self.control else "View only: another user controls this computer")

    def send(self, t, payload=b""):
        if not getattr(self, "control", True) and t in (P.MOUSE, P.KEY, P.CLIPBOARD, P.FILE_BEGIN,
                                                         P.FILE_DATA, P.FILE_END):
            if t != P.MOUSE:
                self.status.showMessage("View only — another user has control", 3000)
            return
        try:
            self.chan.send(t, payload)
        except Exception:
            pass

    QUALITY = [{"quality": 85, "fps": 20}, {"quality": 60, "fps": 15}, {"quality": 35, "fps": 12}]

    def _quality(self, i):
        self.quality_idx = i
        self.settings.setValue("quality", i)
        self._send_display()

    def _set_mode(self, mode):
        self.mode = mode
        self.settings.setValue("mode", mode)
        self._send_display()

    def _send_display(self):
        q = dict(self.QUALITY[self.quality_idx])
        q["fps"] = q["fps"] + (10 if self.mode == "video" else 0)    # video can afford more frames
        q["mode"] = self.mode
        self.send(P.SET_QUALITY, q)

    def _set_sound(self, on):
        self.sound_action.setText("Sound: on" if on else "Sound: off")
        self.settings.setValue("sound", "true" if on else "false")
        self.send(P.AUDIO_CTRL, {"enabled": bool(on)})
        if on and self._player is None:
            from .media import AudioPlayer
            self._player = AudioPlayer(lambda m: self.sig.emit(-2, m.encode()))
        elif not on and self._player is not None:
            self._player.stop()
            self._player = None

    def _cad(self):
        for k in ("control", "alt", "delete"):
            self.send(P.KEY, {"key": k, "down": True})
        for k in ("delete", "alt", "control"):
            self.send(P.KEY, {"key": k, "down": False})

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
                if t == P.VIDEO:
                    self._decode_video(data)        # decoded here, off the UI thread
                    continue
                if t == P.AUDIO:
                    if self._player:
                        self._player.push(data)
                    continue
                self.sig.emit(t, data)
                if t == P.BYE:
                    break
        except Exception:
            self.sig.emit(P.BYE, b"")

    def _decode_video(self, data):
        try:
            if self._decoder is None:
                from .media import VideoDecoder
                self._decoder = VideoDecoder()
            rgb = self._decoder.decode(data[1:])
        except ImportError:
            self.sig.emit(-2, b"Video needs the 'av' package; switching to JPEG tiles")
            self.mode = "jpeg"
            self._send_display()
            return
        except Exception:
            self.send(P.KEYFRAME)                    # lost sync: ask for a fresh keyframe
            return
        if rgb is not None:
            h, w, _ = rgb.shape
            img = QtGui.QImage(rgb.data, w, h, 3 * w, QtGui.QImage.Format_RGB888).copy()
            self.frame_sig.emit(img)

    def _on_video_frame(self, img):
        self.canvas.img = img
        self.canvas.update()

    def _on_msg(self, t, data):
        if t == P.FRAME_INFO:
            info = json.loads(data)
            self._pending = QtGui.QImage(info["w"], info["h"], QtGui.QImage.Format_RGB32)
            self._pending.fill(QtGui.QColor(0, 0, 0))
            self._set_monitors(info.get("monitors") or [{"w": info["w"], "h": info["h"]}],
                               info.get("monitor", 0))
            self.status.showMessage(f"Remote screen {info['w']}x{info['h']}")
        elif t == P.ROLE:
            self._set_role(json.loads(data))
        elif t == P.AUDIO_CTRL:
            st = json.loads(data)
            if not st.get("available", True):
                self.status.showMessage(st.get("error") or "No sound from the remote computer", 8000)
                self.sound_action.setToolTip(st.get("error") or "")
        elif t == -2:                                   # local media message
            self.status.showMessage(data.decode(), 8000)
        elif t == P.CHAT:
            s = data.decode("utf-8", "replace")
            try:
                m = json.loads(s) if s.startswith("{") else {"text": s}
            except ValueError:
                m = {"text": s}
            self.chat.add(m.get("from"), m.get("text", ""), m.get("ts"))
            if not self.chat_dock.isVisible():
                self._unread += 1
                self.chat_action.setText(f"Chat ({self._unread})")
                self.chat_dock.show()
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
        if self._player:
            self._player.stop()
            self._player = None
        self.chan.close()
        super().closeEvent(e)
