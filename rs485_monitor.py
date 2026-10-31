# -*- coding: utf-8 -*-
"""
rs485_monitor.py
================
"RS-485" tab of fsw_vsa_monitor.py: the four ports of a MOXA UPort 1450I
(USB to 4 x RS-232/422/485, each port appears in Windows as its own COM port).

* Up to 4 ports at once, each with its own COM port, baud rate, data bits, parity and stop bits.
* Received bytes are grouped into frames: a pause longer than the frame gap ends a frame.
* Trace (every frame, hex + ASCII), port filter, pause, clear.
* Transmit: port + hex bytes, once or periodically.
* Command library (key, name, port, hex bytes), double-click / Send to transmit;
  also used by the "+ RS-485" sequence step on the Main tab. Stored in rs485_commands.json.
* Record to a log file (rs485_log_*.log, one line per frame).
* Demo mode: a simulated FRM answers on port 1.

The FRM command layout is not entered yet - commands are plain hex bytes for now.

RS-485 mode: select "RS-485 2W" (or 4W) for each port in the MOXA driver (Device Manager ->
MOXA UPort 1450I -> port -> Port Settings / Properties). pyserial then only opens the COM port.
"""

from __future__ import annotations

import json
import os
import queue
import string
import sys
import threading
import time
import uuid

from PyQt5 import QtCore, QtGui, QtWidgets

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # pragma: no cover - demo mode still works
    serial = None


def script_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


N_PORTS = 4                         # MOXA UPort 1450I
BAUDS = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
PARITIES = [("None", "N"), ("Even", "E"), ("Odd", "O"), ("Mark", "M"), ("Space", "S")]
DEFAULT_PORTS = [  # name, COM, baud, data bits, parity, stop bits, enabled
    {"name": "FRM", "com": "", "baud": 115200, "bits": 8, "parity": "N", "stop": 1, "on": True},
    {"name": "P2", "com": "", "baud": 115200, "bits": 8, "parity": "N", "stop": 1, "on": False},
    {"name": "P3", "com": "", "baud": 115200, "bits": 8, "parity": "N", "stop": 1, "on": False},
    {"name": "P4", "com": "", "baud": 115200, "bits": 8, "parity": "N", "stop": 1, "on": False},
]
PORT_COLORS = ["#f0b03e", "#79c0ff", "#4fd18b", "#d2a8ff"]
MOXA_HINTS = ("MOXA", "UPORT")
TRACE_MAX = 50_000
PORTS_FILE = "rs485_ports.json"
COMMANDS_FILE = "rs485_commands.json"


def parse_hex(text: str) -> bytes:
    s = "".join(text.replace("0x", "").replace("0X", "").replace(",", " ").split())
    if len(s) % 2:
        raise ValueError("odd number of hex digits")
    return bytes.fromhex(s)


def hex_str(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def ascii_str(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


def list_ports() -> list[tuple[str, str]]:
    """[(COM, description)], MOXA ports first."""
    if serial is None:
        return []
    try:
        ports = list(serial.tools.list_ports.comports())
    except Exception:
        return []
    out = [(p.device, f"{p.device}  {p.description}") for p in ports]
    moxa = lambda d: any(h in d.upper() for h in MOXA_HINTS)
    return sorted(out, key=lambda x: (not moxa(x[1]), x[0]))


class Frame:
    """One RS-485 frame: time, port name, direction ('RX' / 'TX'), bytes."""
    __slots__ = ("ts", "port", "dir", "data")

    def __init__(self, ts, port, direction, data: bytes):
        self.ts, self.port, self.dir, self.data = ts, port, direction, bytes(data)

    def log_line(self) -> str:
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts)) + f".{int(self.ts * 1000) % 1000:03d}"
        return f"{t} {self.port} {self.dir} {hex_str(self.data)}"


