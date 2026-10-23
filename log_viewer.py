"""
Logs tab: load any of the program's CSV logs and overlay them on one time plot.

Understood formats (detected from the header, any number of files and days):
* voltage_log_YYYY-MM-DD*.csv   Time, Channel, Voltage  (old 4-column files too)
* hmp_log_YYYY-MM-DD.csv        Time, Master, CHn_On, CHn_Vset_V, ... per channel
* switch_log_YYYY-MM-DD.csv      Time, Output, Pin, Name, Action, State, Reason  (0/1 steps)
* temperature_log_YYYY-MM-DD.csv Time, ROM, Name, Temperature_C, ...  (1-Wire sensors)
* telemetry_log_YYYY-MM-DD.csv  Time, Unit, Array, Designation, Byte, Value, Meaning, Warning
* any other CSV whose first column is a time: every numeric column becomes a series

The date comes from the file name (YYYY-MM-DD), the time of day from the first column.
Series with the same name from several files (e.g. several days) are joined into one.
"""
from __future__ import annotations

import csv
import datetime as _dt
import os
import re
import sys

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

PALETTE = ["#58a6ff", "#f0b03e", "#4fd18b", "#ff6b6b", "#d2a8ff", "#79c0ff", "#ffa657",
           "#7ee787", "#f778ba", "#a5d6ff", "#e3b341", "#56d4dd", "#bc8cff", "#ff7b72"]
GROUPS = ("Voltage", "HMP4040", "Temperature", "Switch", "Telemetry", "Other")

_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _channel_names(folder: str) -> dict[int, str]:
    names = {}
    for d in (folder, _app_dir()):
        try:
            with open(os.path.join(d, "channels.txt"), encoding="utf-8") as f:
                for line in f:
                    k, _, v = line.strip().partition("=")
                    if k.strip().isdigit() and v.strip():
                        names.setdefault(int(k), v.strip())
        except OSError:
            pass
    return names


def _time_parser(path: str):
    """Returns f(text) -> unix time, using the date in the file name (else today)."""
    m = _DATE.search(os.path.basename(path))
    day = _dt.date.fromisoformat(m.group(1)) if m else _dt.date.today()
    base = _dt.datetime.combine(day, _dt.time()).timestamp()

    def parse(text: str) -> float | None:
        text = text.strip()
        if len(text) >= 19 and text[4] == "-":          # full "YYYY-MM-DD HH:MM:SS"
            try:
                return _dt.datetime.fromisoformat(text.replace("T", " ")).timestamp()
            except ValueError:
                return None
        parts = text.split(":")
        if len(parts) != 3:
            return None
        try:
            return base + int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        except ValueError:
            return None
    return parse


def _telemetry_value(value: str, meaning: str) -> tuple[float, str]:
    """(number, unit). Physical value from Meaning when it starts with a number, else raw value."""
    m = re.match(r"\s*([-+]?\d+(?:\.\d+)?)\s*(°C|°|[A-Za-zµ%/]+)?\s*$", meaning or "")
    if m:
        return float(m.group(1)), (m.group(2) or "")
    v = value.strip()
    p = re.search(r"\((-?\d+)\)", v)
    try:
        if p:
            return float(p.group(1)), ""
        if v.lower().startswith("0b"):
            return float(int(v[2:], 2)), ""
        if v.lower().startswith("0x") and " " not in v:
            return float(int(v, 16)), ""
        return float(v), ""
    except ValueError:
        return float("nan"), ""


