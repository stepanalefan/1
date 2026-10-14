# -*- coding: utf-8 -*-
"""
can_commands.py
===============
Command library + block script editor for can_sniffer.py (keep both files in
the same folder).

* Command library - frames captured from the bus (or typed in), each with a
  short key (letter) and a name. Double-click / Send / press the letter key
  (with "Letter hotkeys" on) to transmit it.
* Scripts - a single column of blocks:
      Send <command>        transmit a command from the library
      Pause <ms>            wait
      Repeat <N> ... End    repeat the enclosed blocks N times (0 = forever)
  Repeats can be nested. Several named scripts can be kept.

Everything is stored in can_commands.json next to the program and saved on
every change.
"""

from __future__ import annotations

import json
import os
import string
import uuid

from PyQt5 import QtCore, QtGui, QtWidgets


# --------------------------------------------------------------------------- #
#  Data                                                                        #
# --------------------------------------------------------------------------- #
class Command:
    def __init__(self, key="", name="", can_id=0, ext=False, rtr=False, dlc=0, data=b"", uid=None):
        self.uid = uid or uuid.uuid4().hex[:12]
        self.key = key
        self.name = name
        self.can_id = can_id
        self.ext = ext
        self.rtr = rtr
        self.data = bytes(data)
        self.dlc = dlc if rtr else len(self.data)

    def to_frame(self, frame_cls):
        if self.rtr:
            return frame_cls(0, self.can_id, self.ext, True, self.dlc)
        return frame_cls(0, self.can_id, self.ext, False, None, self.data)

    def id_str(self) -> str:
        return f"{self.can_id:08X}" if self.ext else f"{self.can_id:03X}"

    def data_str(self) -> str:
        if self.rtr:
            return f"RTR dlc={self.dlc}"
        return " ".join(f"{b:02X}" for b in self.data)

    def label(self) -> str:
        return f"[{self.key}] {self.name}" if self.name else f"[{self.key}]"

    def to_dict(self):
        return {"uid": self.uid, "key": self.key, "name": self.name, "id": self.id_str(),
                "ext": self.ext, "rtr": self.rtr, "dlc": self.dlc, "data": self.data.hex().upper()}

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("key", ""), d.get("name", ""), int(d["id"], 16), bool(d.get("ext")),
                   bool(d.get("rtr")), int(d.get("dlc", 0)), bytes.fromhex(d.get("data", "")), d.get("uid"))


class Store:
    """can_commands.json: {"commands": [...], "scripts": {name: [step, ...]}}"""

    def __init__(self, path):
        self.path = path
        self.commands: list[Command] = []
        self.scripts: dict[str, list[dict]] = {}
        self.error = ""
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            self.commands = [Command.from_dict(c) for c in d.get("commands", [])]
            self.scripts = {k: list(v) for k, v in d.get("scripts", {}).items()}
        except FileNotFoundError:
            pass
        except Exception as e:
            self.error = f"cannot read {path}: {e}"
        if not self.scripts:
            self.scripts["Script 1"] = []

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"commands": [c.to_dict() for c in self.commands], "scripts": self.scripts},
                      f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    def by_uid(self, uid) -> Command | None:
        return next((c for c in self.commands if c.uid == uid), None)

    def by_key(self, key) -> Command | None:
        return next((c for c in self.commands if c.key == key), None)

    def next_key(self) -> str:
        used = {c.key for c in self.commands}
        for k in string.ascii_uppercase + string.digits + string.ascii_lowercase:
            if k not in used:
                return k
        n = 1
        while f"C{n}" in used:
            n += 1
        return f"C{n}"


def _hex_bytes(text: str) -> bytes:
    s = "".join(text.replace("0x", "").replace("0X", "").replace(",", " ").split())
    if len(s) % 2:
        raise ValueError("odd number of hex digits")
    b = bytes.fromhex(s)
    if len(b) > 8:
        raise ValueError("at most 8 data bytes")
    return b


