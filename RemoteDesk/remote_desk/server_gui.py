"""RemoteDesk Server - GUI for the relay. Run it on the computer that both
sides can reach (VPS, office server, home PC with port forwarding)."""
import json, os, socket, sys, threading, time

from PyQt5 import QtCore, QtGui, QtWidgets

from . import theme
from .netutil import format_hostport, local_addresses
from .relay import Registry, Relay

CONFIG = os.path.join(os.path.expanduser("~"), ".remotedesk_server.json")


def load_cfg():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except Exception:
        return {}


def fmt_age(t):
    s = int(time.time() - t)
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s"


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


SERVICE_UNIT = "/etc/systemd/system/remotedesk-server.service"
SERVICE_NAME = "remotedesk-server"
SERVICE_SOCKET = "/run/remotedesk/admin.sock"


class BackendError(Exception):
    pass


class LocalBackend:
    """The server runs inside this window (Windows, or Linux without the service)."""
    kind = "local"

    def __init__(self, registry):
        self.registry = registry
        self.relay = None

    @property
    def running(self):
        return self.relay is not None

    def start(self, port):
        relay = Relay("", port, registry=self.registry)
        relay.start()                          # raises OSError if the port is busy
        self.relay = relay

    def stop(self):
        if self.relay:
            self.relay.stop()
            self.relay = None

    def call(self, req):
        if self.relay:
            return self.relay.admin(req)
        # stopped: the desk list can still be viewed and edited
        cmd = req["cmd"]
        if cmd == "list":
            return {"ok": True, "desks": [{"id": d["id"], "name": d.get("name", ""), "online": False,
                                           "ip": d.get("ip", ""), "since": None,
                                           "last_seen": d.get("last_seen")} for d in self.registry.list()]}
        if cmd == "set-id":
            try:
                self.registry.change_id(req["old"], req["new"])
            except (ValueError, KeyError) as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True}
        if cmd == "delete":
            self.registry.delete(req["id"])
            return {"ok": True}
        if cmd == "set-option":
            self.registry.match_by_name = bool(req.get("match_by_name"))
            self.registry.save()
            return {"ok": True}
        if cmd == "sessions":
            return {"ok": True, "sessions": []}
        if cmd == "log":
            return {"ok": True, "lines": []}
        if cmd == "stats":
            return {"ok": True, "total_sessions": 0, "match_by_name": self.registry.match_by_name}
        return {"ok": False, "error": "server is stopped"}


class ServiceBackend:
    """The server runs as the systemd service (installed by linux/install_server.sh);
    this window manages it through the admin socket."""
    kind = "service"

    def call(self, req):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        try:
            s.connect(SERVICE_SOCKET)
            s.sendall(json.dumps(req).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf or b"{}")
        except PermissionError:
            raise BackendError("no permission to manage the service — log out and in again, or run: "
                               "sudo usermod -aG remotedesk $USER")
        except (FileNotFoundError, ConnectionRefusedError):
            raise BackendError("the service is stopped")
        except (OSError, ValueError) as e:
            raise BackendError(str(e))
        finally:
            s.close()

    @staticmethod
    def _systemctl(*args):
        import subprocess
        return subprocess.run(["systemctl", *args, SERVICE_NAME], capture_output=True, text=True)

    @property
    def running(self):
        return self._systemctl("is-active", "--quiet").returncode == 0

    @property
    def enabled(self):
        return self._systemctl("is-enabled", "--quiet").returncode == 0

    @staticmethod
    def control(action):
        """start / stop / restart / enable / disable — asks for the password (pkexec)."""
        import subprocess
        r = subprocess.run(["pkexec", "systemctl", action, SERVICE_NAME], capture_output=True, text=True)
        if r.returncode != 0:
            raise BackendError((r.stderr or r.stdout or f"systemctl {action} failed").strip())


def pick_backend(registry):
    if sys.platform.startswith("linux") and os.path.exists(SERVICE_UNIT):
        return ServiceBackend()
    return LocalBackend(registry)