def parse_log(path: str) -> dict[tuple[str, str], tuple[list, list]]:
    """{(group, series name): ([t...], [y...])}"""
    out: dict[tuple[str, str], tuple[list, list]] = {}
    tp = _time_parser(path)

    def add(group, name, t, y):
        ts, ys = out.setdefault((group, name), ([], []))
        ts.append(t)
        ys.append(y)

    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        rows = csv.reader(f)
        head = next(rows, None)
        if not head:
            return out
        h = [c.strip() for c in head]
        base = os.path.basename(path).lower()

        if h[:3] == ["Time", "Channel", "Voltage"] or (base.startswith("voltage_log") and len(h) == 4):
            names = _channel_names(os.path.dirname(path))
            for row in rows:
                if len(row) < 3 or not row[1].strip().isdigit():
                    continue
                t = tp(row[0])
                if t is None:
                    continue
                ch = int(row[1])
                try:
                    add("Voltage", f"Ch {ch:3d}  {names.get(ch, '')}".rstrip(), t, float(row[-1]))
                except ValueError:
                    pass
        elif h[:2] == ["Time", "Master"]:
            cols = [(k, c) for k, c in enumerate(h) if k > 0]
            for row in rows:
                t = tp(row[0]) if row else None
                if t is None:
                    continue
                for k, c in cols:
                    try:
                        y = float(row[k])
                    except (ValueError, IndexError):
                        continue
                    label = c.replace("_V_V", " V").replace("_I_A", " I (A)").replace("_P_W", " P (W)") \
                             .replace("_Vset_V", " Vset").replace("_Iset_A", " Iset").replace("_On", " On")
                    add("HMP4040", label, t, y)
        elif h[:4] == ["Time", "Output", "Pin", "Name"]:          # switch_log: one row per action
            ia, ist = h.index("Action"), h.index("State")
            for row in rows:
                if len(row) <= ist:
                    continue
                t = tp(row[0])
                if t is None:
                    continue
                name = f"{row[2]} {row[3]}".strip()
                m = re.match(r"pulse (\d+) ms", row[ia])
                try:
                    st = float(row[ist])
                except ValueError:
                    continue
                add("Switch", name, t - 1e-3, 0.0 if m or st else 1.0)   # edge, so steps show the change
                add("Switch", name, t, st)
                if m:                                                    # the slave ends the pulse itself
                    add("Switch", name, t + int(m.group(1)) / 1000, 0.0)
        elif h[:4] == ["Time", "ROM", "Name", "Temperature_C"]:
            last_name, pts = {}, []
            for row in rows:
                if len(row) < 4 or not row[3].strip():
                    continue
                t = tp(row[0])
                if t is None:
                    continue
                try:
                    pts.append((row[1], t, float(row[3])))
                except ValueError:
                    continue
                if row[2].strip():
                    last_name[row[1]] = row[2].strip()
            for rom, t, y in pts:                  # one series per sensor, named by its latest name
                add("Temperature", f"{last_name.get(rom, rom)} [{rom[-4:]}] (°C)", t, y)
        elif h[:4] == ["Time", "Unit", "Array", "Designation"]:
            iv, im = h.index("Value"), h.index("Meaning")
            for row in rows:
                if len(row) <= im:
                    continue
                t = tp(row[0])
                if t is None:
                    continue
                y, unit = _telemetry_value(row[iv], row[im])
                name = f"{row[1]} {row[2]} · {row[3]}" + (f" ({unit})" if unit else "")
                add("Telemetry", name, t, y)
        else:
            label = os.path.splitext(os.path.basename(path))[0]
            for row in rows:
                t = tp(row[0]) if row else None
                if t is None:
                    continue
                for k in range(1, min(len(row), len(h))):
                    m = _NUM.fullmatch(row[k].strip())
                    if m:
                        add("Other", f"{label}: {h[k]}", t, float(row[k]))
    return out