# --------------------------------------------------------------------------- #
#  Ports                                                                       #
# --------------------------------------------------------------------------- #
class SerialPort:
    def __init__(self, cfg: dict):
        if serial is None:
            raise RuntimeError("pyserial is not installed (pip install pyserial)")
        self.s = serial.Serial(cfg["com"], int(cfg["baud"]), bytesize=int(cfg["bits"]),
                               parity=cfg["parity"], stopbits=float(cfg["stop"]) if cfg["stop"] == 1.5
                               else int(cfg["stop"]), timeout=0, write_timeout=1.0)
        self.info = f"{cfg['com']} {cfg['baud']} {cfg['bits']}{cfg['parity']}{cfg['stop']}"

    def read(self) -> bytes:
        n = self.s.in_waiting
        return self.s.read(n) if n else b""

    def write(self, data: bytes):
        self.s.write(data)
        self.s.flush()

    def close(self):
        self.s.close()


class DemoPort:
    """Simulated FRM: echoes every request with a status reply, sends a status frame every second."""

    def __init__(self, cfg: dict):
        self.name = cfg["name"]
        self.info = f"demo {cfg['baud']} {cfg['bits']}{cfg['parity']}{cfg['stop']}"
        self.buf = bytearray()
        self.next = time.monotonic() + 1.0
        self.cnt = 0
        self.lock = threading.Lock()

    def read(self) -> bytes:
        now = time.monotonic()
        with self.lock:
            if self.name == "FRM" and now >= self.next:
                self.next = now + 1.0
                self.cnt = (self.cnt + 1) & 0xFF
                self.buf += bytes([0xAA, 0x55, 0x01, self.cnt, 0x10 + self.cnt % 5, 0x00])
            out, self.buf = bytes(self.buf), bytearray()
        return out

    def write(self, data: bytes):
        with self.lock:
            self.buf += bytes([0xAA, 0x55, 0x80, len(data)]) + data[:4]

    def close(self):
        pass


class PortWorker(QtCore.QThread):
    """Reads one port; bytes separated by more than `gap_s` form separate frames."""
    frame = QtCore.pyqtSignal(object)
    error = QtCore.pyqtSignal(str, str)          # port name, message

    def __init__(self, name, dev, gap_s):
        super().__init__()
        self.name, self.dev, self.gap_s = name, dev, gap_s
        self.tx: queue.Queue = queue.Queue()
        self._quit = False

    def send(self, data: bytes):
        self.tx.put(data)

    def stop(self):
        self._quit = True
        self.wait(3000)

    def run(self):
        buf, t0, last = bytearray(), 0.0, 0.0
        try:
            while not self._quit:
                try:
                    data = self.tx.get_nowait()
                except queue.Empty:
                    data = None
                if data is not None:
                    if buf:                                  # finish a pending RX frame first
                        self.frame.emit(Frame(t0, self.name, "RX", buf))
                        buf = bytearray()
                    self.dev.write(data)
                    self.frame.emit(Frame(time.time(), self.name, "TX", data))
                    continue
                chunk = self.dev.read()
                now = time.monotonic()
                if chunk:
                    if not buf:
                        t0 = time.time()
                    buf += chunk
                    last = now
                elif buf and now - last >= self.gap_s:
                    self.frame.emit(Frame(t0, self.name, "RX", buf))
                    buf = bytearray()
                else:
                    time.sleep(0.001)
        except Exception as e:
            self.error.emit(self.name, f"{type(e).__name__}: {e}")
        finally:
            try:
                self.dev.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
#  Command library                                                             #
# --------------------------------------------------------------------------- #
class Command:
    def __init__(self, key="", name="", data=b"", port="", uid=None):
        self.uid = uid or uuid.uuid4().hex[:12]
        self.key, self.name, self.data, self.port = key, name, bytes(data), port

    def data_str(self) -> str:
        return hex_str(self.data)

    def label(self) -> str:
        return (f"[{self.key}] {self.name}" if self.name else f"[{self.key}]") + (f" @{self.port}" if self.port else "")

    def to_dict(self):
        return {"uid": self.uid, "key": self.key, "name": self.name, "data": self.data.hex().upper(),
                "port": self.port}

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("key", ""), d.get("name", ""), bytes.fromhex(d.get("data", "")), d.get("port", ""),
                   d.get("uid"))


