"""RemoteDesk main window.

  python -m remote_desk.app [--relay host:port] [--port 7071]
"""
import argparse, json, os, secrets, string, sys, threading

from PyQt5 import QtCore, QtWidgets

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
    h, _, p = s.partition(":")
    return (h, int(p or default_port)) if h else None


class MainWindow(QtWidgets.QWidget):
    host_sig = QtCore.pyqtSignal(str, object)

    def __init__(self, args):
        super().__init__()
        self.cfg = load_cfg()
        relay = args.relay or self.cfg.get("relay", "")
        self.cfg["relay"] = relay
        self.relay = parse_hp(relay, 7070) if relay else None
        self.setWindowTitle("RemoteDesk")
        self.resize(720, 360)
        self.viewers = []
        self._accept_result = None
        self._accept_done = threading.Event()

        # left: this desk
        self.id_lbl = QtWidgets.QLabel("—")
        self.id_lbl.setStyleSheet("font-size: 26px; font-weight: bold;")
        self.id_lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.pw = QtWidgets.QLineEdit(self.cfg.get("password") or gen_password())
        self.pw.setEchoMode(QtWidgets.QLineEdit.PasswordEchoOnEdit)
        new_pw = QtWidgets.QPushButton("New password")
        new_pw.clicked.connect(lambda: self.pw.setText(gen_password()))
        self.pw.textChanged.connect(self._pw_changed)
        self.unattended = QtWidgets.QCheckBox("Unattended access (no accept prompt)")
        self.unattended.setChecked(self.cfg.get("unattended", False))
        self.unattended.toggled.connect(self._opt_changed)
        self.view_only = QtWidgets.QCheckBox("View only (block remote input)")
        self.view_only.setChecked(self.cfg.get("view_only", False))
        self.view_only.toggled.connect(self._opt_changed)
        self.status = QtWidgets.QLabel("starting...")
        self.status.setWordWrap(True)
        self.banner = QtWidgets.QPushButton()
        self.banner.setStyleSheet("background:#c0392b;color:white;font-weight:bold;padding:6px")
        self.banner.hide()
        self.banner.clicked.connect(self._end_session)

        left = QtWidgets.QGroupBox("This desk")
        lf = QtWidgets.QFormLayout(left)
        lf.addRow("Your ID:", self.id_lbl)
        pwrow = QtWidgets.QHBoxLayout()
        pwrow.addWidget(self.pw)
        pwrow.addWidget(new_pw)
        lf.addRow("Password:", pwrow)
        lf.addRow(self.unattended)
        lf.addRow(self.view_only)
        lf.addRow(self.status)
        lf.addRow(self.banner)

        # right: remote desk
        self.target = QtWidgets.QComboBox()
        self.target.setEditable(True)
        self.target.addItems(self.cfg.get("recent", []))
        self.target.setCurrentText("")
        self.target.lineEdit().setPlaceholderText("Remote ID or host[:port]")
        self.target.lineEdit().returnPressed.connect(self._connect)
        go = QtWidgets.QPushButton("Connect")
        go.clicked.connect(self._connect)
        self.relay_edit = QtWidgets.QLineEdit(relay)
        self.relay_edit.setPlaceholderText("relay host:port (optional)")
        self.relay_edit.editingFinished.connect(self._relay_changed)
        right = QtWidgets.QGroupBox("Remote desk")
        rf = QtWidgets.QFormLayout(right)
        rf.addRow(self.target)
        rf.addRow(go)
        rf.addRow("Relay:", self.relay_edit)

        lay = QtWidgets.QHBoxLayout(self)
        lay.addWidget(left, 1)
        lay.addWidget(right, 1)

        self.host_sig.connect(self._on_event)
        self.host = Host(self.pw.text(), self._host_event, direct_port=args.port,
                         relay=self.relay, host_id=self.cfg.get("id"),
                         unattended=self.unattended.isChecked(),
                         view_only=self.view_only.isChecked())
        if not self.relay:
            self.id_lbl.setText("(direct only)")
        self.host.start()
        self._save()

    # --- host callbacks arrive on worker threads
    def _host_event(self, name, arg):
        if name == "accept":
            self._accept_done.clear()
            self.host_sig.emit(name, arg)
            self._accept_done.wait(60)
            return bool(self._accept_result)
        self.host_sig.emit(name, arg)

    def _on_event(self, name, arg):
        if name == "id":
            self.id_lbl.setText(f"{arg[:3]} {arg[3:6]} {arg[6:]}")
            self.cfg["id"] = arg
            self._save()
        elif name == "status":
            self.status.setText(arg)
        elif name == "accept":
            self.raise_()
            self.activateWindow()
            r = QtWidgets.QMessageBox.question(
                self, "Incoming connection",
                f"A remote user ({arg}) wants to view and control this computer.\nAllow?")
            self._accept_result = r == QtWidgets.QMessageBox.Yes
            self._accept_done.set()
        elif name == "session_start":
            self.banner.setText(f"Remote session active ({arg}) — click to end")
            self.banner.show()
            self.showNormal()
        elif name == "session_end":
            self.banner.hide()
        elif name == "clipboard":
            QtWidgets.QApplication.clipboard().setText(arg)
        elif name == "chat":
            QtWidgets.QMessageBox.information(self, "Message from remote user", arg)
        elif name == "file":
            self.status.setText(f"Received file: {arg}")

    def _end_session(self):
        self.host.end_session()

    def _pw_changed(self, t):
        self.host.password = t
        self._save()

    def _opt_changed(self):
        self.host.unattended = self.unattended.isChecked()
        self.host.view_only = self.view_only.isChecked()
        self._save()

    def _relay_changed(self):
        self.cfg["relay"] = self.relay_edit.text().strip()
        self._save()
        self.status.setText("Relay changed; restart RemoteDesk to apply.")

    def _save(self):
        self.cfg.update(password=self.pw.text(), unattended=self.unattended.isChecked(),
                        view_only=self.view_only.isChecked())
        save_cfg(self.cfg)

    def _connect(self):
        target = self.target.currentText().strip()
        if not target:
            return
        pw, ok = QtWidgets.QInputDialog.getText(self, "Password", f"Password for {target}:",
                                                QtWidgets.QLineEdit.Password)
        if not ok:
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            chan = open_channel(target, pw, self.relay)
        except Exception as e:
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.warning(self, "RemoteDesk", f"Connection failed:\n{e}")
            return
        QtWidgets.QApplication.restoreOverrideCursor()
        recent = [target] + [r for r in self.cfg.get("recent", []) if r != target]
        self.cfg["recent"] = recent[:10]
        self._save()
        v = ViewerWindow(chan, target)
        self.viewers.append(v)
        v.show()


def main():
    ap = argparse.ArgumentParser(description="RemoteDesk")
    ap.add_argument("--relay", help="relay server host:port")
    ap.add_argument("--port", type=int, default=7071, help="direct listen port (0 = off)")
    args = ap.parse_args()
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow(args)
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