class LogsTab(QtWidgets.QWidget):
    def __init__(self, folder_fn=None, parent=None):
        super().__init__(parent)
        self.folder_fn = folder_fn or _app_dir
        self.files: list[str] = []
        self.data: dict[tuple[str, str], tuple[np.ndarray, np.ndarray]] = {}
        self.curves: dict[tuple[str, str], pg.PlotDataItem] = {}
        self._color_i = 0
        self._build()

    # ------------------------------------------------------------------ UI
    def _build(self):
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        root.addWidget(split)

        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        row = QtWidgets.QHBoxLayout()
        add_b = QtWidgets.QPushButton("Add CSV files…")
        add_b.clicked.connect(self.on_add)
        today_b = QtWidgets.QPushButton("Today's logs")
        today_b.setToolTip("Load voltage, HMP4040, temperature, switch and telemetry logs of today from the log folders")
        today_b.clicked.connect(self.on_today)
        clr_b = QtWidgets.QPushButton("Clear")
        clr_b.clicked.connect(self.on_clear)
        for b in (add_b, today_b, clr_b):
            row.addWidget(b)
        lv.addLayout(row)
        self.file_list = QtWidgets.QListWidget()
        self.file_list.setMaximumHeight(110)
        self.file_list.setToolTip("Loaded files")
        lv.addWidget(self.file_list)

        self.filter = QtWidgets.QLineEdit()
        self.filter.setPlaceholderText("Filter series…  (e.g. CH1, Ch  12, BOS)")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        lv.addWidget(self.filter)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Series", "Points"])
        self.tree.setColumnWidth(0, 250)
        self.tree.itemChanged.connect(self._on_item_changed)
        lv.addWidget(self.tree, 1)
        row = QtWidgets.QHBoxLayout()
        all_b = QtWidgets.QPushButton("Plot filtered")
        all_b.setToolTip("Tick every series that matches the filter")
        all_b.clicked.connect(lambda: self._check_visible(True))
        none_b = QtWidgets.QPushButton("Hide all")
        none_b.clicked.connect(lambda: self._check_visible(False, everything=True))
        self.steps = QtWidgets.QCheckBox("Steps")
        self.steps.setToolTip("Draw values as steps (hold until the next sample) - useful for telemetry")
        self.steps.setChecked(True)
        self.steps.toggled.connect(self._redraw_all)
        self.dots = QtWidgets.QCheckBox("Points")
        self.dots.toggled.connect(self._redraw_all)
        for w in (all_b, none_b, self.steps, self.dots):
            row.addWidget(w)
        lv.addLayout(row)
        split.addWidget(left)

        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setClipToView(True)
        self.legend = self.plot.addLegend(offset=(10, 10))
        self.plot.getPlotItem().setDownsampling(auto=True, mode="peak")
        rv.addWidget(self.plot, 1)
        bot = QtWidgets.QHBoxLayout()
        self.readout = QtWidgets.QLabel("Load CSV logs to plot them.  Mouse wheel = zoom, "
                                        "right-drag = zoom one axis, right-click = menu")
        self.readout.setStyleSheet("color:#8a96a3;")
        fit_b = QtWidgets.QPushButton("Fit")
        fit_b.clicked.connect(lambda: self.plot.enableAutoRange())
        bot.addWidget(self.readout, 1)
        bot.addWidget(fit_b)
        rv.addLayout(bot)
        split.addWidget(right)
        split.setSizes([360, 1000])

        self.vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#8a96a3", style=QtCore.Qt.DashLine))
        self.plot.addItem(self.vline, ignoreBounds=True)
        self.plot.scene().sigMouseMoved.connect(self._on_mouse)

    # --------------------------------------------------------------- loading
    def _folders(self) -> list[str]:
        out = []
        for d in (self.folder_fn(), _app_dir()):
            if d and d not in out:
                out.append(d)
        return out

    def on_add(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Load CSV logs", self._folders()[0],
                                                          "CSV logs (*.csv);;All files (*)")
        self.load(paths)

    def on_today(self):
        day = _dt.date.today().isoformat()
        paths = []
        for d in self._folders():
            try:
                for n in sorted(os.listdir(d)):
                    if n.endswith(".csv") and day in n and n.split("_log")[0] in ("voltage", "hmp", "telemetry", "temperature", "switch"):
                        paths.append(os.path.join(d, n))
            except OSError:
                pass
        if not paths:
            self.readout.setText(f"No logs for {day} in {', '.join(self._folders())}")
        self.load(paths)

    def load(self, paths):
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            for p in paths:
                p = os.path.abspath(p)
                if p in self.files:
                    continue
                try:
                    part = parse_log(p)
                except Exception as e:
                    self.readout.setText(f"Could not read {os.path.basename(p)}: {type(e).__name__}: {e}")
                    continue
                self.files.append(p)
                n = sum(len(t) for t, _ in part.values())
                self.file_list.addItem(f"{os.path.basename(p)}   ({len(part)} series, {n:,} points)")
                for key, (t, y) in part.items():
                    t, y = np.asarray(t, float), np.asarray(y, float)
                    if key in self.data:
                        t = np.concatenate((self.data[key][0], t))
                        y = np.concatenate((self.data[key][1], y))
                    order = np.argsort(t, kind="stable")
                    self.data[key] = (t[order], y[order])
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self._rebuild_tree()
        for key in self.curves:
            self._draw(key)
        self.plot.enableAutoRange()

    def on_clear(self):
        for c in self.curves.values():
            self.plot.removeItem(c)
        self.curves.clear()
        self.legend.clear()
        self.files.clear()
        self.data.clear()
        self.file_list.clear()
        self.tree.clear()
        self._color_i = 0

    # ----------------------------------------------------------------- tree
    def _rebuild_tree(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        tops = {}
        for g in GROUPS:
            keys = sorted(k for k in self.data if k[0] == g)
            if not keys:
                continue
            top = QtWidgets.QTreeWidgetItem([f"{g}  ({len(keys)})", ""])
            top.setFlags(top.flags() & ~QtCore.Qt.ItemIsUserCheckable)
            self.tree.addTopLevelItem(top)
            tops[g] = top
            for k in keys:
                it = QtWidgets.QTreeWidgetItem([k[1], f"{len(self.data[k][0]):,}"])
                it.setData(0, QtCore.Qt.UserRole, k)
                it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
                it.setCheckState(0, QtCore.Qt.Checked if k in self.curves else QtCore.Qt.Unchecked)
                if k in self.curves:
                    it.setForeground(0, QtGui.QColor(self.curves[k].opts["pen"].color()))
                top.addChild(it)
            top.setExpanded(True)
        self.tree.blockSignals(False)
        self._apply_filter()

    def _items(self):
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                yield top.child(j)

    def _apply_filter(self, *_):
        words = self.filter.text().lower().split()
        for it in self._items():
            text = f"{it.parent().text(0)} {it.text(0)}".lower()
            it.setHidden(not all(w in text for w in words))

    def _check_visible(self, on: bool, everything=False):
        shown = [it for it in self._items() if everything or not it.isHidden()]
        if on and len(shown) > 40:
            if QtWidgets.QMessageBox.question(self, "Many series", f"Plot {len(shown)} series?") \
                    != QtWidgets.QMessageBox.Yes:
                return
        for it in shown:
            it.setCheckState(0, QtCore.Qt.Checked if on else QtCore.Qt.Unchecked)

    def _on_item_changed(self, it, col):
        key = it.data(0, QtCore.Qt.UserRole)
        if key is None or col != 0:
            return
        on = it.checkState(0) == QtCore.Qt.Checked
        if on and key not in self.curves:
            color = PALETTE[self._color_i % len(PALETTE)]
            self._color_i += 1
            c = self.plot.plot([], [], name=key[1], pen=pg.mkPen(color, width=2))
            self.curves[key] = c
            self._draw(key)
            self.tree.blockSignals(True)
            it.setForeground(0, QtGui.QColor(color))
            self.tree.blockSignals(False)
        elif not on and key in self.curves:
            c = self.curves.pop(key)
            self.legend.removeItem(c)
            self.plot.removeItem(c)
            self.tree.blockSignals(True)
            it.setForeground(0, QtGui.QBrush())
            self.tree.blockSignals(False)

    # ----------------------------------------------------------------- plot
    def _draw(self, key):
        t, y = self.data[key]
        c = self.curves[key]
        if self.steps.isChecked() and len(t) > 1:
            t2 = np.repeat(t, 2)[1:]
            y2 = np.repeat(y, 2)[:-1]
            c.setData(t2, y2, connect="finite")
        else:
            c.setData(t, y, connect="finite")
        color = c.opts["pen"].color()
        if self.dots.isChecked():
            c.setSymbol("o")
            c.setSymbolSize(4)
            c.setSymbolBrush(color)
            c.setSymbolPen(None)
        else:
            c.setSymbol(None)

    def _redraw_all(self, *_):
        for key in self.curves:
            self._draw(key)

    def _on_mouse(self, pos):
        vb = self.plot.getPlotItem().vb
        if not self.plot.sceneBoundingRect().contains(pos) or not self.curves:
            return
        x = vb.mapSceneToView(pos).x()
        self.vline.setPos(x)
        parts = [_dt.datetime.fromtimestamp(x).strftime("%Y-%m-%d %H:%M:%S")]
        for key in list(self.curves)[:8]:
            t, y = self.data[key]
            i = np.searchsorted(t, x, side="right") - 1      # last value at or before the cursor
            if 0 <= i < len(t):
                parts.append(f"{key[1]}: {y[i]:.4g}")
        if len(self.curves) > 8:
            parts.append("…")
        self.readout.setText("   |   ".join(parts))
