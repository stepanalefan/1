"""RemoteDesk Server - GUI for the relay. Run it on the computer that both
sides can reach (VPS, office server, home PC with port forwarding)."""
import json, os, socket, sys, time

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


class ServerWindow(QtWidgets.QMainWindow):
    log_sig = QtCore.pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.cfg = load_cfg()
        self.relay = None
        self.registry = Registry(match_by_name=self.cfg.get("match_by_name", True))
        self.quitting = False
        self.setWindowTitle("RemoteDesk Server")
        self.setWindowIcon(theme.app_icon("RemoteDeskServer"))
        self.resize(900, 640)
        self.log_sig.connect(self._append_log)
        self._build()
        self._tray()
        self.timer = QtCore.QTimer(self, timeout=self._refresh, interval=1000)
        self.timer.start()
        if self.cfg.get("autostart", True):
            QtCore.QTimer.singleShot(0, self.start_server)

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
        v.addWidget(header)

        body = QtWidgets.QVBoxLayout()
        body.setContentsMargins(18, 18, 18, 18)
        body.setSpacing(14)
        v.addLayout(body, 1)

        top = QtWidgets.QHBoxLayout()
        top.setSpacing(14)
        # settings card
        c, l = theme.card("Settings")
        form = QtWidgets.QFormLayout()
        self.port = QtWidgets.QSpinBox(minimum=1, maximum=65535, value=self.cfg.get("port", 7070))
        self.port.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
        form.addRow("Port:", self.port)
        self.autostart = QtWidgets.QCheckBox("Start server when the app opens")
        self.autostart.setChecked(self.cfg.get("autostart", True))
        self.tray_close = QtWidgets.QCheckBox("Keep running in the tray when closed")
        self.tray_close.setChecked(self.cfg.get("tray", True))
        for w in (self.autostart, self.tray_close):
            w.toggled.connect(self._save)
        self.port.valueChanged.connect(self._save)
        self.name_match = QtWidgets.QCheckBox("Recognise desks by computer name (keeps the ID "
                                              "after a reinstall)")
        self.name_match.setChecked(self.cfg.get("match_by_name", True))
        self.name_match.toggled.connect(self._save)
        form.addRow(self.autostart)
        form.addRow(self.tray_close)
        form.addRow(self.name_match)
        l.addLayout(form)
        l.addStretch()
        top.addWidget(c, 1)
        # address card
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

        # stats
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

        # tables / log
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
        self._fill_addresses()
        self._set_state(False)

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
    def _toggle(self):
        self.stop_server() if self.relay else self.start_server()

    def start_server(self):
        relay = Relay("", self.port.value(), on_log=self.log_sig.emit, registry=self.registry)
        try:
            relay.start()
        except OSError as e:
            self._append_log(f"Cannot start on port {self.port.value()}: {e}")
            QtWidgets.QMessageBox.warning(self, "RemoteDesk Server",
                                          f"Cannot listen on port {self.port.value()}:\n{e}")
            return
        self.relay = relay
        self._set_state(True)
        self._fill_addresses()

    def stop_server(self):
        if self.relay:
            self.relay.stop()
            self.relay = None
        self._set_state(False)
        self._refresh()

    def _set_state(self, on):
        self.state.setText(f"{theme.dot('#7CF29A' if on else '#FFD1CC')} "
                           f"{'Running on port %d' % self.port.value() if on else 'Stopped'}")
        self.toggle.setText("Stop server" if on else "Start server")
        self.port.setEnabled(not on)

    def _append_log(self, msg):
        self.log.appendPlainText(time.strftime("%Y-%m-%d %H:%M:%S  ") + msg)

    def _refresh(self):
        hosts, sess = self.relay.snapshot() if self.relay else ([], [])
        self.stat["desks"].setText(str(len(hosts)))
        self.stat["sessions"].setText(str(len(sess)))
        self.stat["total"].setText(str(self.relay.total_sessions if self.relay else 0))
        now_bytes = sum(s[4] for s in sess)
        rate = max(0, now_bytes - getattr(self, "_last_bytes", 0))
        self._last_bytes = now_bytes
        self.stat["traffic"].setText(fmt_bytes(rate) + "/s")
        self.tabs.setTabText(0, f"Desks ({len(hosts)} online / {len(self.registry.desks)})")
        self.tabs.setTabText(1, f"Active sessions ({len(sess)})")

        online = {i: (ip, since) for i, ip, since in hosts}
        q = self.desk_filter.text().strip().lower().replace(" ", "")
        rows = [d for d in self.registry.list()
                if not q or q in d["id"] or q in (d.get("name") or "").lower().replace(" ", "")]
        rows.sort(key=lambda d: (d["id"] not in online, (d.get("name") or "~").lower(), d["id"]))
        selected = self._selected_id()
        self.desks.setRowCount(len(rows))
        for r, d in enumerate(rows):
            i = d["id"]
            on = online.get(i)
            last = "now" if on else (time.strftime("%Y-%m-%d %H:%M", time.localtime(d["last_seen"]))
                                     if d.get("last_seen") else "—")
            vals = (f"{i[:3]} {i[3:6]} {i[6:]}", d.get("name") or "(unnamed)",
                    f"● Online {fmt_age(on[1])}" if on else "Offline",
                    on[0] if on else d.get("ip", ""), last)
            for c, val in enumerate(vals):
                item = QtWidgets.QTableWidgetItem(val)
                item.setData(QtCore.Qt.UserRole, i)
                if c == 2:
                    item.setForeground(QtGui.QColor(theme.OK if on else "#9AA0A6"))
                self.desks.setItem(r, c, item)
            if i == selected:
                self.desks.selectRow(r)

        self.sess.setRowCount(len(sess))
        for r, (sid, hid, vip, since, nbytes) in enumerate(sess):
            dname = self.registry.desks.get(hid, {}).get("name") or ""
            for c, val in enumerate((vip, f"{hid[:3]} {hid[3:6]} {hid[6:]}" + (f" — {dname}" if dname else ""),
                                     fmt_age(since), fmt_bytes(nbytes))):
                self.sess.setItem(r, c, QtWidgets.QTableWidgetItem(val))
            old = self.sess.cellWidget(r, 4)
            if old is None or old.property("sid") != sid:
                b = QtWidgets.QPushButton("Disconnect")
                b.setProperty("sid", sid)
                b.clicked.connect(lambda _, s=sid: self.relay and self.relay.disconnect_session(s))
                self.sess.setCellWidget(r, 4, b)
        self.tray.setToolTip(f"RemoteDesk Server - {len(hosts)} desks, {len(sess)} sessions")

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
        name = self.registry.desks.get(old, {}).get("name") or "this desk"
        new, ok = QtWidgets.QInputDialog.getText(
            self, "Edit ID", f"New 9-digit ID for {name}:", text=old)
        if not ok:
            return
        new = new.replace(" ", "")
        try:
            if self.relay:
                self.relay.change_id(old, new)
            else:
                self.registry.change_id(old, new)
                self._append_log(f"Desk ID {old} changed to {new}")
        except (ValueError, KeyError) as e:
            QtWidgets.QMessageBox.warning(self, "Edit ID", str(e))
        self._refresh()

    def _delete_desk(self):
        i = self._selected_id()
        if not i:
            return
        name = self.registry.desks.get(i, {}).get("name") or "(unnamed)"
        if QtWidgets.QMessageBox.question(
                self, "Delete desk", f"Remove {i} ({name}) from the list?\n\nIf that computer "
                "connects again it gets a new ID (or the same one, if it is recognised by name).") \
                != QtWidgets.QMessageBox.Yes:
            return
        if self.relay:
            self.relay.delete_desk(i)
        else:
            self.registry.delete(i)
        self._refresh()

    def _save(self):
        self.registry.match_by_name = self.name_match.isChecked()
        self.cfg.update(port=self.port.value(), autostart=self.autostart.isChecked(),
                        tray=self.tray_close.isChecked(), match_by_name=self.name_match.isChecked())
        with open(CONFIG, "w") as f:
            json.dump(self.cfg, f, indent=2)

    def _show(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self):
        self.quitting = True
        self.stop_server()
        QtWidgets.QApplication.quit()

    def closeEvent(self, e):
        if not self.quitting and self.tray_close.isChecked() and self.tray.isVisible():
            e.ignore()
            self.hide()
            self.tray.showMessage("RemoteDesk Server", "Still running in the tray.",
                                  QtWidgets.QSystemTrayIcon.Information, 2500)
        else:
            self.stop_server()
            e.accept()
            QtWidgets.QApplication.quit()


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