# --------------------------------------------------------------------------- #
#  Command edit dialog                                                         #
# --------------------------------------------------------------------------- #
class CommandDialog(QtWidgets.QDialog):
    def __init__(self, parent, cmd: Command, taken_keys: set[str]):
        super().__init__(parent)
        self.setWindowTitle("Command")
        self.cmd = cmd
        self.taken = taken_keys
        f = QtWidgets.QFormLayout(self)
        self.key = QtWidgets.QLineEdit(cmd.key)
        self.key.setMaxLength(4)
        self.key.setToolTip("Short designation. A single letter/digit can also be used as a hotkey.")
        self.name = QtWidgets.QLineEdit(cmd.name)
        self.cid = QtWidgets.QLineEdit(cmd.id_str())
        self.ext = QtWidgets.QCheckBox("Extended (29-bit)")
        self.ext.setChecked(cmd.ext)
        self.rtr = QtWidgets.QCheckBox("RTR")
        self.rtr.setChecked(cmd.rtr)
        self.dlc = QtWidgets.QSpinBox()
        self.dlc.setRange(0, 8)
        self.dlc.setValue(cmd.dlc)
        self.data = QtWidgets.QLineEdit("" if cmd.rtr else cmd.data_str())
        flags = QtWidgets.QHBoxLayout()
        flags.addWidget(self.ext)
        flags.addWidget(self.rtr)
        flags.addWidget(QtWidgets.QLabel("RTR DLC"))
        flags.addWidget(self.dlc)
        f.addRow("Key", self.key)
        f.addRow("Name", self.name)
        f.addRow("ID (hex)", self.cid)
        f.addRow("", flags)
        f.addRow("Data (hex)", self.data)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.name.setFocus()

    def accept(self):
        try:
            key = self.key.text().strip()
            if not key:
                raise ValueError("key is empty")
            if key in self.taken:
                raise ValueError(f"key '{key}' is already used")
            can_id = int(self.cid.text().strip().upper().removeprefix("0X"), 16)
            ext = self.ext.isChecked() or can_id > 0x7FF
            if can_id > 0x1FFFFFFF:
                raise ValueError("ID exceeds 29 bits")
            data = b"" if self.rtr.isChecked() else _hex_bytes(self.data.text())
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "Command", str(e))
            return
        c = self.cmd
        c.key, c.name, c.can_id, c.ext, c.rtr = key, self.name.text().strip(), can_id, ext, self.rtr.isChecked()
        c.data = data
        c.dlc = self.dlc.value() if c.rtr else len(data)
        super().accept()