class Store:
    def __init__(self, path):
        self.path = path
        self.commands: list[Command] = []
        self.error = ""
        try:
            with open(path, encoding="utf-8") as f:
                self.commands = [Command.from_dict(c) for c in json.load(f).get("commands", [])]
        except FileNotFoundError:
            pass
        except Exception as e:
            self.error = f"cannot read {path}: {e}"

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"commands": [c.to_dict() for c in self.commands]}, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    def by_uid(self, uid) -> Command | None:
        return next((c for c in self.commands if c.uid == uid), None)

    def next_key(self) -> str:
        used = {c.key for c in self.commands}
        for k in string.ascii_uppercase + string.digits:
            if k not in used:
                return k
        n = 1
        while f"C{n}" in used:
            n += 1
        return f"C{n}"


class CommandDialog(QtWidgets.QDialog):
    def __init__(self, parent, cmd: Command, taken: set, ports):
        super().__init__(parent)
        self.setWindowTitle("RS-485 command")
        self.cmd, self.taken = cmd, taken
        f = QtWidgets.QFormLayout(self)
        self.key = QtWidgets.QLineEdit(cmd.key)
        self.key.setMaxLength(4)
        self.name = QtWidgets.QLineEdit(cmd.name)
        self.data = QtWidgets.QLineEdit(cmd.data_str())
        self.data.setPlaceholderText("hex bytes, e.g. AA 55 01 00")
        self.data.setMinimumWidth(360)
        self.port = QtWidgets.QComboBox()
        self.port.addItem("(port selected in the Transmit panel)", "")
        for n in list(ports) + ([cmd.port] if cmd.port and cmd.port not in ports else []):
            self.port.addItem(n, n)
        self.port.setCurrentIndex(max(0, self.port.findData(cmd.port)))
        f.addRow("Key", self.key)
        f.addRow("Name", self.name)
        f.addRow("Data (hex)", self.data)
        f.addRow("Send on port", self.port)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def accept(self):
        try:
            key = self.key.text().strip()
            if not key:
                raise ValueError("key is empty")
            if key in self.taken:
                raise ValueError(f"key '{key}' is already used")
            data = parse_hex(self.data.text())
            if not data:
                raise ValueError("no data bytes")
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "Command", str(e))
            return
        c = self.cmd
        c.key, c.name, c.data, c.port = key, self.name.text().strip(), data, self.port.currentData() or ""
        super().accept()


# --------------------------------------------------------------------------- #
#  Trace model                                                                 #
# --------------------------------------------------------------------------- #
class TraceModel(QtCore.QAbstractTableModel):
    COLS = ["Time", "Port", "Dir", "Len", "Data (hex)", "ASCII"]

    def __init__(self, colors):
        super().__init__()
        self.rows: list[Frame] = []
        self.colors = colors
        self.mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return len(self.COLS)

    def headerData(self, s, o, role=QtCore.Qt.DisplayRole):
        if o == QtCore.Qt.Horizontal and role == QtCore.Qt.DisplayRole:
            return self.COLS[s]
        return None

    def data(self, idx, role=QtCore.Qt.DisplayRole):
        f = self.rows[idx.row()]
        c = idx.column()
        if role == QtCore.Qt.DisplayRole:
            if c == 0:
                return time.strftime("%H:%M:%S", time.localtime(f.ts)) + f".{int(f.ts * 1000) % 1000:03d}"
            return (None, f.port, f.dir, str(len(f.data)), hex_str(f.data), ascii_str(f.data))[c]
        if role == QtCore.Qt.FontRole and c >= 3:
            return self.mono
        if role == QtCore.Qt.ForegroundRole:
            if c == 1:
                return QtGui.QBrush(QtGui.QColor(self.colors.get(f.port, "#c9d1d9")))
            if f.dir == "TX":
                return QtGui.QBrush(QtGui.QColor("#58a6ff"))
        return None

    def add(self, frames):
        if not frames:
            return
        over = len(self.rows) + len(frames) - TRACE_MAX
        if over > 0:
            self.beginRemoveRows(QtCore.QModelIndex(), 0, over - 1)
            del self.rows[:over]
            self.endRemoveRows()
        n = len(self.rows)
        self.beginInsertRows(QtCore.QModelIndex(), n, n + len(frames) - 1)
        self.rows.extend(frames)
        self.endInsertRows()

    def clear(self):
        self.beginResetModel()
        self.rows = []
        self.endResetModel()