class ServerWindow(QtWidgets.QMainWindow):
    status_sig = QtCore.pyqtSignal(str)        # result of a background service action

    def __init__(self):
        super().__init__()
        self.cfg = load_cfg()
        self.registry = Registry(match_by_name=self.cfg.get("match_by_name", True))
        self.backend = pick_backend(self.registry)
        self.service = self.backend.kind == "service"
        self.quitting = False
        self._log_seq = 0
        self._last_bytes = 0
        self._busy = False
        self.setWindowTitle("RemoteDesk Server")
        self.setWindowIcon(theme.app_icon("RemoteDeskServer"))
        self.resize(940, 680)
        self.status_sig.connect(self._action_done)
        self._build()
        self._tray()
        self.timer = QtCore.QTimer(self, timeout=self._refresh, interval=1000)
        self.timer.start()
        if not self.service and self.cfg.get("autostart", True):
            QtCore.QTimer.singleShot(0, self.start_server)
        QtCore.QTimer.singleShot(0, self._refresh)

    # ------------------------------------------------------------- layout
    def _build(self):
        root = QtWidgets.QWidget(objectName="Body")
        v = QtWidgets.QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QtWidgets.QFrame(objectName="Header")
        h = QtWidgets.QHBoxLayout(header)
        h.setContentsMargins(18, 12, 18, 12)
        h.addWidget(theme.label("RemoteDesk Server", "Logo"))
        h.addStretch()
        self.state = theme.label("")
        h.addWidget(self.state)
        h.addSpacing(12)
        self.toggle = QtWidgets.QPushButton("Start server", objectName="HeaderBtn")
        self.toggle.clicked.connect(self._toggle)
        h.addWidget(self.toggle)
        if self.service:
            restart = QtWidgets.QPushButton("Restart", objectName="HeaderBtn")
            restart.clicked.connect(lambda: self._service_action("restart"))
            h.addWidget(restart)
        v.addWidget(header)

        self.notice = theme.label("")
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet("background:#FFF4E5; color:#7A4B00; padding:8px 18px;")
        self.notice.hide()
        v.addWidget(self.notice)

        body = QtWidgets.QVBoxLayout()
        body.setContentsMargins(18, 18, 18, 18)
        body.setSpacing(14)
        v.addLayout(body, 1)

        top = QtWidgets.QHBoxLayout()
        top.setSpacing(14)
        c, l = theme.card("Settings")
        form = QtWidgets.QFormLayout()
        self.port = QtWidgets.QSpinBox(minimum=1, maximum=65535, value=self.cfg.get("port", 7070))
        self.port.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
        form.addRow("Port:", self.port)
        if self.service:
            self.port.setEnabled(False)
            self.port.setToolTip("Set by the service; change it with: sudo ./install_server.sh --port N")
            self.autostart = QtWidgets.QCheckBox("Start the server when this computer starts")
            self.autostart.setChecked(self.backend.enabled)
            self.autostart.toggled.connect(
                lambda on: self._service_action("enable" if on else "disable"))
            mode = theme.label("Runs as a system service — it keeps running when this window is "
                               "closed or nobody is logged in.", "Muted")
            mode.setWordWrap(True)
        else:
            self.autostart = QtWidgets.QCheckBox("Start server when the app opens")
            self.autostart.setChecked(self.cfg.get("autostart", True))
            self.autostart.toggled.connect(self._save)
            self.port.valueChanged.connect(self._save)
            mode = None
        self.tray_close = QtWidgets.QCheckBox("Keep this window in the tray when closed")
        self.tray_close.setChecked(self.cfg.get("tray", not self.service))
        self.tray_close.toggled.connect(self._save)
        self.name_match = QtWidgets.QCheckBox("Recognise desks by computer name (keeps the ID "
                                              "after a reinstall)")
        self.name_match.setChecked(self.cfg.get("match_by_name", True))
        self.name_match.toggled.connect(self._set_name_match)
        form.addRow(self.autostart)
        form.addRow(self.tray_close)
        form.addRow(self.name_match)
        l.addLayout(form)
        if mode:
            l.addWidget(mode)
        l.addStretch()
        top.addWidget(c, 1)
        c, l = theme.card("Clients connect to")
        self.addr_box = QtWidgets.QVBoxLayout()
        l.addLayout(self.addr_box)
        hint = theme.label("Enter one of these in the client under Settings → Server. "
                           "Over the internet use this computer's public IP or domain, "
                           "and open the port in the firewall / router. IPv6 addresses "
                           "are written in brackets, e.g. [2001:db8::10]:7070.", "Muted")
        hint.setWordWrap(True)
        l.addWidget(hint)
        l.addStretch()
        top.addWidget(c, 1)
        body.addLayout(top)

        stats = QtWidgets.QHBoxLayout()
        stats.setSpacing(14)
        self.stat = {}
        for key, title in (("desks", "Desks online"), ("sessions", "Active sessions"),
                           ("total", "Sessions since start"), ("traffic", "Live traffic")):
            c, l = theme.card()
            l.addWidget(theme.label(title, "Muted"))
            self.stat[key] = theme.label("0", "StatValue")
            l.addWidget(self.stat[key])
            stats.addWidget(c)
        body.addLayout(stats)

        self.tabs = QtWidgets.QTabWidget()
        desk_page = QtWidgets.QWidget()
        dl = QtWidgets.QVBoxLayout(desk_page)
        dl.setContentsMargins(8, 8, 8, 8)
        bar = QtWidgets.QHBoxLayout()
        self.desk_filter = QtWidgets.QLineEdit(placeholderText="Search ID or computer name")
        self.desk_filter.textChanged.connect(lambda _: self._refresh())
        bar.addWidget(self.desk_filter, 1)
        edit = QtWidgets.QPushButton("Edit ID…")
        edit.clicked.connect(self._edit_id)
        bar.addWidget(edit)
        delete = QtWidgets.QPushButton("Delete")
        delete.clicked.connect(self._delete_desk)
        bar.addWidget(delete)
        dl.addLayout(bar)
        self.desks = self._table(["Desk ID", "Computer name", "Status", "IP address", "Last seen"])
        self.desks.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.desks.doubleClicked.connect(self._edit_id)
        dl.addWidget(self.desks)
        self.sess = self._table(["Viewer IP", "Desk", "Duration", "Transferred", ""])
        self.log = QtWidgets.QPlainTextEdit(readOnly=True)
        self.log.setMaximumBlockCount(5000)
        self.tabs.addTab(desk_page, "Desks")
        self.tabs.addTab(self.sess, "Active sessions")
        self.tabs.addTab(self.log, "Log")
        body.addWidget(self.tabs, 1)
        self.setCentralWidget(root)
        self._names = {}
        self._fill_addresses()

    def _table(self, cols):
        t = QtWidgets.QTableWidget(0, len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.verticalHeader().hide()
        t.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        t.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        t.setShowGrid(False)
        t.setAlternatingRowColors(True)
        return t

    def _fill_addresses(self):
        while self.addr_box.count():
            self.addr_box.takeAt(0).widget().deleteLater()
        for ip in local_addresses() or ["(no network)"]:
            row = QtWidgets.QWidget()
            hl = QtWidgets.QHBoxLayout(row)
            hl.setContentsMargins(0, 0, 0, 0)
            text = format_hostport(ip, self.port.value())
            lab = theme.label(text)
            lab.setStyleSheet("font-size: 13pt; font-weight: 600;")
            lab.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            hl.addWidget(lab)
            hl.addStretch()
            b = QtWidgets.QPushButton("Copy", objectName="Icon")
            b.clicked.connect(lambda _, t=text: QtWidgets.QApplication.clipboard().setText(t))
            hl.addWidget(b)
            self.addr_box.addWidget(row)

    def _tray(self):
        self.tray = QtWidgets.QSystemTrayIcon(self.windowIcon(), self)
        m = QtWidgets.QMenu()
        m.addAction("Open", self._show)
        m.addAction("Quit", self._quit)
        self.tray.setContextMenu(m)
        self.tray.setToolTip("RemoteDesk Server")
        self.tray.activated.connect(lambda r: r == QtWidgets.QSystemTrayIcon.Trigger and self._show())
        self.tray.show()

    # ------------------------------------------------------------ actions
    def call(self, req):
        """Request to the server (in-process or service). Returns the reply or None on error."""
        try:
            r = self.backend.call(req)
        except BackendError as e:
            self._show_notice(str(e))
            return None
        self._show_notice("")
        return r

    def _show_notice(self, text):
        self.notice.setText(text)
        self.notice.setVisible(bool(text) and not (self.service and text == "the service is stopped"))

    def _toggle(self):
        if self.service:
            self._service_action("stop" if self.backend.running else "start")
        elif self.backend.running:
            self.stop_server()
        else:
            self.start_server()

    def _service_action(self, action):
        """Runs pkexec systemctl … in the background (it shows a password prompt)."""
        if self._busy:
            return
        self._busy = True
        self.toggle.setEnabled(False)
        self.state.setText(f"{theme.dot('#FFE08A')} {action.capitalize()}ing…")

        def work():
            try:
                self.backend.control(action)
                self.status_sig.emit("")
            except BackendError as e:
                self.status_sig.emit(f"Could not {action} the service: {e}")
        threading.Thread(target=work, daemon=True).start()

    def _action_done(self, err):
        self._busy = False
        self.toggle.setEnabled(True)
        if err:
            QtWidgets.QMessageBox.warning(self, "RemoteDesk Server", err)
        if self.service:
            self.autostart.blockSignals(True)
            self.autostart.setChecked(self.backend.enabled)
            self.autostart.blockSignals(False)
        self._refresh()

    def start_server(self):
        try:
            self.backend.start(self.port.value())
        except OSError as e:
            self._append_log(f"Cannot start on port {self.port.value()}: {e}")
            QtWidgets.QMessageBox.warning(self, "RemoteDesk Server",
                                          f"Cannot listen on port {self.port.value()}:\n{e}")
            return
        self._log_seq = 0
        self._fill_addresses()
        self._refresh()

    def stop_server(self):
        if not self.service:
            self.backend.stop()
            self._append_log("Server stopped")
        self._refresh()

    def _set_state(self, on, port=None):
        if not self.service:
            port = port or self.port.value()
        what = "Service" if self.service else "Server"
        running = f"{what} running" + (f" on port {port}" if port else "")   # port unknown without access
        self.state.setText(f"{theme.dot('#7CF29A' if on else '#FFD1CC')} {running if on else f'{what} stopped'}")
        self.toggle.setText(("Stop " if on else "Start ") + ("service" if self.service else "server"))
        if not self.service:
            self.port.setEnabled(not on)

    def _append_log(self, msg, t=None):
        self.log.appendPlainText(time.strftime("%Y-%m-%d %H:%M:%S  ", time.localtime(t)) + msg)

    def _refresh(self):
        running = self.backend.running
        lst = self.call({"cmd": "list"}) if (running or not self.service) else None
        ses = self.call({"cmd": "sessions"}) if running else None
        st = self.call({"cmd": "stats"}) if running else None
        if running:
            lg = self.call({"cmd": "log", "after": self._log_seq})
            for line in (lg or {}).get("lines", []):
                self._log_seq = line["seq"]
                self._append_log(line["text"], line["time"])
        port = (st or {}).get("port")
        if self.service and port and port != self.port.value():
            self.port.setValue(port)
            self._fill_addresses()
        if st and self.name_match.isChecked() != st.get("match_by_name", True):
            self.name_match.blockSignals(True)
            self.name_match.setChecked(st.get("match_by_name", True))
            self.name_match.blockSignals(False)
        if not self._busy:
            self._set_state(running, port)
        if self.service and not running:
            self._show_notice("")

        desks = (lst or {}).get("desks", [])
        sess = (ses or {}).get("sessions", [])
        self._names = {d["id"]: d.get("name") or "" for d in desks}
        online_n = sum(d["online"] for d in desks)
        self.stat["desks"].setText(str(online_n))
        self.stat["sessions"].setText(str(len(sess)))
        self.stat["total"].setText(str((st or {}).get("total_sessions", 0)))
        now_bytes = sum(x["bytes"] for x in sess)
        self.stat["traffic"].setText(fmt_bytes(max(0, now_bytes - self._last_bytes)) + "/s")
        self._last_bytes = now_bytes
        self.tabs.setTabText(0, f"Desks ({online_n} online / {len(desks)})")
        self.tabs.setTabText(1, f"Active sessions ({len(sess)})")

        q = self.desk_filter.text().strip().lower().replace(" ", "")
        rows = [d for d in desks if not q or q in d["id"] or q in (d.get("name") or "").lower().replace(" ", "")]
        rows.sort(key=lambda d: (not d["online"], (d.get("name") or "~").lower(), d["id"]))
        selected = self._selected_id()
        self.desks.setRowCount(len(rows))
        for r, d in enumerate(rows):
            i, on = d["id"], d["online"]
            last = "now" if on else (time.strftime("%Y-%m-%d %H:%M", time.localtime(d["last_seen"]))
                                     if d.get("last_seen") else "—")
            vals = (f"{i[:3]} {i[3:6]} {i[6:]}", d.get("name") or "(unnamed)",
                    f"● Online {fmt_age(d['since'])}" if on and d.get("since") else ("● Online" if on else "Offline"),
                    d.get("ip") or "", last)
            for c, val in enumerate(vals):
                item = QtWidgets.QTableWidgetItem(val)
                item.setData(QtCore.Qt.UserRole, i)
                if c == 2:
                    item.setForeground(QtGui.QColor(theme.OK if on else "#9AA0A6"))
                self.desks.setItem(r, c, item)
            if i == selected:
                self.desks.selectRow(r)

        self.sess.setRowCount(len(sess))
        for r, x in enumerate(sess):
            hid, sid = x["desk"], x["sid"]
            dname = self._names.get(hid, "")
            for c, val in enumerate((x["viewer"], f"{hid[:3]} {hid[3:6]} {hid[6:]}" + (f" — {dname}" if dname else ""),
                                     fmt_age(x["since"]), fmt_bytes(x["bytes"]))):
                self.sess.setItem(r, c, QtWidgets.QTableWidgetItem(val))
            old = self.sess.cellWidget(r, 4)
            if old is None or old.property("sid") != sid:
                b = QtWidgets.QPushButton("Disconnect")
                b.setProperty("sid", sid)
                b.clicked.connect(lambda _, s=sid: self.call({"cmd": "disconnect", "sid": s}))
                self.sess.setCellWidget(r, 4, b)
        self.tray.setToolTip(f"RemoteDesk Server - {online_n} desks, {len(sess)} sessions")

    def _selected_id(self):
        rows = self.desks.selectionModel().selectedRows() if self.desks.selectionModel() else []
        if not rows:
            return None
        item = self.desks.item(rows[0].row(), 0)
        return item.data(QtCore.Qt.UserRole) if item else None

    def _edit_id(self):
        old = self._selected_id()
        if not old:
            QtWidgets.QMessageBox.information(self, "Edit ID", "Select a desk first.")
            return
        name = self._names.get(old) or "this desk"
        new, ok = QtWidgets.QInputDialog.getText(self, "Edit ID", f"New 9-digit ID for {name}:", text=old)
        if not ok:
            return
        r = self.call({"cmd": "set-id", "old": old, "new": new.replace(" ", "")})
        if r and not r.get("ok"):
            QtWidgets.QMessageBox.warning(self, "Edit ID", r.get("error", "failed"))
        elif r and not self.backend.running:
            self._append_log(f"Desk ID {old} changed to {new}")
        self._refresh()

    def _delete_desk(self):
        i = self._selected_id()
        if not i:
            return
        name = self._names.get(i) or "(unnamed)"
        if QtWidgets.QMessageBox.question(
                self, "Delete desk", f"Remove {i} ({name}) from the list?\n\nIf that computer "
                "connects again it gets a new ID (or the same one, if it is recognised by name).") \
                != QtWidgets.QMessageBox.Yes:
            return
        r = self.call({"cmd": "delete", "id": i})
        if r and not r.get("ok"):
            QtWidgets.QMessageBox.warning(self, "Delete desk", r.get("error", "failed"))
        self._refresh()

    def _set_name_match(self, on):
        self.call({"cmd": "set-option", "match_by_name": bool(on)})
        self._save()

    def _save(self):
        self.cfg.update(tray=self.tray_close.isChecked(), match_by_name=self.name_match.isChecked())
        if not self.service:
            self.cfg.update(port=self.port.value(), autostart=self.autostart.isChecked())
        with open(CONFIG, "w") as f:
            json.dump(self.cfg, f, indent=2)

    def _show(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self):
        self.quitting = True
        if not self.service:
            self.backend.stop()                 # the service keeps running on its own
        QtWidgets.QApplication.quit()

    def closeEvent(self, e):
        if not self.quitting and self.tray_close.isChecked() and self.tray.isVisible():
            e.ignore()
            self.hide()
            self.tray.showMessage("RemoteDesk Server", "Still running in the tray.",
                                  QtWidgets.QSystemTrayIcon.Information, 2500)
        else:
            e.accept()
            self._quit()


def main():
    theme.set_windows_app_id("RemoteDesk.Server")
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("RemoteDesk Server")
    app.setWindowIcon(theme.app_icon("RemoteDeskServer"))
    app.setQuitOnLastWindowClosed(False)
    theme.apply(app, theme.SERVER_ACCENT, "#2459B5")
    w = ServerWindow()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