# --------------------------------------------------------------------------- #
#  Command library panel                                                       #
# --------------------------------------------------------------------------- #
class CommandPanel(QtWidgets.QGroupBox):
    changed = QtCore.pyqtSignal()          # library edited -> script view relabels
    to_script = QtCore.pyqtSignal(list)    # [uid, ...] to append as Send blocks

    def __init__(self, store: Store, host):
        """host provides: send_frame(Frame)->bool, selected_frames()->list[Frame],
        tx_panel_frame()->Frame, logmsg(text, level), FRAME (Frame class)."""
        super().__init__("Commands")
        self.store = store
        self.host = host
        v = QtWidgets.QVBoxLayout(self)

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Key", "Name", "ID", "Data"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.table.horizontalHeader().setStretchLastSection(True)
        for i, w in enumerate((44, 130, 80)):
            self.table.setColumnWidth(i, w)
        self.table.doubleClicked.connect(lambda *_: self.send_selected())
        self.table.setToolTip("Double-click = send.  With 'Letter hotkeys' on, press the key to send.")
        v.addWidget(self.table, 1)

        add = QtWidgets.QHBoxLayout()
        b_sel = QtWidgets.QPushButton("+ From capture")
        b_sel.setToolTip("Add the rows selected in Trace / Monitor as commands")
        b_sel.clicked.connect(self.add_from_selection)
        b_tx = QtWidgets.QPushButton("+ From Transmit")
        b_tx.setToolTip("Add the frame currently in the Transmit panel")
        b_tx.clicked.connect(self.add_from_tx)
        b_new = QtWidgets.QPushButton("+ New…")
        b_new.clicked.connect(self.add_new)
        for b in (b_sel, b_tx, b_new):
            add.addWidget(b)
        v.addLayout(add)

        ed = QtWidgets.QHBoxLayout()
        self.b_send = QtWidgets.QPushButton("Send")
        self.b_send.clicked.connect(self.send_selected)
        b_edit = QtWidgets.QPushButton("Edit…")
        b_edit.clicked.connect(self.edit_selected)
        b_del = QtWidgets.QPushButton("Delete")
        b_del.clicked.connect(self.delete_selected)
        b_up = QtWidgets.QPushButton("▲")
        b_up.clicked.connect(lambda: self.move(-1))
        b_dn = QtWidgets.QPushButton("▼")
        b_dn.clicked.connect(lambda: self.move(1))
        b_scr = QtWidgets.QPushButton("→ Script")
        b_scr.setToolTip("Append the selected commands to the current script as Send blocks")
        b_scr.clicked.connect(lambda: self.to_script.emit([c.uid for c in self.selected()]))
        for b in (self.b_send, b_edit, b_del, b_up, b_dn, b_scr):
            ed.addWidget(b)
        for b in (b_up, b_dn):
            b.setFixedWidth(32)
        v.addLayout(ed)

        self.hotkeys = QtWidgets.QCheckBox("Letter hotkeys (press the key to send)")
        self.hotkeys.setToolTip("Works while no text field has the keyboard focus")
        v.addWidget(self.hotkeys)
        QtWidgets.QApplication.instance().installEventFilter(self)
        self.reload()

    # ---------------------------------------------------------- helpers ---
    def reload(self, select_uids=()):
        self.table.setRowCount(len(self.store.commands))
        rows = []
        for r, c in enumerate(self.store.commands):
            items = [c.key, c.name, c.id_str(), c.data_str()]
            for col, text in enumerate(items):
                it = QtWidgets.QTableWidgetItem(text)
                if col == 0:
                    it.setTextAlignment(QtCore.Qt.AlignCenter)
                    it.setForeground(QtGui.QColor("#f0b03e"))
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                it.setData(QtCore.Qt.UserRole, c.uid)
                self.table.setItem(r, col, it)
            if c.uid in select_uids:
                rows.append(r)
        if rows:
            self.table.clearSelection()
            for r in rows:
                self.table.selectRow(r) if len(rows) == 1 else self.table.selectionModel().select(
                    self.table.model().index(r, 0),
                    QtCore.QItemSelectionModel.Select | QtCore.QItemSelectionModel.Rows)

    def _commit(self, select_uids=()):
        try:
            self.store.save()
        except OSError as e:
            self.host.logmsg(f"cannot save commands: {e}", "err")
        self.reload(select_uids)
        self.changed.emit()

    def selected(self) -> list[Command]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.store.commands[r] for r in rows if r < len(self.store.commands)]

    def _add(self, cmds: list[Command]):
        if not cmds:
            return
        self.store.commands.extend(cmds)
        self._commit({c.uid for c in cmds})
        self.host.logmsg(f"added command(s): {', '.join(c.label() for c in cmds)}", "ok")

    def _from_frame(self, f) -> Command:
        return Command("", "", f.can_id, f.ext, f.rtr, f.dlc, f.data)

    # ---------------------------------------------------------- actions ---
    def add_from_selection(self):
        frames = self.host.selected_frames()
        if not frames:
            self.host.logmsg("select frames in the Trace or Monitor view first", "warn")
            return
        seen, cmds = set(), []
        for f in frames:
            sig = (f.ext, f.can_id, f.rtr, f.dlc, f.data)
            if sig in seen:
                continue
            seen.add(sig)
            c = self._from_frame(f)
            c.key = self.store.next_key()
            c.name = f"{c.id_str()}"
            self.store.commands.append(c)      # reserve the key for next_key()
            cmds.append(c)
        del self.store.commands[-len(cmds):]
        if len(cmds) == 1:
            dlg = CommandDialog(self, cmds[0], {x.key for x in self.store.commands})
            if dlg.exec_() != QtWidgets.QDialog.Accepted:
                return
        self._add(cmds)

    def add_from_tx(self):
        try:
            f = self.host.tx_panel_frame()
        except Exception as e:
            self.host.logmsg(f"bad frame in Transmit panel: {e}", "err")
            return
        c = self._from_frame(f)
        c.key = self.store.next_key()
        if CommandDialog(self, c, {x.key for x in self.store.commands}).exec_() == QtWidgets.QDialog.Accepted:
            self._add([c])

    def add_new(self):
        c = Command(self.store.next_key())
        if CommandDialog(self, c, {x.key for x in self.store.commands}).exec_() == QtWidgets.QDialog.Accepted:
            self._add([c])

    def edit_selected(self):
        sel = self.selected()
        if not sel:
            return
        c = sel[0]
        taken = {x.key for x in self.store.commands if x is not c}
        if CommandDialog(self, c, taken).exec_() == QtWidgets.QDialog.Accepted:
            self._commit({c.uid})

    def delete_selected(self):
        sel = self.selected()
        if not sel:
            return
        names = ", ".join(c.label() for c in sel)
        if QtWidgets.QMessageBox.question(self, "Delete", f"Delete {names}?\n"
                                          "Send blocks using them will be marked as missing.") \
                != QtWidgets.QMessageBox.Yes:
            return
        uids = {c.uid for c in sel}
        self.store.commands = [c for c in self.store.commands if c.uid not in uids]
        self._commit()

    def move(self, d):
        sel = self.selected()
        if len(sel) != 1:
            return
        cmds = self.store.commands
        i = cmds.index(sel[0])
        j = i + d
        if 0 <= j < len(cmds):
            cmds[i], cmds[j] = cmds[j], cmds[i]
            self._commit({sel[0].uid})

    def send_cmd(self, c: Command) -> bool:
        ok = self.host.send_frame(c.to_frame(self.host.FRAME))
        if ok:
            self.host.logmsg(f"sent {c.label()}  {c.id_str()}  {c.data_str()}")
        return ok

    def send_selected(self):
        for c in self.selected():
            if not self.send_cmd(c):
                break

    def eventFilter(self, obj, ev):
        if (ev.type() == QtCore.QEvent.KeyPress and self.hotkeys.isChecked()
                and not ev.isAutoRepeat()
                and not (ev.modifiers() & (QtCore.Qt.ControlModifier | QtCore.Qt.AltModifier))
                and self.window().isActiveWindow()):
            fw = QtWidgets.QApplication.focusWidget()
            if isinstance(fw, (QtWidgets.QLineEdit, QtWidgets.QAbstractSpinBox, QtWidgets.QTextEdit,
                               QtWidgets.QPlainTextEdit)) or (isinstance(fw, QtWidgets.QComboBox) and fw.isEditable()):
                return False
            if isinstance(QtWidgets.QApplication.activeModalWidget(), QtWidgets.QDialog):
                return False
            t = ev.text()
            if t and t.strip():
                c = self.store.by_key(t) or self.store.by_key(t.upper())
                if c is not None:
                    self.send_cmd(c)
                    return True
        return False