# --------------------------------------------------------------------------- #
#  Tab                                                                         #
# --------------------------------------------------------------------------- #
class Rs485Tab(QtWidgets.QWidget):
    """Interface used by other tabs: store, send_command(cmd), send(port, data), connected_ports(),
    status (QLabel), n_rx, frame_listeners (callables(list[Frame])), demo_setup(), connect_all(), shutdown()."""
    log = QtCore.pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.ports_file = os.path.join(script_dir(), PORTS_FILE)
        self.cfg = [dict(p) for p in DEFAULT_PORTS]
        try:
            with open(self.ports_file, encoding="utf-8") as f:
                saved = json.load(f)
            for i, p in enumerate(saved.get("ports", [])[:N_PORTS]):
                self.cfg[i].update(p)
            self.gap_ms = int(saved.get("gap_ms", 5))
        except Exception:
            self.gap_ms = 5
        self.demo = False
        self.store = Store(os.path.join(script_dir(), COMMANDS_FILE))
        self.workers: dict[str, PortWorker] = {}
        self.frame_listeners = []
        self.n_rx = 0
        self.n_tx = 0
        self.pending: list[Frame] = []
        self.rec = None
        self._build()
        self.flush = QtCore.QTimer(self, interval=100, timeout=self._flush)
        self.flush.start()
        self.periodic = QtCore.QTimer(self, timeout=self.on_send)
        if self.store.error:
            QtCore.QTimer.singleShot(0, lambda: self.log.emit(self.store.error, "err"))

    # ------------------------------------------------------------------ build
    def _build(self):
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)

        g = QtWidgets.QGroupBox("MOXA UPort 1450I ports (RS-485)")
        grid = QtWidgets.QGridLayout(g)
        for c, h in enumerate(("Use", "Name", "COM port", "Baud", "Bits", "Parity", "Stop")):
            grid.addWidget(QtWidgets.QLabel(f"<b>{h}</b>"), 0, c)
        self.rows = []
        coms = list_ports()
        for i, p in enumerate(self.cfg):
            use = QtWidgets.QCheckBox()
            use.setChecked(p["on"])
            name = QtWidgets.QLineEdit(p["name"])
            name.setMaximumWidth(70)
            name.setStyleSheet(f"color:{PORT_COLORS[i]};")
            com = QtWidgets.QComboBox()
            com.setEditable(True)
            com.setMinimumWidth(90)
            for dev, desc in coms:
                com.addItem(dev)
                com.setItemData(com.count() - 1, desc, QtCore.Qt.ToolTipRole)
            com.setEditText(p["com"])
            baud = QtWidgets.QComboBox()
            baud.setEditable(True)
            baud.addItems([str(b) for b in BAUDS])
            baud.setEditText(str(p["baud"]))
            bits = QtWidgets.QComboBox()
            bits.addItems(["8", "7", "6", "5"])
            bits.setCurrentText(str(p["bits"]))
            par = QtWidgets.QComboBox()
            for lbl, k in PARITIES:
                par.addItem(lbl, k)
            par.setCurrentIndex(max(0, par.findData(p["parity"])))
            stop = QtWidgets.QComboBox()
            stop.addItems(["1", "1.5", "2"])
            stop.setCurrentText(f"{p['stop']:g}")
            for c, w in enumerate((use, name, com, baud, bits, par, stop)):
                grid.addWidget(w, i + 1, c)
            self.rows.append((use, name, com, baud, bits, par, stop))
        grid.setColumnStretch(2, 1)
        bar = QtWidgets.QHBoxLayout()
        self.refresh_btn = QtWidgets.QPushButton("⟳ COM ports")
        self.refresh_btn.setToolTip("List COM ports again (MOXA ports first)")
        self.refresh_btn.clicked.connect(self._refresh_coms)
        self.gap = QtWidgets.QSpinBox()
        self.gap.setRange(1, 1000)
        self.gap.setValue(self.gap_ms)
        self.gap.setPrefix("Frame gap ")
        self.gap.setSuffix(" ms")
        self.gap.setToolTip("A pause longer than this between received bytes ends a frame")
        self.conn_btn = QtWidgets.QPushButton("Connect")
        self.conn_btn.setMinimumWidth(110)
        self.conn_btn.clicked.connect(self.toggle_connect)
        bar.addWidget(self.refresh_btn)
        bar.addWidget(self.gap)
        bar.addStretch(1)
        bar.addWidget(self.conn_btn)
        grid.addLayout(bar, N_PORTS + 1, 0, 1, 7)
        self.status = QtWidgets.QLabel("● not connected")
        self.status.setStyleSheet("color:#8a96a3;")
        self.status.setWordWrap(True)
        grid.addWidget(self.status, N_PORTS + 2, 0, 1, 7)
        lv.addWidget(g)

        g = QtWidgets.QGroupBox("Transmit")
        f = QtWidgets.QFormLayout(g)
        self.tx_port = QtWidgets.QComboBox()
        self.tx_data = QtWidgets.QLineEdit()
        self.tx_data.setPlaceholderText("hex bytes, e.g. AA 55 01 00")
        self.tx_data.returnPressed.connect(self.on_send)
        row = QtWidgets.QHBoxLayout()
        send = QtWidgets.QPushButton("Send")
        send.clicked.connect(self.on_send)
        self.per_chk = QtWidgets.QCheckBox("every")
        self.per_ms = QtWidgets.QSpinBox()
        self.per_ms.setRange(10, 3_600_000)
        self.per_ms.setValue(1000)
        self.per_ms.setSuffix(" ms")
        self.per_chk.toggled.connect(self._on_periodic)
        row.addWidget(send)
        row.addWidget(self.per_chk)
        row.addWidget(self.per_ms)
        row.addStretch(1)
        save = QtWidgets.QPushButton("→ Library")
        save.setToolTip("Add these bytes to the command library")
        save.clicked.connect(self.add_from_tx)
        row.addWidget(save)
        f.addRow("Port", self.tx_port)
        f.addRow("Data", self.tx_data)
        f.addRow(row)
        lv.addWidget(g)

        g = QtWidgets.QGroupBox("Command library")
        cv = QtWidgets.QVBoxLayout(g)
        self.cmd_list = QtWidgets.QListWidget()
        self.cmd_list.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.cmd_list.itemDoubleClicked.connect(lambda *_: self.send_selected())
        self.cmd_list.setToolTip("Double-click to send. The FRM command layout will be added later.")
        cv.addWidget(self.cmd_list, 1)
        cb = QtWidgets.QHBoxLayout()
        for text, fn in (("Send", self.send_selected), ("New", self.new_cmd), ("Edit", self.edit_cmd),
                         ("Delete", self.delete_cmd)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            cb.addWidget(b)
        cv.addLayout(cb)
        lv.addWidget(g, 1)
        left.setMaximumWidth(620)
        root.addWidget(left)

        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        tb = QtWidgets.QHBoxLayout()
        self.show_chk = {}
        tb.addWidget(QtWidgets.QLabel("Show:"))
        for i in range(N_PORTS):
            c = QtWidgets.QCheckBox(self.cfg[i]["name"])
            c.setChecked(True)
            c.setStyleSheet(f"color:{PORT_COLORS[i]};")
            tb.addWidget(c)
            self.show_chk[i] = c
        self.show_tx = QtWidgets.QCheckBox("TX")
        self.show_tx.setChecked(True)
        tb.addWidget(self.show_tx)
        self.pause_btn = QtWidgets.QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.follow = QtWidgets.QCheckBox("Follow")
        self.follow.setChecked(True)
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(lambda: self.model.clear())
        self.rec_btn = QtWidgets.QPushButton("● Record")
        self.rec_btn.setCheckable(True)
        self.rec_btn.setToolTip("Write every frame to rs485_log_YYYY-MM-DD_HHMMSS.log next to the program")
        self.rec_btn.toggled.connect(self._on_record)
        tb.addStretch(1)
        for w in (self.follow, self.pause_btn, clear, self.rec_btn):
            tb.addWidget(w)
        rv.addLayout(tb)
        self.model = TraceModel(self._colors())
        self.view = QtWidgets.QTableView()
        self.view.setModel(self.model)
        self.view.verticalHeader().hide()
        self.view.verticalHeader().setDefaultSectionSize(18)
        self.view.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        for c, w in enumerate((95, 50, 32, 40, 520)):
            self.view.setColumnWidth(c, w)
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.doubleClicked.connect(self._trace_to_tx)
        self.view.setToolTip("Double-click a frame to copy it to Transmit")
        rv.addWidget(self.view, 1)
        self.counts = QtWidgets.QLabel("")
        self.counts.setStyleSheet("color:#8a96a3;")
        rv.addWidget(self.counts)
        root.addWidget(right, 1)
        self._fill_tx_ports()
        self._fill_cmds()

    # ---------------------------------------------------------------- helpers
    def _colors(self):
        return {self.cfg[i]["name"]: PORT_COLORS[i] for i in range(N_PORTS)}

    def _refresh_coms(self):
        coms = list_ports()
        for row in self.rows:
            com = row[2]
            cur = com.currentText()
            com.clear()
            for dev, desc in coms:
                com.addItem(dev)
                com.setItemData(com.count() - 1, desc, QtCore.Qt.ToolTipRole)
            com.setEditText(cur)
        self.log.emit("COM ports: " + (", ".join(d for _, d in coms) or "none found"), "info")

    def _read_rows(self):
        names = set()
        for i, (use, name, com, baud, bits, par, stop) in enumerate(self.rows):
            n = name.text().strip() or f"P{i + 1}"
            if n in names:
                raise ValueError(f"port name '{n}' is used twice")
            names.add(n)
            self.cfg[i] = {"name": n, "com": com.currentText().strip(), "baud": int(baud.currentText()),
                           "bits": int(bits.currentText()), "parity": par.currentData(),
                           "stop": float(stop.currentText()) if stop.currentText() == "1.5" else int(stop.currentText()),
                           "on": use.isChecked()}
        self.gap_ms = self.gap.value()

    def _save_ports(self):
        if self.demo:
            return                                  # demo never overwrites the real setup
        try:
            with open(self.ports_file, "w", encoding="utf-8") as f:
                json.dump({"ports": self.cfg, "gap_ms": self.gap_ms}, f, indent=1)
        except OSError as e:
            self.log.emit(f"cannot save {self.ports_file}: {e}", "err")

    def _fill_tx_ports(self):
        cur = self.tx_port.currentText()
        self.tx_port.clear()
        self.tx_port.addItems([p["name"] for p in self.cfg if p["on"]] or [self.cfg[0]["name"]])
        if cur:
            self.tx_port.setCurrentText(cur)
        for i, c in self.show_chk.items():
            c.setText(self.cfg[i]["name"])
        self.model.colors = self._colors()

    def _fill_cmds(self):
        self.cmd_list.clear()
        for c in self.store.commands:
            it = QtWidgets.QListWidgetItem(f"{c.label():<24} {c.data_str()}")
            it.setData(QtCore.Qt.UserRole, c.uid)
            self.cmd_list.addItem(it)

    def _save_cmds(self):
        try:
            self.store.save()
        except OSError as e:
            self.log.emit(f"cannot save {self.store.path}: {e}", "err")
        self._fill_cmds()

    def _set_editable(self, on):
        for row in self.rows:
            for w in row:
                w.setEnabled(on)
        self.gap.setEnabled(on)
        self.refresh_btn.setEnabled(on)

    # ------------------------------------------------------------ connection
    def demo_setup(self):
        """Simulated ports: FRM on port 1."""
        self.demo = True
        self.rows[0][0].setChecked(True)
        self.rows[0][1].setText("FRM")

    def connected_ports(self) -> list[str]:
        return list(self.workers)

    def connect_all(self):
        if not self.workers:
            self.toggle_connect()

    def toggle_connect(self):
        if self.workers:
            self.disconnect_all()
            return
        try:
            self._read_rows()
        except ValueError as e:
            self.log.emit(f"RS-485: {e}", "err")
            return
        self._save_ports()
        self._fill_tx_ports()
        ok, bad = [], []
        for p in self.cfg:
            if not p["on"]:
                continue
            try:
                if self.demo:
                    dev = DemoPort(p)
                else:
                    if not p["com"]:
                        raise ValueError("no COM port selected")
                    dev = SerialPort(p)
            except Exception as e:
                bad.append(f"{p['name']}: {type(e).__name__}: {e}")
                continue
            w = PortWorker(p["name"], dev, self.gap_ms / 1000)
            w.frame.connect(self._on_frame)
            w.error.connect(self._on_port_error)
            w.start()
            self.workers[p["name"]] = w
            ok.append(f"{p['name']} = {dev.info}")
        for b in bad:
            self.log.emit(f"RS-485 {b}", "err")
        if ok:
            self.log.emit("RS-485 connected: " + ", ".join(ok), "ok")
        self._update_status(bad)

    def disconnect_all(self):
        self.per_chk.setChecked(False)
        for w in self.workers.values():
            w.stop()
        if self.workers:
            self.log.emit("RS-485 disconnected", "info")
        self.workers = {}
        self._update_status()

    def _update_status(self, bad=()):
        up = self.connected_ports()
        self.conn_btn.setText("Disconnect" if up else "Connect")
        self._set_editable(not up)
        if up:
            self.status.setText("● " + ", ".join(up) + (" (demo)" if self.demo else "")
                                + ("   ✗ " + "; ".join(bad) if bad else ""))
            self.status.setStyleSheet("color:#4fd18b;")
        else:
            self.status.setText("● not connected" + ("   ✗ " + "; ".join(bad) if bad else ""))
            self.status.setStyleSheet(f"color:{'#ff6b6b' if bad else '#8a96a3'};")

    def _on_port_error(self, name, msg):
        self.log.emit(f"RS-485 {name}: {msg} - port closed", "err")
        w = self.workers.pop(name, None)
        if w:
            w.wait(1000)
        self._update_status([f"{name}: {msg}"])

    # -------------------------------------------------------------- transmit
    def send(self, port: str, data: bytes) -> bool:
        w = self.workers.get(port)
        if w is None or not data:
            return False
        w.send(data)
        return True

    def send_command(self, cmd: Command) -> bool:
        return self.send(cmd.port or self.tx_port.currentText(), cmd.data)

    def on_send(self):
        try:
            data = parse_hex(self.tx_data.text())
        except ValueError as e:
            self.log.emit(f"RS-485 transmit: {e}", "err")
            self.per_chk.setChecked(False)
            return
        if not self.send(self.tx_port.currentText(), data):
            self.log.emit(f"RS-485 transmit: port {self.tx_port.currentText()} not connected", "err")
            self.per_chk.setChecked(False)

    def _on_periodic(self, on):
        if on:
            self.periodic.start(self.per_ms.value())
        else:
            self.periodic.stop()

    def _trace_to_tx(self, idx):
        f = self.model.rows[idx.row()]
        self.tx_data.setText(hex_str(f.data))
        self.tx_port.setCurrentText(f.port)

    # --------------------------------------------------------------- library
    def _sel_cmd(self) -> Command | None:
        it = self.cmd_list.currentItem()
        return self.store.by_uid(it.data(QtCore.Qt.UserRole)) if it else None

    def send_selected(self):
        c = self._sel_cmd()
        if c and not self.send_command(c):
            self.log.emit(f"RS-485: cannot send {c.label()} - port not connected", "err")

    def _ports(self):
        return [p["name"] for p in self.cfg]

    def new_cmd(self, data=b"", port=""):
        c = Command(self.store.next_key(), "", data, port)
        dlg = CommandDialog(self, c, {x.key for x in self.store.commands}, self._ports())
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            self.store.commands.append(c)
            self._save_cmds()

    def add_from_tx(self):
        try:
            data = parse_hex(self.tx_data.text())
        except ValueError:
            data = b""
        self.new_cmd(data, self.tx_port.currentText())

    def edit_cmd(self):
        c = self._sel_cmd()
        if c is None:
            return
        dlg = CommandDialog(self, c, {x.key for x in self.store.commands if x is not c}, self._ports())
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            self._save_cmds()

    def delete_cmd(self):
        c = self._sel_cmd()
        if c and QtWidgets.QMessageBox.question(self, "Delete", f"Delete command {c.label()}?") \
                == QtWidgets.QMessageBox.Yes:
            self.store.commands.remove(c)
            self._save_cmds()

    # ------------------------------------------------------------------ data
    def _on_frame(self, f: Frame):
        if f.dir == "RX":
            self.n_rx += 1
        else:
            self.n_tx += 1
        self.pending.append(f)
        if self.rec:
            try:
                self.rec.write(f.log_line() + "\n")
            except OSError as e:
                self.log.emit(f"RS-485 recording stopped: {e}", "err")
                self.rec_btn.setChecked(False)

    def _flush(self):
        if not self.pending:
            return
        frames, self.pending = self.pending, []
        for cb in self.frame_listeners:
            try:
                cb(frames)
            except Exception as e:
                self.log.emit(f"RS-485 listener: {type(e).__name__}: {e}", "err")
        if not self.pause_btn.isChecked():
            shown = {self.cfg[i]["name"] for i, c in self.show_chk.items() if c.isChecked()}
            self.model.add([f for f in frames if f.port in shown and (f.dir == "RX" or self.show_tx.isChecked())])
            if self.follow.isChecked():
                self.view.scrollToBottom()
        self.counts.setText(f"RX {self.n_rx:,} frames   TX {self.n_tx:,} frames   shown {len(self.model.rows):,}")

    def _on_record(self, on):
        if on:
            path = os.path.join(script_dir(), time.strftime("rs485_log_%Y-%m-%d_%H%M%S.log"))
            try:
                self.rec = open(path, "a", encoding="utf-8")
            except OSError as e:
                self.log.emit(f"RS-485: cannot record: {e}", "err")
                self.rec_btn.setChecked(False)
                return
            self.rec_btn.setText("■ Stop recording")
            self.log.emit(f"RS-485 recording to {path}", "ok")
        else:
            if self.rec:
                self.rec.close()
                self.rec = None
            self.rec_btn.setText("● Record")

    def shutdown(self):
        self.flush.stop()
        self.periodic.stop()
        self.disconnect_all()
        if self.rec:
            self.rec.close()
            self.rec = None


def main():
    import argparse
    ap = argparse.ArgumentParser(description="RS-485 monitor (MOXA UPort 1450I)")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    w = QtWidgets.QMainWindow()
    tab = Rs485Tab()
    tab.log.connect(lambda t, l: print(f"[{l}] {t}"))
    if args.demo:
        tab.demo_setup()
    w.setCentralWidget(tab)
    w.setWindowTitle("RS-485 monitor")
    w.resize(1400, 800)
    w.show()
    app.aboutToQuit.connect(tab.shutdown)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
