# -*- coding: utf-8 -*-
"""
ktr_control.py
==============
"Control KTR" tab: manual control of the КТР units over CAN.

Left: sub-tabs BOS / AFAR / UPNG. BOS: the commands of ПИЛВ 0102-004 v2 (bos_commands_0102.json) in three
groups - basic, session (СР) control, technological. Picking a command opens its editor: every data field
of the document with its range and meaning, presets, the CAN frame as it will be sent (live), the expected
acknowledgement. "Send now" sends it and shows the КВ, "Add to queue" puts it in the queue.

Right: the command queue. Besides BOS commands it holds the equipment operations of the Main tab sequences
(pause, HMP4040, N6701C, IT6832, TFG6806, FSW, CAN library frame, BOS test plan, repeat, log) and operator
prompts; it runs step by step, waits for every КВ and every instrument, and logs the results.
Stored in ktr_control.json next to the program.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import time

from PyQt5 import QtCore, QtGui, QtWidgets

import bos_protocol as bp
import bos_steps as bs

try:
    import dashboard as dsh
except Exception:                                     # pragma: no cover
    dsh = None

C_OK, C_ERR, C_DIM, C_WARN = "#4fd18b", "#ff6b6b", "#8a96a3", "#e3b341"
MONO = "font-family:Consolas,'DejaVu Sans Mono',monospace;"
GROUPS = [("basic", "Basic commands"), ("sr", "Session (СР) control commands"), ("tech", "Technological commands")]
EQUIPMENT = [("wait", "Pause"), ("hmp", "HMP4040"), ("n67", "N6701C"), ("it68", "IT6832"), ("tfg", "TFG6806"),
             ("fsw", "FSW run / single / level"), ("fsw_set", "FSW settings"), ("can", "CAN library frame"),
             ("bos", "BOS test plan"), ("prompt", "Operator prompt"), ("repeat", "Repeat … End"),
             ("log", "Log message")]
STEP_DEFAULTS = {"hmp": {"ch": {"1": {"v": 16.0, "i": 1.0, "on": True}}, "master": True},
                 "wait": {"ms": 1000}, "fsw": {"action": "single"}, "bos": {"plan": "", "stop_on_fail": True},
                 "fsw_set": {"app": "vsa", "params": {}}, "repeat": {"count": 3}, "log": {"text": ""}, "can": {},
                 "tfg": {"ch": [1], "p": {"wave": "SINusoid", "freq": 10e6, "ampl": 2.0, "out": True}},
                 "n67": {"ch": {"1": {"v": 12.0, "i": 1.0, "on": True}}, "all": None, "clear": False},
                 "it68": {"v": 14.0, "i": 1.5, "on": True}, "prompt": {"text": ""}}


def _app_dir() -> str:
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else \
        os.path.dirname(os.path.abspath(__file__))


STORE_FILE = os.path.join(_app_dir(), "ktr_control.json")


def check_steps(steps) -> str:
    depth = 0
    for i, s in enumerate(steps):
        if s["op"] == "repeat":
            depth += 1
        elif s["op"] == "end":
            depth -= 1
            if depth < 0:
                return f"step {i + 1}: 'End repeat' without 'Repeat'"
    return f"{depth} 'Repeat' without 'End repeat'" if depth else ""


def cmd_text(s) -> str:
    code = s.get("code", 0)
    name = bp.COMMANDS.get(code, (f"0x{code:02X}",))[0]
    p = bs.cmd_params_text(code, s.get("params", {}))
    exp = s.get("expect", "КВ+") if s.get("ack", True) and bp.wants_ack(code) else "no КВ"
    return f"→BOS 0x{code:02X} {name}" + (f" ({p})" if p else "") + f"  · CAN{s.get('line', 1)} · {exp}"


class PromptDialog(QtWidgets.QDialog):
    def __init__(self, parent, step):
        super().__init__(parent)
        self.setWindowTitle("Operator prompt")
        self.step = dict(step)
        f = QtWidgets.QFormLayout(self)
        self.text = QtWidgets.QPlainTextEdit(step.get("text", ""))
        self.text.setPlaceholderText("What the operator must do before the queue goes on")
        f.addRow("Text", self.text)
        f.addRow(QtWidgets.QLabel("The queue waits for OK; Stop ends the run."))
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def accept(self):
        if not self.text.toPlainText().strip():
            return
        self.step = {"op": "prompt", "text": self.text.toPlainText().strip()}
        super().accept()


# =============================================================================================== BOS page
class BosPanel(QtWidgets.QWidget):
    """Command groups (tree) + editor of the selected command."""

    def __init__(self, ktr):
        super().__init__()
        self.k = ktr
        self.code = None
        self.pw = {}
        self.edit_row = None                         # queue row being edited (None = new command)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        top = QtWidgets.QHBoxLayout()
        self.sender = QtWidgets.QComboBox()
        for a, n in bp.SENDERS:
            self.sender.addItem(f"0x{a:02X}  {n}", a)
        self.sender.setCurrentIndex(max(0, self.sender.findData(ktr.data.get("sender", 0x18))))
        self.sender.setToolTip("Sender address (AO) in the CAN identifier. The BOS executes commands only from "
                               "allowed addresses.")
        self.sender.currentIndexChanged.connect(self._opts_changed)
        self.mode = QtWidgets.QLabel()
        top.addWidget(QtWidgets.QLabel("Sender"))
        top.addWidget(self.sender, 1)
        top.addWidget(self.mode)
        v.addLayout(top)
        self.filter = QtWidgets.QLineEdit()
        self.filter.setPlaceholderText("Filter: code, mnemonic or text …")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._fill)
        v.addWidget(self.filter)
        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Code", "Command", "Description"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setStretchLastSection(True)
        self.tree.setColumnWidth(0, 120)
        self.tree.setColumnWidth(1, 190)
        self.tree.currentItemChanged.connect(self._picked)
        self.tree.itemDoubleClicked.connect(lambda it, _c: it.childCount() and it.setExpanded(not it.isExpanded()))
        split.addWidget(self.tree)
        # ---- editor
        ed = QtWidgets.QWidget()
        ev = QtWidgets.QVBoxLayout(ed)
        ev.setContentsMargins(0, 0, 0, 0)
        self.title = QtWidgets.QLabel("Choose a command")
        self.title.setStyleSheet("font-size:12pt;font-weight:700;")
        self.title.setWordWrap(True)
        self.info = QtWidgets.QLabel()
        self.info.setWordWrap(True)
        self.info.setStyleSheet(f"color:{C_DIM};")
        self.note = QtWidgets.QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"color:{C_WARN};")
        ev.addWidget(self.title)
        ev.addWidget(self.info)
        ev.addWidget(self.note)
        pr = QtWidgets.QHBoxLayout()
        self.preset = QtWidgets.QComboBox()
        self.preset.activated.connect(self._preset)
        pr.addWidget(QtWidgets.QLabel("Preset"))
        pr.addWidget(self.preset, 1)
        ev.addLayout(pr)
        self.fbox = QtWidgets.QGroupBox("Data field")
        self.fform = QtWidgets.QFormLayout(self.fbox)
        self.fform.setLabelAlignment(QtCore.Qt.AlignLeft)
        self.fform.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        ev.addWidget(self.fbox)
        opt = QtWidgets.QHBoxLayout()
        self.line = QtWidgets.QComboBox()
        self.line.addItem("CAN line 1", 1)
        self.line.addItem("CAN line 2", 2)
        self.line.setCurrentIndex(max(0, self.line.findData(ktr.data.get("line", 1))))
        self.line.currentIndexChanged.connect(self._opts_changed)
        self.ack = QtWidgets.QCheckBox("Request КВ")
        self.ack.setChecked(True)
        self.ack.toggled.connect(self._update)
        self.expect = QtWidgets.QComboBox()
        for k, t in (("КВ+", "expect КВ+"), ("КВ-", "expect КВ-"), ("any", "any answer")):
            self.expect.addItem(t, k)
        for w in (self.line, self.ack, self.expect):
            opt.addWidget(w)
        opt.addStretch(1)
        ev.addLayout(opt)
        self.frame = QtWidgets.QLabel("—")
        self.frame.setStyleSheet(MONO)
        self.frame.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.err = QtWidgets.QLabel()
        self.err.setStyleSheet(f"color:{C_ERR};")
        self.err.setWordWrap(True)
        ev.addWidget(self.frame)
        ev.addWidget(self.err)
        btn = QtWidgets.QHBoxLayout()
        self.b_send = QtWidgets.QPushButton("▶ Send now")
        self.b_send.setStyleSheet("font-weight:700;padding:5px 12px;")
        self.b_send.clicked.connect(self.send_now)
        self.b_queue = QtWidgets.QPushButton("＋ Add to queue →")
        self.b_queue.clicked.connect(self.to_queue)
        self.b_upd = QtWidgets.QPushButton("✔ Update queue step")
        self.b_upd.clicked.connect(self.update_step)
        self.b_upd.hide()
        for b in (self.b_send, self.b_queue, self.b_upd):
            btn.addWidget(b)
        btn.addStretch(1)
        ev.addLayout(btn)
        self.result = QtWidgets.QLabel()
        self.result.setWordWrap(True)
        ev.addWidget(self.result)
        ev.addStretch(1)
        esc = QtWidgets.QScrollArea()                 # the whole editor scrolls: fields keep their natural size
        esc.setWidgetResizable(True)
        esc.setFrameShape(QtWidgets.QFrame.NoFrame)
        esc.setWidget(ed)
        split.addWidget(esc)
        split.setSizes([300, 520])
        v.addWidget(split, 1)
        self._fill()

    # ---- tree
    def _fill(self, *_):
        q = self.filter.text().strip().lower()
        open_ = {self.tree.topLevelItem(i).data(0, QtCore.Qt.UserRole) for i in range(self.tree.topLevelItemCount())
                 if self.tree.topLevelItem(i).isExpanded()} if self.tree.topLevelItemCount() else {"basic"}
        self.tree.clear()
        bold = QtGui.QFont()
        bold.setBold(True)
        for g, gname in GROUPS:
            cmds = [(c, d) for c, d in bp.DEFS.items() if d.get("group", "basic") == g]
            if q:
                cmds = [(c, d) for c, d in cmds if q in f"0x{c:02x} {d['mnemonic']} {d['name']}".lower()]
            top = QtWidgets.QTreeWidgetItem([_tr(gname), "", f"{len(cmds)}"])
            top.setData(0, QtCore.Qt.UserRole, g)
            top.setFirstColumnSpanned(False)
            for col in range(3):
                top.setFont(col, bold)
            top.setFlags(top.flags() & ~QtCore.Qt.ItemIsSelectable)
            self.tree.addTopLevelItem(top)
            for c, d in cmds:
                it = QtWidgets.QTreeWidgetItem([f"0x{c:02X}", d["mnemonic"], d["name"]])
                it.setData(0, QtCore.Qt.UserRole, c)
                it.setToolTip(2, d["name"] + (f"\n{d['note']}" if d.get("note") else ""))
                it.setFont(0, QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
                if d.get("modes") == "ДЖР":
                    it.setForeground(0, QtGui.QColor(C_WARN))
                    it.setToolTip(0, "executed only in standby (ДЖР)")
                if d.get("reserved"):
                    for col in range(3):
                        it.setForeground(col, QtGui.QColor(C_DIM))
                top.addChild(it)
            top.setExpanded(bool(q) or g in open_)
        self.tree.resizeColumnToContents(0)

    def select_code(self, code):
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for k in range(top.childCount()):
                if top.child(k).data(0, QtCore.Qt.UserRole) == code:
                    top.setExpanded(True)
                    self.tree.setCurrentItem(top.child(k))
                    return

    def _picked(self, it, _prev=None):
        if it is None or it.parent() is None:
            return
        self.edit_row = None
        self.b_upd.hide()
        self.b_queue.setText("＋ Add to queue →")
        self.show_cmd(it.data(0, QtCore.Qt.UserRole), None)

    # ---- editor
    def show_cmd(self, code, params=None, line=None, ack=None, expect=None):
        d = bp.DEFS.get(code)
        if d is None:
            return
        self.code = code
        self.title.setText(f"0x{code:02X}  {d['mnemonic']} — {d['name']}")
        mode = "only in standby (ДЖР)" if d.get("modes") == "ДЖР" else "in any mode"
        recv = {"group": "group УПНГ+АФАР+БОС 0x0D"}.get(d.get("receiver"), "BOS 0x29")
        n = 8 if d.get("encoder") == "session" else d.get("bytes", 0)
        self.info.setText(f"§{d.get('section', '')} · {_tr(dict(GROUPS).get(d.get('group', 'basic')))} · "
                          f"executed {mode} · receiver {recv} · {n} data byte(s) · "
                          + ("acknowledgement КВ requested" if d.get("ack", True) else "sent without КВ"))
        self.note.setText(d.get("note", ""))
        self.note.setVisible(bool(d.get("note")))
        self.preset.clear()
        self.preset.addItem("(fill from a preset)", None)
        for p in d.get("presets", []):
            self.preset.addItem(p["name"], p.get("params", {}))
        self._build_fields(d, params if params is not None else
                           (d.get("presets") or [{}])[0].get("params", {}) if d.get("presets") else {})
        self.ack.setEnabled(d.get("ack", True))
        self.ack.setChecked(d.get("ack", True) if ack is None else bool(ack) and d.get("ack", True))
        self.expect.setCurrentIndex(max(0, self.expect.findData(expect or ("КВ-" if d.get("reserved") else "КВ+"))))
        if line in (1, 2):
            self.line.setCurrentIndex(self.line.findData(line))
        self._update()

    def _build_fields(self, d, params):
        while self.fform.rowCount():
            self.fform.removeRow(0)
        self.pw = {}
        for f in d.get("fields", []):
            w = bs.field_widget(f, params.get(f["name"], f.get("default")), self._update)
            w.setMinimumWidth(180)
            unit = f" , {f['unit']}" if f.get("unit") and f["unit"] not in ("°",) else ""
            lab = QtWidgets.QLabel(f"<b>{f['name']}</b>{unit.replace(' ,', ',')}")
            self.fform.addRow(lab, w)
            if f.get("note"):
                n = QtWidgets.QLabel(f["note"])
                n.setWordWrap(True)
                n.setStyleSheet(f"color:{C_DIM};font-size:8.5pt;")
                self.fform.addRow("", n)
            self.pw[f["name"]] = w
        if not d.get("fields"):
            self.fform.addRow(QtWidgets.QLabel("This command has no data field."))

    def _preset(self, i):
        p = self.preset.itemData(i)
        if p is not None and self.code is not None:
            self._build_fields(bp.DEFS[self.code], p)
            self._update()

    def params(self):
        return {k: w.get() for k, w in self.pw.items()}

    def _opts_changed(self, *_):
        self.k.data["sender"] = self.sender.currentData()
        self.k.data["line"] = self.line.currentData()
        self.k.save()
        self._update()

    def _update(self, *_):
        self.expect.setEnabled(self.ack.isChecked() and self.ack.isEnabled())
        if self.code is None:
            return False
        try:
            data = bp.encode(self.code, self.params())
            ack = self.ack.isChecked() and bp.wants_ack(self.code)
            cid, pl = bp.command(self.code, self.sender.currentData(), data, ack, bp.receiver_of(self.code))
            self.frame.setText(f"{self.k.line_name(self.line.currentData())}:  ID 0x{cid:08X}   "
                               f"[{len(pl)}] {pl.hex(' ').upper() or '—'}")
            self.err.setText("")
            ok = True
        except (ValueError, KeyError) as e:
            self.frame.setText("—")
            self.err.setText(str(e))
            ok = False
        busy = self.k.running or self.k.waiting
        self.b_send.setEnabled(ok and not busy)
        self.b_queue.setEnabled(ok)
        self.b_upd.setEnabled(ok)
        return ok

    def step(self) -> dict:
        return {"op": "bos_cmd", "code": self.code, "params": self.params(), "line": self.line.currentData(),
                "ack": self.ack.isChecked(), "expect": self.expect.currentData()}

    def send_now(self):
        if not self._update():
            return
        s = self.step()
        self.result.setText("sending …")
        self.result.setStyleSheet(f"color:{C_DIM};")
        self.k.send_step(s, self._shown)

    def _shown(self, r):
        col = C_OK if r.get("ok") else C_ERR
        self.result.setText(r.get("text", ""))
        self.result.setStyleSheet(f"color:{col};font-weight:600;")
        self._update()

    def to_queue(self):
        if self._update():
            self.k.queue_add(self.step())

    def edit_step(self, row, s):
        """Load a queue step into the editor ('Update queue step' writes it back)."""
        self.select_code(s.get("code"))
        self.show_cmd(s.get("code"), s.get("params", {}), s.get("line", 1), s.get("ack", True), s.get("expect"))
        self.edit_row = row
        self.b_upd.show()
        self.b_upd.setText(f"✔ Update queue step {row + 1}")
        self.b_queue.setText("＋ Add as new step →")

    def update_step(self):
        if self.edit_row is not None and self._update():
            self.k.queue_replace(self.edit_row, self.step())


def _tr(text):
    try:
        import i18n
        return i18n.tr(text, i18n.CURRENT)
    except Exception:
        return text


# =============================================================================================== tab
class KtrControl(QtWidgets.QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.data = {"queues": {"Queue 1": []}, "queue": "Queue 1", "sender": 0x18, "line": 1, "ack_ms": 300,
                     "stop_on_err": True}
        try:
            with open(STORE_FILE, encoding="utf-8") as fh:
                self.data.update(json.load(fh))
        except FileNotFoundError:
            pass
        except Exception as e:
            QtCore.QTimer.singleShot(0, lambda: self.log(f"Control KTR: cannot read {STORE_FILE}: {e}", "err"))
        if not self.data["queues"]:
            self.data["queues"] = {"Queue 1": []}
        if self.data.get("queue") not in self.data["queues"]:
            self.data["queue"] = next(iter(self.data["queues"]))
        self.running = False
        self.waiting = None                        # outstanding acknowledgement
        self.ack_timer = QtCore.QTimer(self, singleShot=True, timeout=self._ack_timeout)
        self.runner = QtCore.QTimer(self, singleShot=True, timeout=self._step)
        self._await_meas = None
        self._build()

    # ---------------------------------------------------------------- UI
    def _build(self):
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(4, 4, 4, 4)
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.units = QtWidgets.QTabWidget()
        self.bos = BosPanel(self)
        self.units.addTab(self.bos, "BOS")
        for name in ("AFAR", "UPNG"):
            w = QtWidgets.QLabel(f"{name}: commands will be detailed here later.\n\nMeanwhile use the CAN tab "
                                 f"library (lines '{name} CAN1' / '{name} CAN2') - its commands can be added to the "
                                 f"queue with 'CAN library frame'.")
            w.setAlignment(QtCore.Qt.AlignCenter)
            w.setWordWrap(True)
            w.setStyleSheet(f"color:{C_DIM};")
            self.units.addTab(w, name)
        split.addWidget(self.units)
        # ---- queue
        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(4, 0, 0, 0)
        top = QtWidgets.QHBoxLayout()
        lab = QtWidgets.QLabel("Command queue")
        lab.setStyleSheet("font-weight:700;font-size:11pt;")
        top.addWidget(lab)
        self.qcb = QtWidgets.QComboBox()
        self.qcb.addItems(list(self.data["queues"]))
        self.qcb.setCurrentText(self.data["queue"])
        self.qcb.currentTextChanged.connect(self._queue_changed)
        top.addWidget(self.qcb, 1)
        for txt, fn, tip in (("New", self.new_queue, "new empty queue"), ("Rename", self.rename_queue, ""),
                             ("Delete", self.delete_queue, "")):
            b = QtWidgets.QToolButton()
            b.setText(txt)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            top.addWidget(b)
        rv.addLayout(top)
        self.list = QtWidgets.QListWidget()
        self.list.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.list.itemDoubleClicked.connect(lambda *_: self.edit_step())
        self.list.setToolTip("Double-click a step to edit it (a BOS command opens in the editor on the left)")
        rv.addWidget(self.list, 3)
        eq = QtWidgets.QGridLayout()
        eq.setSpacing(3)
        for k, (op, name) in enumerate(EQUIPMENT):
            b = QtWidgets.QPushButton("+ " + name)
            col = (dsh.STEP_COLORS.get(op) if dsh else None) or ("#ff6b6b" if op == "prompt" else "#c9d1d9")
            b.setStyleSheet(f"color:{col};padding:2px 4px;")
            b.clicked.connect(lambda _=False, op=op: self.add_equipment(op))
            eq.addWidget(b, k // 4, k % 4)
        rv.addLayout(eq)
        ed = QtWidgets.QHBoxLayout()
        for txt, fn in (("▲", lambda: self.move(-1)), ("▼", lambda: self.move(1)), ("Copy", self.duplicate),
                        ("Edit", self.edit_step), ("Delete", self.delete_steps), ("Clear", self.clear_queue)):
            b = QtWidgets.QPushButton(txt)
            b.setStyleSheet("padding:2px 6px;")
            b.clicked.connect(fn)
            ed.addWidget(b)
        rv.addLayout(ed)
        run = QtWidgets.QHBoxLayout()
        self.run_btn = QtWidgets.QPushButton("▶ Run queue")
        self.run_btn.setStyleSheet("font-weight:700;padding:5px 12px;")
        self.run_btn.clicked.connect(lambda: self.toggle_run(0))
        self.run_sel = QtWidgets.QPushButton("▶ From selected")
        self.run_sel.clicked.connect(lambda: self.toggle_run(None))
        self.loop_chk = QtWidgets.QCheckBox("Loop")
        self.stop_chk = QtWidgets.QCheckBox("Stop on wrong / missing КВ")
        self.stop_chk.setChecked(self.data.get("stop_on_err", True))
        self.stop_chk.toggled.connect(lambda on: (self.data.__setitem__("stop_on_err", on), self.save()))
        for w in (self.run_btn, self.run_sel, self.loop_chk, self.stop_chk):
            run.addWidget(w)
        run.addStretch(1)
        rv.addLayout(run)
        self.status = QtWidgets.QLabel()
        self.status.setStyleSheet(f"color:{C_DIM};")
        rv.addWidget(self.status)
        lh = QtWidgets.QHBoxLayout()
        lh.addWidget(QtWidgets.QLabel("Results"))
        lh.addStretch(1)
        b = QtWidgets.QToolButton()
        b.setText("Clear")
        b.clicked.connect(lambda: self.out.clear())
        lh.addWidget(b)
        rv.addLayout(lh)
        self.out = QtWidgets.QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setMaximumBlockCount(5000)
        self.out.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        rv.addWidget(self.out, 2)
        split.addWidget(right)
        split.setSizes([620, 560])
        h.addWidget(split)
        self.reload()
        self.mode_timer = QtCore.QTimer(self, interval=1000, timeout=self._show_mode)
        self.mode_timer.start()
        self._show_mode()

    # ---------------------------------------------------------------- helpers
    def log(self, text, level="info"):
        fn = getattr(self.host, "logmsg", None)
        if fn:
            fn(text, level)

    def result(self, text, ok=True):
        col = C_OK if ok is True else C_ERR if ok is False else C_DIM
        self.out.appendHtml(f"<span style='color:{C_DIM}'>{time.strftime('%H:%M:%S')}</span> "
                            f"<span style='color:{col}'>{_esc(text)}</span>")

    def save(self):
        try:
            tmp = STORE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=1, ensure_ascii=False)
            os.replace(tmp, STORE_FILE)
        except OSError as e:
            self.log(f"Control KTR: cannot save {STORE_FILE}: {e}", "err")

    def _station(self):
        return getattr(self.host, "bos", None)

    def demo(self) -> bool:
        st = self._station()
        return bool(st and st._is_demo())

    def line_name(self, n) -> str:
        st = self._station()
        ctl = st.data["ctl"] if st else {}
        name = ctl.get("line2" if n == 2 else "line", f"BOS CAN{n}")
        c = getattr(self.host, "can_win", None)
        try:
            import canable_analyzer as ca
            return ca.resolve_line(name, c.line_names() if c is not None else [])
        except Exception:
            return name

    def _show_mode(self):
        if self.demo():
            t, col = "DEMO: simulated BOS", C_WARN
        else:
            c = getattr(self.host, "can_win", None)
            names = [self.line_name(1), self.line_name(2)]
            up = [n for n in names if c is not None and getattr(c.workers.get(n), "backend", None) is not None] \
                if c is not None and hasattr(c, "workers") else []
            t = "connected: " + ", ".join(up) if up else "BOS CAN lines not connected (CAN tab)"
            col = C_OK if up else C_ERR
        self.bos.mode.setText(t)
        self.bos.mode.setStyleSheet(f"color:{col};font-weight:600;")

    # ---------------------------------------------------------------- send + acknowledgement
    def send_step(self, s, done):
        """Send a bos_cmd step; done({"ok", "text", "ack"}) when the КВ arrives / times out / is not wanted."""
        code = s["code"]
        name = bp.COMMANDS.get(code, (f"0x{code:02X}",))[0]
        st = self._station()
        if st is not None and st.running:
            return done({"ok": False, "text": f"{name}: not sent - the BOS Test Station is running a plan"})
        try:
            data = bp.encode(code, s.get("params", {}))
        except (ValueError, KeyError) as e:
            return done({"ok": False, "text": f"{name}: {e}"})
        ack = bool(s.get("ack", True)) and bp.wants_ack(code)
        sender = self.bos.sender.currentData()
        cid, payload = bp.command(code, sender, data, ack, bp.receiver_of(code))
        line = self.line_name(s.get("line", 1))
        frame = f"ID 0x{cid:08X} [{len(payload)}] {payload.hex(' ').upper() or '—'}"
        expect = s.get("expect", "КВ+")
        if self.demo():
            good = st.sim.handle(code, bytes(payload) + bytes(8), st._sim_map())
            r = {"ack": ("КВ+" if good else "КВ-") if ack else None, "via": s.get("line", 1), "ms": 0}
            QtCore.QTimer.singleShot(10, lambda: done(self._judge(name, frame, line, r, ack, expect, demo=True)))
            return
        c = getattr(self.host, "can_win", None)
        if c is None or not c.send_frame(c.FRAME(0, cid, True, False, None, payload, bus=line)):
            return done({"ok": False, "text": f"{name}: not sent - line '{line}' not connected (CAN tab)"})
        if not ack:
            return done(self._judge(name, frame, line, {"ack": None}, ack, expect))
        self.waiting = {"code": code, "sender": sender, "t0": time.monotonic(), "done": done, "name": name,
                        "frame": frame, "line": line, "expect": expect}
        self.bos._update()
        self.ack_timer.start(int(self.data.get("ack_ms", 300)))

    def _judge(self, name, frame, line, r, ack, expect, demo=False):
        tag = " (demo)" if demo else ""
        if not ack:
            return {"ok": True, "ack": None, "text": f"{name} sent on {line}{tag}: {frame} (no КВ requested)"}
        got = r.get("ack")
        if got is None or got == "none":
            return {"ok": False, "ack": "none", "text": f"{name} sent on {line}: {frame} → NO КВ within "
                                                         f"{self.data.get('ack_ms', 300)} ms"}
        ok = expect == "any" or got == expect
        how = f"{got} via CAN{r.get('via', 1)}" + (f", {r['ms']:.0f} ms" if r.get("ms") else "") + \
            (f" — {r['why']}" if r.get("why") else "")
        return {"ok": ok, "ack": got, "text": f"{name} sent on {line}{tag}: {frame} → {how}" +
                ("" if ok else f" (expected {expect})")}

    def on_frames(self, frames):
        w = self.waiting
        if not w:
            return
        for fr in frames:
            a = bp.parse_ack(fr.can_id, fr.ext, fr.data, w["sender"])
            if a and a["code"] == w["code"]:
                self.ack_timer.stop()
                self.waiting = None
                why = "" if not a["negative"] else ("command does not exist" if a["unknown"] else
                                                    "sender not allowed / not executable now (standby only?)")
                r = {"ack": "КВ-" if a["negative"] else "КВ+", "via": 2 if a["can2"] else 1,
                     "ms": (time.monotonic() - w["t0"]) * 1000, "why": why}
                res = self._judge(w["name"], w["frame"], w["line"], r, True, w["expect"])
                QtCore.QTimer.singleShot(0, lambda: (w["done"](res), self.bos._update()))
                return

    def _ack_timeout(self):
        w, self.waiting = self.waiting, None
        if w:
            w["done"](self._judge(w["name"], w["frame"], w["line"], {"ack": "none"}, True, w["expect"]))
            self.bos._update()

    # ---------------------------------------------------------------- queue editing
    @property
    def steps(self) -> list:
        return self.data["queues"].setdefault(self.qcb.currentText(), [])

    def text(self, s) -> str:
        if s["op"] == "bos_cmd":
            return cmd_text(s)
        if s["op"] == "prompt":
            return "☝ Operator   " + s.get("text", "").splitlines()[0][:70]
        return dsh.step_text(self.host, s) if dsh else s["op"]

    def reload(self, select=()):
        self.list.clear()
        depth = 0
        for i, s in enumerate(self.steps):
            if s["op"] == "end":
                depth = max(0, depth - 1)
            it = QtWidgets.QListWidgetItem(f"{i + 1:>3}  " + "    " * depth + self.text(s))
            col = "#79c0ff" if s["op"] == "bos_cmd" else "#ff6b6b" if s["op"] == "prompt" else \
                (dsh.STEP_COLORS.get(s["op"], "#c9d1d9") if dsh else "#c9d1d9")
            if s["op"] == "can" and "<missing" in it.text():
                col = C_ERR
            it.setForeground(QtGui.QColor(col))
            self.list.addItem(it)
            if i in select:
                it.setSelected(True)
            if s["op"] == "repeat":
                depth += 1
        if not self.running:
            err = check_steps(self.steps)
            self.status.setText(err or f"{len(self.steps)} steps")
            self.status.setStyleSheet(f"color:{C_ERR if err else C_DIM};")

    def _sel(self):
        return sorted(self.list.row(i) for i in self.list.selectedItems())

    def _insert(self, new: list):
        rows = self._sel()
        pos = rows[-1] + 1 if rows else len(self.steps)
        self.steps[pos:pos] = new
        self.save()
        self.reload({pos})
        self.list.scrollToItem(self.list.item(pos))

    def queue_add(self, s):
        if self.running:
            return
        self._insert([copy.deepcopy(s)])
        self.result(f"added to the queue: {cmd_text(s)}", None)

    def queue_replace(self, row, s):
        if self.running or not 0 <= row < len(self.steps):
            return
        self.steps[row] = copy.deepcopy(s)
        self.save()
        self.reload({row})

    def add_equipment(self, op):
        if self.running:
            return
        if op == "prompt":
            dlg = PromptDialog(self, {"op": "prompt", "text": ""})
            if dlg.exec_() == QtWidgets.QDialog.Accepted:
                self._insert([dlg.step])
            return
        if dsh is None:
            return
        cmds = dsh._can_cmds(self.host)
        if op == "repeat":
            dlg = dsh.StepDialog(self, {"op": "repeat", "count": 3}, [])
            if dlg.exec_() != QtWidgets.QDialog.Accepted:
                return
            rows = self._sel()
            if rows:                                  # around the selected steps
                self.steps.insert(rows[-1] + 1, {"op": "end"})
                self.steps.insert(rows[0], dlg.step)
                self.save()
                self.reload({rows[0]})
            else:
                self._insert([dlg.step, {"op": "end"}])
            return
        dlg = dsh.StepDialog(self, {"op": op, **copy.deepcopy(STEP_DEFAULTS[op])}, cmds, self.host)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            self._insert([dlg.step])

    def edit_step(self):
        rows = self._sel()
        if self.running or len(rows) != 1:
            return
        r = rows[0]
        s = self.steps[r]
        if s["op"] == "end":
            return
        if s["op"] == "bos_cmd":
            self.units.setCurrentWidget(self.bos)
            self.bos.edit_step(r, s)
            return
        if s["op"] == "prompt":
            dlg = PromptDialog(self, s)
        elif dsh:
            dlg = dsh.StepDialog(self, s, dsh._can_cmds(self.host), self.host)
        else:
            return
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            self.steps[r] = dlg.step
            self.save()
            self.reload({r})

    def delete_steps(self):
        rows = self._sel()
        if self.running or not rows:
            return
        for r in reversed(rows):
            del self.steps[r]
        self.save()
        self.reload()

    def clear_queue(self):
        if self.running or not self.steps:
            return
        if QtWidgets.QMessageBox.question(self, "Control KTR", f"Remove all {len(self.steps)} steps of "
                                          f"'{self.qcb.currentText()}'?") == QtWidgets.QMessageBox.Yes:
            self.steps.clear()
            self.save()
            self.reload()

    def duplicate(self):
        rows = self._sel()
        if self.running or not rows:
            return
        pos = rows[-1] + 1
        self.steps[pos:pos] = [copy.deepcopy(self.steps[r]) for r in rows]
        self.save()
        self.reload(set(range(pos, pos + len(rows))))

    def move(self, d):
        rows = self._sel()
        if self.running or not rows:
            return
        st, a, b = self.steps, rows[0], rows[-1]
        if d < 0 and a > 0:
            st.insert(b, st.pop(a - 1))
        elif d > 0 and b < len(st) - 1:
            st.insert(a, st.pop(b + 1))
        else:
            return
        self.save()
        self.reload({r + d for r in rows})

    def _queue_changed(self, name):
        if name:
            self.data["queue"] = name
            self.save()
            self.reload()

    def new_queue(self):
        n = len(self.data["queues"]) + 1
        name, ok = QtWidgets.QInputDialog.getText(self, "New queue", "Name:", text=f"Queue {n}")
        name = name.strip()
        if ok and name and name not in self.data["queues"]:
            self.data["queues"][name] = []
            self.qcb.addItem(name)
            self.qcb.setCurrentText(name)

    def rename_queue(self):
        old = self.qcb.currentText()
        name, ok = QtWidgets.QInputDialog.getText(self, "Rename queue", "Name:", text=old)
        name = name.strip()
        if ok and name and name != old and name not in self.data["queues"]:
            self.data["queues"] = {(name if k == old else k): v for k, v in self.data["queues"].items()}
            self.data["queue"] = name
            self.qcb.setItemText(self.qcb.currentIndex(), name)
            self.save()

    def delete_queue(self):
        if self.running:
            return
        name = self.qcb.currentText()
        if QtWidgets.QMessageBox.question(self, "Delete", f"Delete queue '{name}'?") != QtWidgets.QMessageBox.Yes:
            return
        del self.data["queues"][name]
        if not self.data["queues"]:
            self.data["queues"]["Queue 1"] = []
        self.qcb.blockSignals(True)
        self.qcb.clear()
        self.qcb.addItems(list(self.data["queues"]))
        self.qcb.blockSignals(False)
        self.data["queue"] = self.qcb.currentText()
        self.save()
        self.reload()

    # ---------------------------------------------------------------- runner
    def toggle_run(self, start=0):
        if self.running:
            return self.stop("stopped by the operator", "warn")
        err = check_steps(self.steps) or ("the queue is empty" if not self.steps else "")
        if err:
            self.result(f"queue '{self.qcb.currentText()}': {err}", False)
            return
        if start is None:
            rows = self._sel()
            start = rows[0] if rows else 0
        st = self._station()
        if st is not None and st.running:
            self.result("the BOS Test Station is running a plan - wait until it ends", False)
            return
        self.run_steps = copy.deepcopy(self.steps)
        self.pc, self.stack, self.pending, self.wait_until, self.n_err = start, [], None, 0.0, 0
        self.running = True
        self.run_btn.setText("■ Stop")
        self.run_sel.setEnabled(False)
        self.qcb.setEnabled(False)
        self.result(f"— queue '{self.qcb.currentText()}' started" + (f" at step {start + 1}" if start else "") +
                    (" (DEMO: simulated BOS)" if self.demo() else "") + " —", None)
        self.bos._update()
        self.runner.start(0)

    def stop(self, why="", level="info"):
        self.runner.stop()
        if not self.running:
            return
        self.running = False
        self.pending = None
        self.run_btn.setText("▶ Run queue")
        self.run_sel.setEnabled(True)
        self.qcb.setEnabled(True)
        self._mark(-1)
        self.result(f"— queue '{self.qcb.currentText()}' {why} —", True if level == "ok" else False
                    if level == "err" else None)
        self.log(f"Control KTR: queue '{self.qcb.currentText()}' {why}", level)
        self.reload()
        self.bos._update()

    def _mark(self, pc):
        for i in range(self.list.count()):
            self.list.item(i).setBackground(QtGui.QColor("#1f6f43") if i == pc else QtGui.QBrush())
        if 0 <= pc < self.list.count():
            self.list.scrollToItem(self.list.item(pc))

    def _fail(self, msg):
        self.result(f"step {self.pc + 1}: {msg}", False)
        self.stop(f"ABORTED at step {self.pc + 1}", "err")

    def _step(self):
        if not self.running:
            return
        p = self.pending
        if p is not None:
            if isinstance(p, dict):                   # BOS command: waiting for its КВ
                if not p.get("done"):
                    self.runner.start(10)
                    return
                self.pending = None
                r = p["r"]
                self.result(f"step {self.pc + 1}: {r['text']}", r["ok"])
                if not r["ok"]:
                    self.n_err += 1
                    if self.stop_chk.isChecked():
                        return self.stop(f"ABORTED at step {self.pc + 1}", "err")
                self.pc += 1
            else:                                    # instrument job (dashboard Token)
                if not p.done:
                    if p.deadline and time.monotonic() > p.deadline:
                        self._await_meas = None
                        return self._fail(f"{p.what}: timeout")
                    self.runner.start(20)
                    return
                self.pending = None
                if p.error:
                    return self._fail(f"{p.what}: {p.error}")
                msg = p.msg or getattr(getattr(p, "msg_from", None), "msg", "")
                self.result(f"step {self.pc + 1}: {msg or p.what + ' done'}", True)
                self.pc += 1
        if time.monotonic() < self.wait_until:
            left = self.wait_until - time.monotonic()
            self.status.setText(f"step {self.pc}/{len(self.run_steps)}: pause, {left:.1f} s left")
            self.runner.start(min(100, int(left * 1000) + 1))
            return
        for _ in range(200):
            if self.pc >= len(self.run_steps):
                if self.loop_chk.isChecked():
                    self.pc, self.stack = 0, []
                    self.runner.start(1)
                    return
                return self.stop("finished" + (f" with {self.n_err} error(s)" if self.n_err else ""),
                                 "warn" if self.n_err else "ok")
            s = self.run_steps[self.pc]
            self._mark(self.pc)
            self.status.setText(f"running: step {self.pc + 1}/{len(self.run_steps)}")
            self.status.setStyleSheet(f"color:{C_OK};")
            op = s["op"]
            try:
                if op == "repeat":
                    self.stack.append([self.pc + 1, s["count"]])
                    self.pc += 1
                    continue
                if op == "end":
                    top = self.stack[-1]
                    if top[1] == 0 or top[1] > 1:
                        if top[1] > 1:
                            top[1] -= 1
                        self.pc = top[0]
                    else:
                        self.stack.pop()
                        self.pc += 1
                    continue
                if op == "wait":
                    self.pc += 1
                    self.wait_until = time.monotonic() + s["ms"] / 1000
                    self.runner.start(min(100, s["ms"]))
                    return
                if op == "prompt":
                    r = QtWidgets.QMessageBox.question(self, "Control KTR — operator", s.get("text", ""),
                                                       QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Abort)
                    if r != QtWidgets.QMessageBox.Ok:
                        return self.stop(f"stopped by the operator at step {self.pc + 1}", "warn")
                    self.result(f"step {self.pc + 1}: operator confirmed", True)
                    self.pc += 1
                    continue
                if op == "bos_cmd":
                    box = {"done": False}
                    self.pending = box

                    def got(r, box=box):
                        box["r"], box["done"] = r, True
                    self.send_step(s, got)
                    self.runner.start(5)
                    return
                if dsh is None:
                    raise RuntimeError("dashboard.py missing - equipment steps not available")
                tok = dsh.exec_device(self.host, s, lambda t, lv="info": self.result(t, None),
                                      lambda tk, t: setattr(self, "_await_meas", (tk, t)))
                if tok is not None:
                    self.pending = tok
                    self.runner.start(20)
                    return
                if op != "log":
                    self.result(f"step {self.pc + 1}: {self.text(s)}", True)
                self.pc += 1
            except Exception as e:
                return self._fail(f"{type(e).__name__}: {e}")
        self.runner.start(1)

    def on_measurement(self, r: dict):
        """FSW result drawn by the main window (completes 'FSW single' steps)."""
        aw = self._await_meas
        if aw and r.get("mode") == "sync" and r.get("t", 0) >= aw[1]:
            aw[0].done = True
            self._await_meas = None

    def shutdown(self):
        if self.running:
            self.stop("stopped (application closed)", "warn")


def _esc(t):
    import html
    return html.escape(str(t))