# --------------------------------------------------------------------------- #
#  Script editor + runner                                                      #
# --------------------------------------------------------------------------- #
C_SEND = QtGui.QColor("#79c0ff")
C_WAIT = QtGui.QColor("#8a96a3")
C_LOOP = QtGui.QColor("#d2a8ff")
C_BAD = QtGui.QColor("#ff6b6b")
C_RUN = QtGui.QColor("#1f6f43")


def check_script(steps: list[dict]) -> str:
    depth = 0
    for i, s in enumerate(steps):
        if s["op"] == "repeat":
            depth += 1
        elif s["op"] == "end":
            depth -= 1
            if depth < 0:
                return f"block {i + 1}: 'End repeat' without 'Repeat'"
    return f"{depth} 'Repeat' block(s) without 'End repeat'" if depth else ""


class ScriptPanel(QtWidgets.QGroupBox):
    def __init__(self, store: Store, cmd_panel: CommandPanel, host):
        super().__init__("Script")
        self.store = store
        self.cmds = cmd_panel
        self.host = host
        self.running = False
        self.pc = 0
        self.stack: list[list[int]] = []    # [start_pc, remaining] (remaining 0 = forever)
        self.n_sent = 0
        self.timer = QtCore.QTimer(self, singleShot=True, timeout=self._step)
        v = QtWidgets.QVBoxLayout(self)

        top = QtWidgets.QHBoxLayout()
        self.script_cb = QtWidgets.QComboBox()
        self.script_cb.currentIndexChanged.connect(lambda *_: self.reload())
        b_new = QtWidgets.QPushButton("New")
        b_new.clicked.connect(self.new_script)
        b_ren = QtWidgets.QPushButton("Rename")
        b_ren.clicked.connect(self.rename_script)
        b_del = QtWidgets.QPushButton("Delete")
        b_del.clicked.connect(self.delete_script)
        top.addWidget(self.script_cb, 1)
        for b in (b_new, b_ren, b_del):
            top.addWidget(b)
        v.addLayout(top)

        self.list = QtWidgets.QListWidget()
        self.list.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.list.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.list.model().rowsMoved.connect(self._rows_moved)
        self.list.itemDoubleClicked.connect(lambda *_: self.edit_step())
        self.list.setToolTip("Drag to reorder, double-click to edit a block")
        v.addWidget(self.list, 1)

        g = QtWidgets.QGridLayout()
        self.cmd_cb = QtWidgets.QComboBox()
        b_send = QtWidgets.QPushButton("+ Send")
        b_send.clicked.connect(lambda: self._insert({"op": "send", "cmd": self.cmd_cb.currentData()})
                               if self.cmd_cb.currentData() else None)
        self.wait_sp = QtWidgets.QSpinBox()
        self.wait_sp.setRange(1, 3_600_000)
        self.wait_sp.setValue(500)
        self.wait_sp.setSuffix(" ms")
        b_wait = QtWidgets.QPushButton("+ Pause")
        b_wait.clicked.connect(lambda: self._insert({"op": "wait", "ms": self.wait_sp.value()}))
        self.rep_sp = QtWidgets.QSpinBox()
        self.rep_sp.setRange(0, 1_000_000)
        self.rep_sp.setValue(3)
        self.rep_sp.setSuffix(" ×")
        self.rep_sp.setSpecialValueText("forever")
        b_rep = QtWidgets.QPushButton("+ Repeat")
        b_rep.setToolTip("Wraps the selected blocks (or inserts an empty Repeat … End pair)")
        b_rep.clicked.connect(self.add_repeat)
        g.addWidget(self.cmd_cb, 0, 0)
        g.addWidget(b_send, 0, 1)
        g.addWidget(self.wait_sp, 1, 0)
        g.addWidget(b_wait, 1, 1)
        g.addWidget(self.rep_sp, 2, 0)
        g.addWidget(b_rep, 2, 1)
        g.setColumnStretch(0, 1)
        v.addLayout(g)

        ed = QtWidgets.QHBoxLayout()
        for text, fn in (("▲", lambda: self.move(-1)), ("▼", lambda: self.move(1)),
                         ("Copy", self.duplicate), ("Edit", self.edit_step), ("Delete", self.delete_steps)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            ed.addWidget(b)
        v.addLayout(ed)

        run = QtWidgets.QHBoxLayout()
        self.b_run = QtWidgets.QPushButton("▶ Run")
        self.b_run.setCheckable(True)
        self.b_run.clicked.connect(self.toggle_run)
        self.loop_chk = QtWidgets.QCheckBox("Loop script")
        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color:#8a96a3")
        run.addWidget(self.b_run)
        run.addWidget(self.loop_chk)
        run.addWidget(self.status, 1)
        v.addLayout(run)

        QtWidgets.QShortcut(QtGui.QKeySequence.Delete, self.list, activated=self.delete_steps)
        cmd_panel.changed.connect(self.refresh_commands)
        cmd_panel.to_script.connect(lambda uids: [self._insert({"op": "send", "cmd": u}) for u in uids])
        self.refresh_commands()
        self.reload_scripts()

    # -------------------------------------------------------- scripts ---
    @property
    def name(self) -> str:
        return self.script_cb.currentText()

    @property
    def steps(self) -> list[dict]:
        return self.store.scripts.setdefault(self.name, [])

    def reload_scripts(self, select=None):
        self.script_cb.blockSignals(True)
        self.script_cb.clear()
        self.script_cb.addItems(list(self.store.scripts))
        if select:
            self.script_cb.setCurrentText(select)
        self.script_cb.blockSignals(False)
        self.reload()

    def _save(self):
        try:
            self.store.save()
        except OSError as e:
            self.host.logmsg(f"cannot save scripts: {e}", "err")

    def new_script(self):
        n = 1
        while f"Script {n}" in self.store.scripts:
            n += 1
        name, ok = QtWidgets.QInputDialog.getText(self, "New script", "Name:", text=f"Script {n}")
        name = name.strip()
        if not ok or not name:
            return
        if name in self.store.scripts:
            self.host.logmsg(f"script '{name}' already exists", "warn")
            return
        self.store.scripts[name] = []
        self._save()
        self.reload_scripts(name)

    def rename_script(self):
        old = self.name
        name, ok = QtWidgets.QInputDialog.getText(self, "Rename script", "Name:", text=old)
        name = name.strip()
        if not ok or not name or name == old or name in self.store.scripts:
            return
        self.store.scripts = {(name if k == old else k): v for k, v in self.store.scripts.items()}
        self._save()
        self.reload_scripts(name)

    def delete_script(self):
        if self.running:
            return
        if QtWidgets.QMessageBox.question(self, "Delete", f"Delete script '{self.name}'?") \
                != QtWidgets.QMessageBox.Yes:
            return
        del self.store.scripts[self.name]
        if not self.store.scripts:
            self.store.scripts["Script 1"] = []
        self._save()
        self.reload_scripts()

    # ---------------------------------------------------------- blocks ---
    def refresh_commands(self):
        cur = self.cmd_cb.currentData()
        self.cmd_cb.clear()
        for c in self.store.commands:
            self.cmd_cb.addItem(f"{c.label()}  {c.id_str()}", c.uid)
        i = self.cmd_cb.findData(cur)
        if i >= 0:
            self.cmd_cb.setCurrentIndex(i)
        self.reload()

    def _text(self, s: dict) -> tuple[str, QtGui.QColor]:
        op = s["op"]
        if op == "send":
            c = self.store.by_uid(s["cmd"])
            if c is None:
                return "▶ Send  <missing command>", C_BAD
            return f"▶ Send  {c.label()}   {c.id_str()}: {c.data_str()}", C_SEND
        if op == "wait":
            return f"⏱ Pause {s['ms']} ms", C_WAIT
        if op == "repeat":
            return f"⟳ Repeat {'forever' if s['count'] == 0 else str(s['count']) + ' ×'}", C_LOOP
        return "⟲ End repeat", C_LOOP

    def reload(self, select=()):
        self.list.clear()
        depth = 0
        for i, s in enumerate(self.steps):
            if s["op"] == "end":
                depth = max(0, depth - 1)
            text, color = self._text(s)
            it = QtWidgets.QListWidgetItem("    " * depth + text)
            it.setForeground(color)
            it.setData(QtCore.Qt.UserRole, i)
            self.list.addItem(it)
            if i in select:
                it.setSelected(True)
            if s["op"] == "repeat":
                depth += 1
        err = check_script(self.steps)
        if not self.running:
            self.status.setText(err or f"{len(self.steps)} blocks")
            self.status.setStyleSheet("color:#ff6b6b" if err else "color:#8a96a3")

    def _sel_rows(self) -> list[int]:
        return sorted(self.list.row(it) for it in self.list.selectedItems())

    def _insert(self, step: dict):
        if self.running:
            return
        rows = self._sel_rows()
        pos = rows[-1] + 1 if rows else len(self.steps)
        self.steps.insert(pos, step)
        self._save()
        self.reload({pos})
        self.list.scrollToItem(self.list.item(pos))

    def add_repeat(self):
        if self.running:
            return
        rows = self._sel_rows()
        st = self.steps
        if rows:
            a, b = rows[0], rows[-1]
            st.insert(b + 1, {"op": "end"})
            st.insert(a, {"op": "repeat", "count": self.rep_sp.value()})
            sel = {a, b + 2}
        else:
            st += [{"op": "repeat", "count": self.rep_sp.value()}, {"op": "end"}]
            sel = {len(st) - 2}
        self._save()
        self.reload(sel)

    def delete_steps(self):
        rows = self._sel_rows()
        if self.running or not rows:
            return
        for r in reversed(rows):
            del self.steps[r]
        self._save()
        self.reload()

    def duplicate(self):
        rows = self._sel_rows()
        if self.running or not rows:
            return
        copy = [dict(self.steps[r]) for r in rows]
        pos = rows[-1] + 1
        self.steps[pos:pos] = copy
        self._save()
        self.reload(set(range(pos, pos + len(copy))))

    def move(self, d):
        rows = self._sel_rows()
        if self.running or not rows:
            return
        st = self.steps
        a, b = rows[0], rows[-1]
        if d < 0 and a > 0:
            st.insert(b, st.pop(a - 1))
            sel = {r - 1 for r in rows}
        elif d > 0 and b < len(st) - 1:
            st.insert(a, st.pop(b + 1))
            sel = {r + 1 for r in rows}
        else:
            return
        self._save()
        self.reload(sel)

    def _rows_moved(self, *_):
        if self.running:
            self.reload()
            return
        order = [self.list.item(i).data(QtCore.Qt.UserRole) for i in range(self.list.count())]
        old = self.steps
        self.store.scripts[self.name] = [old[i] for i in order]
        self._save()
        QtCore.QTimer.singleShot(0, self.reload)

    def edit_step(self):
        rows = self._sel_rows()
        if self.running or len(rows) != 1:
            return
        s = self.steps[rows[0]]
        if s["op"] == "wait":
            v, ok = QtWidgets.QInputDialog.getInt(self, "Pause", "Pause, ms:", s["ms"], 1, 3_600_000)
            if ok:
                s["ms"] = v
        elif s["op"] == "repeat":
            v, ok = QtWidgets.QInputDialog.getInt(self, "Repeat", "Repeat count (0 = forever):",
                                                  s["count"], 0, 1_000_000)
            if ok:
                s["count"] = v
        elif s["op"] == "send":
            items = [f"{c.label()}  {c.id_str()}" for c in self.store.commands]
            if not items:
                return
            cur = next((i for i, c in enumerate(self.store.commands) if c.uid == s["cmd"]), 0)
            v, ok = QtWidgets.QInputDialog.getItem(self, "Send", "Command:", items, cur, False)
            if ok:
                s["cmd"] = self.store.commands[items.index(v)].uid
        else:
            return
        self._save()
        self.reload({rows[0]})

    # ---------------------------------------------------------- runner ---
    def toggle_run(self):
        if self.running:
            self.stop("stopped")
            return
        steps = self.steps
        err = check_script(steps) or ("script is empty" if not steps else "")
        missing = [i + 1 for i, s in enumerate(steps) if s["op"] == "send" and not self.store.by_uid(s["cmd"])]
        if missing:
            err = f"block(s) {missing}: command no longer exists"
        if err:
            self.host.logmsg(f"script '{self.name}': {err}", "err")
            self.b_run.setChecked(False)
            return
        self.running = True
        self.run_steps = [dict(s) for s in steps]    # snapshot: edits do not affect a running script
        self.pc, self.stack, self.n_sent = 0, [], 0
        self.b_run.setText("■ Stop")
        self.b_run.setChecked(True)
        for w in (self.script_cb, self.list):
            w.setEnabled(False)
        self.host.logmsg(f"script '{self.name}' started", "ok")
        self.timer.start(0)

    def stop(self, why="", level="info"):
        self.timer.stop()
        if not self.running:
            return
        self.running = False
        self.b_run.setChecked(False)
        self.b_run.setText("▶ Run")
        for w in (self.script_cb, self.list):
            w.setEnabled(True)
        self._mark(-1)
        self.host.logmsg(f"script '{self.name}' {why}, {self.n_sent} frame(s) sent", level)
        self.reload()

    def _mark(self, pc):
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setBackground(C_RUN if i == pc else QtGui.QBrush())
        if 0 <= pc < self.list.count():
            self.list.scrollToItem(self.list.item(pc))

    def _step(self):
        steps = self.run_steps
        budget = 200                      # blocks executed per tick before yielding to the GUI
        while self.running and budget > 0:
            budget -= 1
            if self.pc >= len(steps):
                if self.loop_chk.isChecked():
                    self.pc, self.stack = 0, []
                    if budget < 199:      # whole script without pauses: let the GUI breathe
                        self.timer.start(1)
                        return
                    continue
                self.stop("finished", "ok")
                return
            s = steps[self.pc]
            self._mark(self.pc)
            op = s["op"]
            if op == "send":
                c = self.store.by_uid(s["cmd"])
                if c is None or not self.host.send_frame(c.to_frame(self.host.FRAME)):
                    self.stop("aborted (send failed)", "err")
                    return
                self.n_sent += 1
                self.pc += 1
            elif op == "wait":
                self.pc += 1
                self._status()
                self.timer.start(int(s["ms"]))
                return
            elif op == "repeat":
                self.stack.append([self.pc + 1, s["count"]])
                self.pc += 1
            elif op == "end":
                top = self.stack[-1]
                if top[1] == 0:                # forever
                    self.pc = top[0]
                elif top[1] > 1:
                    top[1] -= 1
                    self.pc = top[0]
                else:
                    self.stack.pop()
                    self.pc += 1
        self._status()
        if self.running:
            self.timer.start(1)

    def _status(self):
        loops = " ".join("∞" if n == 0 else f"{n}" for _, n in self.stack)
        self.status.setText(f"running: block {self.pc}/{len(self.run_steps)}, sent {self.n_sent}"
                            + (f", repeats left [{loops}]" if loops else ""))
        self.status.setStyleSheet("color:#4fd18b")
