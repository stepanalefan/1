"""RemoteDesk client: share this desk and connect to other desks.

  python -m remote_desk [--relay host:port] [--port 7071]
"""
import argparse, json, os, secrets, string, sys, threading, time

from PyQt5 import QtCore, QtGui, QtWidgets

from . import theme
from .host import Host
from .viewer import ViewerWindow, open_channel

CONFIG = os.path.join(os.path.expanduser("~"), ".remotedesk.json")


def load_cfg():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(cfg):
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)
    try:
        os.chmod(CONFIG, 0o600)
    except OSError:
        pass


def gen_password():
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(10))


def parse_hp(s, default_port):
    s = (s or "").strip()
    h, _, p = s.rpartition(":") if ":" in s else (s, "", "")
    return (h, int(p or default_port)) if h else None


def fmt_id(i):
    i = i.replace(" ", "")
    return f"{i[:3]} {i[3:6]} {i[6:]}" if i.isdigit() and len(i) == 9 else i


# ------------------------------------------------------------------ dialogs
class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(440)
        f = QtWidgets.QFormLayout(self)
        f.setSpacing(10)
        self.server = QtWidgets.QLineEdit(cfg.get("relay", ""))
        self.server.setPlaceholderText("e.g. 203.0.113.10:7070 or rd.example.com")
        f.addRow("Server:", self.server)
        hint = theme.label("Address of the computer running RemoteDesk Server. "
                           "Leave empty to use direct (LAN/VPN) connections only.", "Muted")
        hint.setWordWrap(True)
        f.addRow(hint)
        self.port = QtWidgets.QSpinBox(minimum=0, maximum=65535, value=cfg.get("port", 7071))
        self.port.setSpecialValueText("off")
        f.addRow("Direct port:", self.port)
        self.unattended = QtWidgets.QCheckBox("Unattended access (connect without accepting)")
        self.unattended.setChecked(cfg.get("unattended", False))
        self.view_only = QtWidgets.QCheckBox("Never allow remote control (view only)")
        self.view_only.setChecked(cfg.get("view_only", False))
        self.tray = QtWidgets.QCheckBox("Keep running in the tray when the window is closed")
        self.tray.setChecked(cfg.get("tray", True))
        for w in (self.unattended, self.view_only, self.tray):
            f.addRow(w)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save |
                                        QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _ok(self):
        if self.unattended.isChecked() and not self.parent().cfg.get("unattended"):
            if QtWidgets.QMessageBox.warning(
                    self, "Unattended access",
                    "Anyone who knows your ID and password will be able to connect "
                    "without confirmation.\nSet a strong password. Continue?",
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
                return
        self.accept()

    def values(self):
        return {"relay": self.server.text().strip(), "port": self.port.value(),
                "unattended": self.unattended.isChecked(),
                "view_only": self.view_only.isChecked(), "tray": self.tray.isChecked()}


class AcceptDialog(QtWidgets.QDialog):
    """Shown on the shared desk when someone wants to connect."""

    def __init__(self, parent, peer):
        super().__init__(parent, QtCore.Qt.WindowStaysOnTopHint)
        self.setWindowTitle("Incoming connection")
        self.setMinimumWidth(380)
        v = QtWidgets.QVBoxLayout(self)
        v.setSpacing(12)
        v.addWidget(theme.label("<b style='font-size:13pt'>Someone wants to connect</b>"))
        v.addWidget(theme.label(f"From: {peer}", "Muted"))
        v.addWidget(theme.label("They entered the correct password. Allow them to:"))
        self.control = QtWidgets.QCheckBox("Control mouse and keyboard")
        self.control.setChecked(True)
        v.addWidget(self.control)
        self.count = theme.label("", "Muted")
        v.addWidget(self.count)
        h = QtWidgets.QHBoxLayout()
        no = QtWidgets.QPushButton("Dismiss")
        no.clicked.connect(self.reject)
        yes = QtWidgets.QPushButton("Accept", objectName="Primary")
        yes.clicked.connect(self.accept)
        h.addStretch()
        h.addWidget(no)
        h.addWidget(yes)
        v.addLayout(h)
        self.left = 45
        self._tick()
        self.t = QtCore.QTimer(self, timeout=self._tick, interval=1000)
        self.t.start()

    def _tick(self):
        self.count.setText(f"Dismissed automatically in {self.left} s")
        self.left -= 1
        if self.left < 0:
            self.reject()


class ConnectDialog(QtWidgets.QDialog):
    """Asks for the password, connects in the background, shows progress."""
    finished_sig = QtCore.pyqtSignal(object, str)

    def __init__(self, parent, target, relay):
        super().__init__(parent)
        self.target, self.relay, self.chan = target, relay, None
        self.setWindowTitle(f"Connect to {fmt_id(target)}")
        self.setMinimumWidth(380)
        v = QtWidgets.QVBoxLayout(self)
        v.setSpacing(12)
        v.addWidget(theme.label(f"<b style='font-size:13pt'>{fmt_id(target)}</b>"))
        self.pw = QtWidgets.QLineEdit(echoMode=QtWidgets.QLineEdit.Password)
        self.pw.setPlaceholderText("Password of the remote desk")
        v.addWidget(self.pw)
        self.msg = theme.label("", "Muted")
        self.msg.setWordWrap(True)
        v.addWidget(self.msg)
        self.bar = QtWidgets.QProgressBar(maximum=0, textVisible=False, maximumHeight=6)
        self.bar.hide()
        v.addWidget(self.bar)
        h = QtWidgets.QHBoxLayout()
        h.addStretch()
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.go = QtWidgets.QPushButton("Connect", objectName="Primary")
        self.go.setDefault(True)
        self.go.clicked.connect(self._start)
        h.addWidget(cancel)
        h.addWidget(self.go)
        v.addLayout(h)
        self.finished_sig.connect(self._done)

    def _start(self):
        if not self.pw.text():
            return
        self.go.setEnabled(False)
        self.pw.setEnabled(False)
        self.bar.show()
        self.msg.setStyleSheet("")
        self.msg.setText("Connecting… waiting for the remote user to accept.")
        pw = self.pw.text()

        def work():
            try:
                self.finished_sig.emit(open_channel(self.target, pw, self.relay), "")
            except Exception as e:
                self.finished_sig.emit(None, str(e) or e.__class__.__name__)
        threading.Thread(target=work, daemon=True).start()

    def _done(self, chan, err):
        if not self.isVisible():          # cancelled meanwhile
            if chan:
                chan.close()
            return
        if chan:
            self.chan = chan
            self.accept()
            return
        self.bar.hide()
        self.go.setEnabled(True)
        self.pw.setEnabled(True)
        self.pw.selectAll()
        self.pw.setFocus()
        if "closed" in err:
            err = "The remote user declined, or the connection was closed."
        self.msg.setStyleSheet("color:#C0392B")
        self.msg.setText(err)


# ------------------------------------------------------------- main window
class MainWindow(QtWidgets.QMainWindow):
    host_sig = QtCore.pyqtSignal(str, object)

    def __init__(self, args):
        super().__init__()
        self.cfg = load_cfg()
        if args.relay:
            self.cfg["relay"] = args.relay
        if args.port is not None:
            self.cfg["port"] = args.port
        self.cfg.setdefault("port", 7071)
        self.cfg.setdefault("password", gen_password())
        self.quitting = False
        self.viewers = []
        self._accept_result = None
        self._accept_done = threading.Event()
        self.setWindowTitle("RemoteDesk")
        self.setWindowIcon(theme.app_icon("RemoteDesk"))
        self.resize(860, 540)
        self.host_sig.connect(self._on_event)
        self._build()
        self._tray()
        self._start_host()
        save_cfg(self.cfg)

    # ---------------------------------------------------------------- UI
    def _build(self):
        root = QtWidgets.QWidget(objectName="Body")
        v = QtWidgets.QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QtWidgets.QFrame(objectName="Header")
        h = QtWidgets.QHBoxLayout(header)
        h.setContentsMargins(18, 12, 18, 12)
        h.setSpacing(10)
        h.addWidget(theme.label("RemoteDesk", "Logo"))
        h.addSpacing(20)
        self.addr = QtWidgets.QComboBox(objectName="Address")
        self.addr.setEditable(True)
        self.addr.lineEdit().setPlaceholderText("Enter remote desk ID or address")
        self.addr.lineEdit().returnPressed.connect(self._connect_clicked)
        self.addr.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        h.addWidget(self.addr, 1)
        go = QtWidgets.QPushButton("Connect", objectName="HeaderBtn")
        go.clicked.connect(self._connect_clicked)
        h.addWidget(go)
        gear = QtWidgets.QPushButton("Settings", objectName="HeaderBtn")
        gear.clicked.connect(self._settings)
        h.addWidget(gear)
        v.addWidget(header)

        self.banner = QtWidgets.QPushButton(objectName="Danger")
        self.banner.hide()
        self.banner.clicked.connect(lambda: self.host.end_session())
        v.addWidget(self.banner)

        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(18, 18, 18, 18)
        body.setSpacing(16)
        v.addLayout(body, 1)

        # left: this desk
        c, l = theme.card("This Desk")
        l.addWidget(theme.label("Your address — give it to the person who will connect", "Muted"))
        row = QtWidgets.QHBoxLayout()
        self.id_lbl = theme.label("—", "BigId")
        self.id_lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        row.addWidget(self.id_lbl)
        row.addStretch()
        cp = QtWidgets.QPushButton("Copy", objectName="Icon")
        cp.clicked.connect(lambda: self._copy(self.cfg.get("id") or self.id_lbl.text()))
        row.addWidget(cp)
        l.addLayout(row)
        l.addSpacing(6)
        l.addWidget(theme.label("Password", "Muted"))
        row = QtWidgets.QHBoxLayout()
        self.pw = QtWidgets.QLineEdit(self.cfg["password"], echoMode=QtWidgets.QLineEdit.Password)
        self.pw.textChanged.connect(self._pw_changed)
        row.addWidget(self.pw, 1)
        for text, fn in (("Show", self._toggle_pw), ("Copy", lambda: self._copy(self.pw.text())),
                         ("New", lambda: self.pw.setText(gen_password()))):
            b = QtWidgets.QPushButton(text, objectName="Icon")
            b.clicked.connect(fn)
            row.addWidget(b)
            if text == "Show":
                self.show_btn = b
        l.addLayout(row)
        l.addSpacing(6)
        self.lan_lbl = theme.label("", "Muted")
        self.lan_lbl.setWordWrap(True)
        l.addWidget(self.lan_lbl)
        l.addStretch()
        body.addWidget(c, 2)

        # right: recent sessions
        c, l = theme.card("Recent sessions")
        self.recent_box = QtWidgets.QGridLayout()
        self.recent_box.setSpacing(10)
        l.addLayout(self.recent_box)
        self.recent_empty = theme.label("Desks you connect to will appear here.", "Muted")
        l.addWidget(self.recent_empty)
        l.addStretch()
        body.addWidget(c, 3)

        self.setCentralWidget(root)
        self.status_dot = theme.label("")
        self.statusBar().addWidget(self.status_dot)
        self.status_msg = theme.label("", "Muted")
        self.statusBar().addPermanentWidget(self.status_msg)
        self._refresh_recent()
        self._show_lan()

    def _show_lan(self):
        from .server_gui import local_addresses
        ips = local_addresses()
        if ips and self.cfg.get("port"):
            self.lan_lbl.setText("On the local network you can also connect to: "
                                 + ", ".join(f"{i}:{self.cfg['port']}" for i in ips))
        else:
            self.lan_lbl.setText("")

    def _refresh_recent(self):
        while self.recent_box.count():
            self.recent_box.takeAt(0).widget().deleteLater()
        recent = self.cfg.get("recent", [])
        self.recent_empty.setVisible(not recent)
        self.addr.blockSignals(True)
        text = self.addr.currentText()
        self.addr.clear()
        self.addr.addItems([fmt_id(r["target"]) for r in recent])
        self.addr.setCurrentText(text)
        self.addr.blockSignals(False)
        for n, r in enumerate(recent[:8]):
            when = time.strftime("%d %b %H:%M", time.localtime(r.get("time", 0)))
            sub = f"{fmt_id(r['target'])} · {when}" if r.get("name") else when
            b = QtWidgets.QPushButton(f"{r.get('name') or fmt_id(r['target'])}\n{sub}",
                                      objectName="Tile")
            b.setMinimumHeight(64)
            b.clicked.connect(lambda _, t=r["target"]: self.connect_to(t))
            b.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda _, t=r["target"]: self._tile_menu(t))
            self.recent_box.addWidget(b, n // 2, n % 2)

    def _tile_menu(self, target):
        m = QtWidgets.QMenu(self)
        m.addAction("Connect", lambda: self.connect_to(target))
        m.addAction("Rename…", lambda: self._rename(target))
        m.addAction("Remove", lambda: self._remove(target))
        m.exec_(QtGui.QCursor.pos())

    def _rename(self, target):
        name, ok = QtWidgets.QInputDialog.getText(self, "Rename", "Name for this desk:")
        if ok:
            for r in self.cfg.get("recent", []):
                if r["target"] == target:
                    r["name"] = name.strip()
            save_cfg(self.cfg)
            self._refresh_recent()

    def _remove(self, target):
        self.cfg["recent"] = [r for r in self.cfg.get("recent", []) if r["target"] != target]
        save_cfg(self.cfg)
        self._refresh_recent()

    def _tray(self):
        self.tray = QtWidgets.QSystemTrayIcon(self.windowIcon(), self)
        m = QtWidgets.QMenu()
        m.addAction("Open RemoteDesk", self._show)
        m.addAction("End remote session", lambda: self.host.end_session())
        m.addSeparator()
        m.addAction("Quit", self._quit)
        self.tray.setContextMenu(m)
        self.tray.activated.connect(lambda r: r == QtWidgets.QSystemTrayIcon.Trigger and self._show())
        self.tray.show()

    # ------------------------------------------------------------- host
    def _start_host(self):
        relay = parse_hp(self.cfg.get("relay"), 7070)
        self.relay = relay
        self.host = Host(self.pw.text(), self._host_event, direct_port=self.cfg["port"],
                         relay=relay, host_id=self.cfg.get("id"),
                         unattended=self.cfg.get("unattended", False),
                         view_only=self.cfg.get("view_only", False))
        if relay:
            self.id_lbl.setText(fmt_id(self.cfg["id"]) if self.cfg.get("id") else "…")
            self._set_online(False, f"Connecting to server {relay[0]}:{relay[1]}…")
        else:
            self.id_lbl.setText("No server set")
            self._set_online(False, "No server configured — open Settings to add one. "
                                    "Direct LAN connections still work.")
        self.host.start()

    def _set_online(self, on, text):
        self.status_dot.setText(f"{theme.dot(theme.OK if on else theme.BAD)} {text}")

    def _host_event(self, name, arg):           # called on worker threads
        if name == "accept":
            self._accept_done.clear()
            self.host_sig.emit(name, arg)
            self._accept_done.wait(60)
            return self._accept_result
        self.host_sig.emit(name, arg)

    def _on_event(self, name, arg):
        if name == "id":
            self.cfg["id"] = arg
            save_cfg(self.cfg)
            self.id_lbl.setText(fmt_id(arg))
        elif name == "online":
            if arg:
                self._set_online(True, "Ready to connect (secure connection)")
        elif name == "status":
            if "unreachable" in arg:
                self._set_online(False, arg)
            else:
                self.status_msg.setText(arg)
        elif name == "accept":
            self._show()
            d = AcceptDialog(self, arg)
            ok = d.exec_() == QtWidgets.QDialog.Accepted
            self._accept_result = {"control": d.control.isChecked()} if ok else False
            self._accept_done.set()
        elif name == "session_start":
            self.banner.setText(f"⚠  {arg} is connected to this computer — click here to end the session")
            self.banner.show()
            self.tray.showMessage("RemoteDesk", f"Remote session started ({arg})",
                                  QtWidgets.QSystemTrayIcon.Warning, 4000)
        elif name == "session_end":
            self.banner.hide()
        elif name == "clipboard":
            QtWidgets.QApplication.clipboard().setText(arg)
            self.tray.showMessage("RemoteDesk", "Clipboard received from remote user")
        elif name == "chat":
            QtWidgets.QMessageBox.information(self, "Message from remote user", arg)
        elif name == "file":
            self.tray.showMessage("RemoteDesk", f"File received:\n{arg}")
            self.status_msg.setText(f"File received: {arg}")

    # ---------------------------------------------------------- actions
    def _connect_clicked(self):
        t = self.addr.currentText().strip()
        if t:
            self.connect_to(t.replace(" ", "") if t.replace(" ", "").isdigit() else t)

    def connect_to(self, target):
        is_id = target.isdigit() and len(target) == 9
        if is_id and not self.relay:
            QtWidgets.QMessageBox.information(
                self, "RemoteDesk", "Connecting by ID needs a server. Open Settings and enter "
                "the address of your RemoteDesk Server, or connect directly by IP address.")
            return
        if is_id and target == self.cfg.get("id"):
            QtWidgets.QMessageBox.information(self, "RemoteDesk", "That is this desk's own ID.")
            return
        d = ConnectDialog(self, target, self.relay)
        if d.exec_() != QtWidgets.QDialog.Accepted or not d.chan:
            return
        recent = self.cfg.get("recent", [])
        old = next((r for r in recent if r["target"] == target), {})
        recent = [{"target": target, "time": time.time(), "name": old.get("name", "")}] + \
                 [r for r in recent if r["target"] != target]
        self.cfg["recent"] = recent[:20]
        save_cfg(self.cfg)
        self._refresh_recent()
        self.addr.setCurrentText("")
        name = old.get("name") or fmt_id(target)
        w = ViewerWindow(d.chan, name)
        w.setWindowIcon(self.windowIcon())
        self.viewers = [x for x in self.viewers if x.isVisible()] + [w]
        w.show()

    def _settings(self):
        d = SettingsDialog(self, self.cfg)
        if d.exec_() != QtWidgets.QDialog.Accepted:
            return
        new = d.values()
        self.cfg.update(new)
        save_cfg(self.cfg)
        self.host.unattended = new["unattended"]
        self.host.view_only = new["view_only"]
        if new["port"] != self.host.direct_port:
            self.status_msg.setText("The direct port change takes effect after a restart.")
        relay = parse_hp(new["relay"], 7070)
        if relay != self.relay:
            self.relay = relay
            self.host.set_relay(relay)
            if relay:
                self._set_online(False, f"Connecting to server {relay[0]}:{relay[1]}…")
                self.id_lbl.setText(fmt_id(self.cfg["id"]) if self.cfg.get("id") else "…")
            else:
                self.id_lbl.setText("No server set")
                self._set_online(False, "No server configured — direct LAN connections only.")
        self._show_lan()

    def _toggle_pw(self):
        hidden = self.pw.echoMode() == QtWidgets.QLineEdit.Password
        self.pw.setEchoMode(QtWidgets.QLineEdit.Normal if hidden else QtWidgets.QLineEdit.Password)
        self.show_btn.setText("Hide" if hidden else "Show")

    def _pw_changed(self, t):
        self.cfg["password"] = t
        self.host.password = t
        save_cfg(self.cfg)

    def _copy(self, text):
        QtWidgets.QApplication.clipboard().setText(text.replace(" ", ""))
        self.statusBar().showMessage("Copied to clipboard", 2000)

    def _show(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self):
        self.quitting = True
        self.host.end_session()
        QtWidgets.QApplication.quit()

    def closeEvent(self, e):
        if not self.quitting and self.cfg.get("tray", True) and self.tray.isVisible():
            e.ignore()
            self.hide()
            self.tray.showMessage("RemoteDesk", "RemoteDesk keeps running so this desk stays "
                                  "reachable. Right-click the tray icon to quit.",
                                  QtWidgets.QSystemTrayIcon.Information, 3000)
        else:
            self._quit()


def main():
    ap = argparse.ArgumentParser(description="RemoteDesk")
    ap.add_argument("--relay", help="server host:port (saved in settings)")
    ap.add_argument("--port", type=int, help="direct listen port, 0 = off (saved)")
    args = ap.parse_args()
    theme.set_windows_app_id("RemoteDesk.Client")
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("RemoteDesk")
    app.setWindowIcon(theme.app_icon("RemoteDesk"))
    app.setQuitOnLastWindowClosed(False)
    theme.apply(app)
    w = MainWindow(args)
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
