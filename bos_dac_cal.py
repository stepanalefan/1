# -*- coding: utf-8 -*-
"""
bos_dac_cal.py
==============
DAC38J82 QMC calibration of the BOS from FSW measurements (ПИЛВ 0102-004 v2: Change_DAC_1 0x50, table 4.43, and
the DAC_offsets sets of MD_SETTINGS, table 4.7).

Registers (DAC38J82): qmc_offseta / qmc_offsetb (13-bit two's complement, added to the A / B data - cancel the LO
leakage / carrier offset), qmc_gaina / qmc_gainb (11-bit, gain = code / 1024 - I/Q amplitude balance), qmc_phaseab
(12-bit two's complement - quadrature), coarse_dac (full-scale current, not changed here).

The station measures, on the demodulated OQPSK / BPSK symbols (bos_analysis.iq_impairments): the complex carrier
offset c (relative to the RMS symbol), the Q / I gain imbalance g (dB) and the quadrature error q (deg).
Between DAC and FSW are the upconverter, the LO multiplier and the demodulator, which locks to one of four
orientations on every capture: the DIRECTION of c and the signs of g and q change from capture to capture, only
|c|, |g|, |q| are reliable. The calibration therefore uses rotation-invariant quantities, like LO-leakage nulling
with a spectrum analyser:

* |c|² is a quadratic surface over (offset A, offset B): measured at the working point and at 5 probe points
  (A ±s, B ±s, A+B +s) it is fitted exactly and its minimum solved; g² over (gain B - gain A) and q² over
  phaseab are parabolas from 3 points each;
* the solution is applied and measured; if not within the targets the next round uses half the probe steps;
* the curvatures give the unit's register scales (LSB per unit offset / dB / degree): kept per unit serial and used
  for the guidance from a single measurement (how far each register is off - the direction needs the probes).
"""

from __future__ import annotations

import copy
import math
import time

import numpy as np
from PyQt5 import QtGui, QtWidgets

REGS = ["qmc_offseta", "qmc_offsetb", "qmc_gaina", "qmc_gainb", "qmc_phaseab", "coarse_dac"]
DOC_DEFAULT = {"qmc_offseta": 0x1F84, "qmc_offsetb": 0x1E36, "qmc_gaina": 0x3F4, "qmc_gainb": 0x3F4,
               "qmc_phaseab": 0xFF6, "coarse_dac": 7}          # table 4.43 defaults (raw register values)
BITS = {"qmc_offseta": (13, True), "qmc_offsetb": (13, True), "qmc_gaina": (11, False), "qmc_gainb": (11, False),
        "qmc_phaseab": (12, True), "coarse_dac": (4, False)}
NOMINAL = {  # register scales before a unit has been calibrated (DAC38J82 data-sheet values)
    "off_lsb": 8192.0,                              # offset LSB for |c| = 1 (RMS signal = 8192 LSB)
    "gain_db_lsb": 20 / math.log(10) / 1012,        # |g| dB per LSB of (gainB - gainA), around 0x3F4
    "phase_deg_lsb": math.degrees(1 / 4096),        # |q| degrees per phaseab LSB
    "learned": "",
}
TARGET = {"off_dbc": -55.0, "gain_db": 0.02, "quad_deg": 0.1}
STEP = {"off": 160, "gain": 16, "phase": 64}         # first-round probe steps (LSB); halved every round
STEP_MIN = {"off": 12, "gain": 2, "phase": 6}
OPTS = {"n_avg": 3, "skip": 1, "settle_s": 1.0, "nsym": 32000, "own_session": True}   # defaults of the panel


def to_signed(name, raw_v):
    n, sg = BITS[name]
    raw_v = int(raw_v) & ((1 << n) - 1)
    return raw_v - (1 << n) if sg and raw_v >> (n - 1) else raw_v


def to_raw(name, v):
    n, sg = BITS[name]
    lo, hi = (-(1 << (n - 1)), (1 << (n - 1)) - 1) if sg else (0, (1 << n) - 1)
    v = int(max(lo, min(hi, round(v))))
    return v & ((1 << n) - 1)


def signed(regs):
    return {k: to_signed(k, regs[k]) for k in REGS}


def raw(sregs):
    return {k: to_raw(k, sregs[k]) for k in REGS}


def measurement(r):
    """{c, g, q, mer, mer_corr, mod} from an analyze_modulated result; g / q are NaN for BPSK."""
    if not r or r.get("kind") != "mod":
        return None
    c = complex(r.get("offset_i", float("nan")), r.get("offset_q", float("nan")))
    if not (math.isfinite(c.real) and math.isfinite(c.imag)):
        return None
    return {"c": c, "g": r.get("gain_imb", float("nan")), "q": r.get("quad_err", float("nan")),
            "mer": r.get("mer", float("nan")), "mer_corr": r.get("mer_corr", float("nan")),
            "mod": r.get("expected", "")}


def dbc(c):
    return 20 * math.log10(max(abs(c), 1e-9))


def converged(m):
    ok = dbc(m["c"]) <= TARGET["off_dbc"]
    if math.isfinite(m["g"]):
        ok = ok and abs(m["g"]) <= TARGET["gain_db"]
    if math.isfinite(m["q"]):
        ok = ok and abs(m["q"]) <= TARGET["quad_deg"]
    return ok


def guidance(m, model):
    """How far each register group is from its optimum (LSB), from one measurement - magnitudes only."""
    out = {"off": abs(m["c"]) * model["off_lsb"]}
    if math.isfinite(m["g"]):
        out["gain"] = abs(m["g"]) / model["gain_db_lsb"]
        out["phase"] = abs(m["q"]) / model["phase_deg_lsb"]
    return out


def _vertex(y_m, y_0, y_p, s):
    """Minimum offset of the parabola through (-s, y_m), (0, y_0), (s, y_p) and its curvature (per LSB²);
    (None, k) when it is not convex."""
    curv = y_p + y_m - 2 * y_0
    if not curv > 0:
        return None, curv
    return s * (y_m - y_p) / (2 * curv), curv / (s * s)


class AutoCal:
    """Probe / fit / apply rounds driven by successive measurements (live diagnose)."""

    def __init__(self, regs_raw, model, max_rounds=4, n_avg=3, skip=1):
        self.n_avg, self.skip = max(1, int(n_avg)), max(0, int(skip))
        self.buf, self.to_skip = [], self.skip      # captures of the current setting / still to discard (the working point was just sent)
        self.center = signed(regs_raw)             # working point (signed register values)
        self.model = copy.deepcopy(model)
        self.max_rounds = max_rounds
        self.round = 0
        self.step = dict(STEP)
        self.log = []
        self.done = self.ok = False
        self.y = {}
        self.plan = []
        self.cur = None                            # None = measuring the working point
        self.regs = dict(regs_raw)
        self.bpsk = None
        self.best = None                           # (|c|-based score, signed regs)

    @staticmethod
    def _y(m):
        return {"P": abs(m["c"]) ** 2, "G": m["g"] ** 2 if math.isfinite(m["g"]) else float("nan"),
                "Q": m["q"] ** 2 if math.isfinite(m["q"]) else float("nan")}

    def _point(self, d):
        s = dict(self.center)
        s["qmc_offseta"] += d.get("a", 0)
        s["qmc_offsetb"] += d.get("b", 0)
        s["qmc_gaina"] -= d.get("g", 0) / 2
        s["qmc_gainb"] += d.get("g", 0) / 2
        s["qmc_phaseab"] += d.get("p", 0)
        return raw(s)

    def _start_round(self, m):
        self.round += 1
        so, sg, sp = self.step["off"], self.step["gain"], self.step["phase"]
        self.y = {"0": self._y(m)}
        self.plan = [("a+", {"a": so}), ("a-", {"a": -so}), ("b+", {"b": so}), ("b-", {"b": -so}),
                     ("ab", {"a": so, "b": so})]
        if not self.bpsk:
            self.plan += [("g+", {"g": sg}), ("g-", {"g": -sg}), ("p+", {"p": sp}), ("p-", {"p": -sp})]
        self.log.append(f"round {self.round}: probes ±{so} LSB offset" +
                        ("" if self.bpsk else f", ±{sg} LSB gain, ±{sp} LSB phase") + f" ({len(self.plan)} captures)")
        return self._next()

    def _next(self):
        name, d = self.plan.pop(0)
        self.cur = name
        self.regs = self._point(d)
        return self.regs

    def _score(self, m):
        sc = abs(m["c"]) / 10 ** (TARGET["off_dbc"] / 20)
        if not self.bpsk:
            sc += abs(m["g"]) / TARGET["gain_db"] + abs(m["q"]) / TARGET["quad_deg"]
        return sc

    def feed(self, m):
        """One capture with self.regs. Returns the registers to send next, None when finished, or "wait" while
        captures of this setting are still being collected (after a change the first `skip` are discarded, then
        `n_avg` are averaged: |c|², g², q² - the quantities the fit uses)."""
        if self.to_skip > 0:
            self.to_skip -= 1
            self.log.append(f"  capture discarded (settling after the change)")
            return "wait"
        self.buf.append(m)
        if len(self.buf) < self.n_avg:
            return "wait"
        ms, self.buf = self.buf, []
        nxt = self._feed(self._average(ms))
        if isinstance(nxt, dict):
            self.to_skip = self.skip
        return nxt

    def _average(self, ms):
        P = [abs(x["c"]) ** 2 for x in ms]
        G = [x["g"] ** 2 for x in ms if math.isfinite(x["g"])]
        Q = [x["q"] ** 2 for x in ms if math.isfinite(x["q"])]
        avg = {"c": complex(math.sqrt(float(np.mean(P))), 0.0),
               "g": math.sqrt(float(np.mean(G))) if G else float("nan"),
               "q": math.sqrt(float(np.mean(Q))) if Q else float("nan"),
               "mer": float(np.mean([x["mer"] for x in ms])), "mer_corr": float(np.mean([x["mer_corr"] for x in ms])),
               "mod": ms[0]["mod"], "n": len(ms)}
        if len(ms) > 1:
            d = [dbc(x["c"]) for x in ms]
            avg["spread"] = f" (±{np.std(d):.1f} dB over {len(ms)} captures)"
        else:
            avg["spread"] = ""
        return avg

    def _feed(self, m):
        if self.bpsk is None:
            self.bpsk = not (math.isfinite(m["g"]) and math.isfinite(m["q"]))
        if self.cur is None:                        # working point (start or after a correction)
            self.log.append(f"{'start' if self.round == 0 else 'check'}: offset {dbc(m['c']):.1f} dBc"
                            f"{m.get('spread', '')}" +
                            ("" if self.bpsk else f", gain {abs(m['g']):.3f} dB, quad {abs(m['q']):.2f}°") +
                            f", MER {m['mer']:.1f} dB")
            sc = self._score(m)
            if self.best is None or sc < self.best[0]:
                self.best = (sc, dict(self.center))
            if converged(m):
                return self._finish(True, "within the targets" + (f" after {self.round} round(s)" if self.round else
                                                                  " - nothing changed"))
            if self.round >= self.max_rounds:
                self.center = dict(self.best[1])
                return self._finish(False, f"not within the targets after {self.round} rounds (noise / drift?) - "
                                           f"the best values measured are applied")
            return self._start_round(m)
        self.y[self.cur] = self._y(m)
        self.log.append(f"  {self.cur}: offset {dbc(m['c']):.1f} dBc{m.get('spread', '')}" +
                        ("" if self.bpsk else f", gain {abs(m['g']):.3f} dB, quad {abs(m['q']):.2f}°"))
        if self.plan:
            return self._next()
        return self._solve()

    def _solve(self):
        y, so = self.y, self.step["off"]
        P0 = y["0"]["P"]
        A1 = (y["a+"]["P"] - y["a-"]["P"]) / (2 * so)
        B1 = (y["b+"]["P"] - y["b-"]["P"]) / (2 * so)
        A2 = (y["a+"]["P"] + y["a-"]["P"] - 2 * P0) / (2 * so * so)
        B2 = (y["b+"]["P"] + y["b-"]["P"] - 2 * P0) / (2 * so * so)
        C = (y["ab"]["P"] - P0 - A1 * so - B1 * so - A2 * so * so - B2 * so * so) / (so * so)
        H = np.array([[2 * A2, C], [C, 2 * B2]])
        if A2 > 0 and B2 > 0 and np.linalg.det(H) > 0:
            d = np.linalg.solve(H, -np.array([A1, B1]))
        elif A2 > 0 and B2 > 0:
            d = np.array([-A1 / (2 * A2), -B1 / (2 * B2)])
        else:
            return self._finish(False, "the offset registers have no clear effect on the carrier offset - is the DAC "
                                       "running (session on) and is Change_DAC_1 accepted?")
        d = np.clip(d, -6 * so, 6 * so)
        self.model["off_lsb"] = 1 / math.sqrt((A2 + B2) / 2)           # |c| = 1 per off_lsb LSB
        new = dict(self.center)
        new["qmc_offseta"] += d[0]
        new["qmc_offsetb"] += d[1]
        txt = f"offset A {d[0]:+.0f}, B {d[1]:+.0f}"
        if not self.bpsk:
            sg, sp = self.step["gain"], self.step["phase"]
            dg, kg = _vertex(y["g-"]["G"], y["0"]["G"], y["g+"]["G"], sg)
            dp, kp = _vertex(y["p-"]["Q"], y["0"]["Q"], y["p+"]["Q"], sp)
            if dg is not None:
                dg = float(np.clip(dg, -6 * sg, 6 * sg))
                new["qmc_gaina"] -= dg / 2
                new["qmc_gainb"] += dg / 2
                self.model["gain_db_lsb"] = math.sqrt(kg)
                txt += f", gain B-A {dg:+.0f}"
            if dp is not None:
                dp = float(np.clip(dp, -6 * sp, 6 * sp))
                new["qmc_phaseab"] += dp
                self.model["phase_deg_lsb"] = math.sqrt(kp)
                txt += f", phase {dp:+.0f}"
        self.model["learned"] = time.strftime("%Y-%m-%d %H:%M")
        self.center = signed(raw(new))
        self.regs = raw(self.center)
        self.step = {k: max(STEP_MIN[k], self.step[k] // 2) for k in self.step}
        self.cur = None
        self.log.append(f"fit → {txt} LSB; applying and checking")
        return self.regs

    def _finish(self, ok, why):
        self.done, self.ok = True, ok
        self.regs = raw(self.center)
        self.log.append(("✔ " if ok else "✖ ") + why)
        return None


# ================================================================================================ panel
class DacCalPanel(QtWidgets.QWidget):
    """BOS Test Station results tab 'DAC calibration'."""

    def __init__(self, st):
        super().__init__()
        self.st = st
        d = st.data.setdefault("dac_cal", {})
        self.regs = dict(d.get("regs") or DOC_DEFAULT)
        self.opts = {**OPTS, **d.get("opts", {})}
        self.models = d.get("models", {})               # serial -> model
        self.m = None
        self.auto = None
        self.pending_restore = None
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        self.meas = QtWidgets.QLabel("No OQPSK / BPSK measurement yet - run a plan with BPSK / OQPSK or Live "
                                     "diagnose.")
        self.meas.setWordWrap(True)
        v.addWidget(self.meas)
        h = QtWidgets.QHBoxLayout()
        g = QtWidgets.QGroupBox("Registers in the unit (Change_DAC_1 / DAC_offsets)")
        f = QtWidgets.QFormLayout(g)
        self.ed = {}
        for k in REGS:
            n, sg = BITS[k]
            sp = QtWidgets.QSpinBox()
            sp.setRange(-(1 << (n - 1)) if sg else 0, (1 << (n - 1)) - 1 if sg else (1 << n) - 1)
            sp.setValue(to_signed(k, self.regs[k]))
            sp.setToolTip(f"{n}-bit {'two-s complement' if sg else 'unsigned'} (signed value; raw in the table)")
            sp.valueChanged.connect(self._regs_edited)
            f.addRow(k, sp)
            self.ed[k] = sp
        row = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Document defaults")
        b.clicked.connect(lambda: self.set_regs(DOC_DEFAULT))
        self.b_send = QtWidgets.QPushButton("Send to BOS")
        self.b_send.setToolTip("Change_DAC_1 with these values now")
        self.b_send.clicked.connect(lambda: self._send(self.regs))
        row.addWidget(b)
        row.addWidget(self.b_send)
        f.addRow(row)
        h.addWidget(g)
        right = QtWidgets.QVBoxLayout()
        self.table = QtWidgets.QTableWidget(4, 3)
        self.table.setHorizontalHeaderLabels(["Register(s)", "Measured error", "Off by about"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMaximumHeight(150)
        right.addWidget(self.table)
        self.model_lbl = QtWidgets.QLabel()
        self.model_lbl.setWordWrap(True)
        right.addWidget(self.model_lbl)
        og = QtWidgets.QGroupBox("Auto-calibration measurement")
        of = QtWidgets.QFormLayout(og)
        self.o_avg = QtWidgets.QSpinBox()
        self.o_avg.setRange(1, 20)
        self.o_avg.setValue(int(self.opts["n_avg"]))
        self.o_avg.setToolTip("Captures averaged at every register setting (|offset|², gain², phase² are averaged)")
        self.o_skip = QtWidgets.QSpinBox()
        self.o_skip.setRange(0, 5)
        self.o_skip.setValue(int(self.opts["skip"]))
        self.o_skip.setToolTip("Captures thrown away after every Change_DAC_1 (may contain the old setting)")
        self.o_settle = QtWidgets.QDoubleSpinBox()
        self.o_settle.setRange(0, 30)
        self.o_settle.setDecimals(1)
        self.o_settle.setSuffix(" s")
        self.o_settle.setValue(float(self.opts["settle_s"]))
        self.o_settle.setToolTip("Wait after every Change_DAC_1 before the next capture")
        self.o_nsym = QtWidgets.QSpinBox()
        self.o_nsym.setRange(2000, 1_000_000)
        self.o_nsym.setSingleStep(4000)
        self.o_nsym.setValue(int(self.opts["nsym"]))
        self.o_nsym.setToolTip("Symbols per capture during the calibration (the station setting is used otherwise)")
        self.o_sess = QtWidgets.QCheckBox("Station starts and keeps the session (OQPSK / BPSK)")
        self.o_sess.setChecked(bool(self.opts["own_session"]))
        self.o_sess.setToolTip("Protocol control: the station sends the session command itself, keeps the target "
                               "designation and re-issues the session before it ends (minimum session duration); "
                               "MODE_D at the end. Off: the operator keeps the BOS transmitting.")
        of.addRow("Captures per setting", self.o_avg)
        of.addRow("Discard after a change", self.o_skip)
        of.addRow("Settle after a change", self.o_settle)
        of.addRow("Symbols per capture", self.o_nsym)
        of.addRow(self.o_sess)
        for w in (self.o_avg, self.o_skip, self.o_nsym):
            w.valueChanged.connect(self._opts_changed)
        self.o_settle.valueChanged.connect(self._opts_changed)
        self.o_sess.toggled.connect(self._opts_changed)
        right.addWidget(og)
        self.est = QtWidgets.QLabel()
        self.est.setStyleSheet("color:#8a96a3;")
        right.addWidget(self.est)
        bt = QtWidgets.QHBoxLayout()
        self.b_auto = QtWidgets.QPushButton("▶ Auto-calibrate (live)")
        self.b_auto.setStyleSheet("font-weight:700;padding:5px 12px;")
        self.b_auto.setToolTip("Live diagnose is started if needed (OQPSK: offset, gain and phase; BPSK: offset only). "
                               "The registers are probed around the working point, the optimum is fitted, applied "
                               "and checked - 10 captures per round, up to 4 rounds. The BOS must be transmitting.")
        self.b_auto.clicked.connect(self.toggle_auto)
        self.b_md = QtWidgets.QPushButton("Write to MD_SETTINGS…")
        self.b_md.setToolTip("Put these values into a DAC_offsets row of the MD_SETTINGS editor (Telemetry → BOS) "
                             "with a DAC temperature threshold")
        self.b_md.clicked.connect(self.to_md)
        self.b_ktr = QtWidgets.QPushButton("Open in Control KTR")
        self.b_ktr.clicked.connect(self.to_ktr)
        for w in (self.b_auto, self.b_md, self.b_ktr):
            bt.addWidget(w)
        bt.addStretch(1)
        right.addLayout(bt)
        right.addStretch(1)
        h.addLayout(right, 1)
        v.addLayout(h)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.log.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        v.addWidget(self.log, 1)
        note = QtWidgets.QLabel(
            "Targets: carrier offset ≤ -55 dBc, gain imbalance ≤ 0.02 dB, quadrature error ≤ 0.1°. The FSW demodulator "
            "locks to one of four orientations on every capture, so one measurement tells how far the registers are "
            "off but not in which direction - Auto-calibrate finds it by probing (minimises |offset|², gain² and "
            "phase² by fitted parabolas). Change_DAC_1 values stay until a BOS reset or MD SETUP; at the next "
            "session start the BOS loads the DAC_offsets set of MD_SETTINGS for the DAC temperature - write the "
            "result there to keep it.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#8a96a3;")
        v.addWidget(note)
        self._refresh()
        self._estimate()

    # ---- state
    def serial(self):
        return (self.st.serial.text().strip() if hasattr(self.st, "serial") else "") or "unit"

    def model(self):
        return self.models.get(self.serial()) or NOMINAL

    def _save(self):
        self.st.data["dac_cal"] = {"regs": self.regs, "models": self.models, "opts": self.opts}
        self.st.save()

    def set_regs(self, regs):
        self.regs = dict(regs)
        for k, sp in self.ed.items():
            sp.blockSignals(True)
            sp.setValue(to_signed(k, self.regs[k]))
            sp.blockSignals(False)
        self._save()
        self._refresh()

    def _regs_edited(self, *_):
        self.regs = raw({k: sp.value() for k, sp in self.ed.items()})
        self._save()

    def _opts_changed(self, *_):
        self.opts = {"n_avg": self.o_avg.value(), "skip": self.o_skip.value(), "settle_s": self.o_settle.value(),
                     "nsym": self.o_nsym.value(), "own_session": self.o_sess.isChecked()}
        self._save()
        self._estimate()

    def _estimate(self):
        per = self.opts["n_avg"] + self.opts["skip"]
        n = 1 + 9 * per + per                                # start, 9 probes, check
        sr = self.st.cfg.get("sr_oqpsk", 131.072e6)
        self.est.setText(f"≈ {n} captures per round ({self.opts['nsym']:,} symbols = "
                         f"{self.opts['nsym'] / sr * 1e3:.2f} ms of signal each, ≥ {self.opts['settle_s']:g} s "
                         f"settling after each of 10 changes)")

    def say(self, text):
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")

    # ---- measurements
    def show_result(self, res):
        """Called after every analysis (plans and live diagnose)."""
        r = (res or {}).get("mod_OQPSK") or (res or {}).get("mod_BPSK")
        m = measurement(r)
        if m is not None:
            self.m = m
            self._refresh()

    def _refresh(self):
        m, md = self.m, self.model()
        rows = [("qmc_offseta / qmc_offsetb", "carrier offset", "off"), ("qmc_gaina / qmc_gainb",
                                                                           "gain imbalance", "gain"),
                ("qmc_phaseab", "quadrature error", "phase"), ("coarse_dac", "full-scale current", None)]
        gd = guidance(m, md) if m else {}
        for r, (reg, what, key) in enumerate(rows):
            if m is None or key is None:
                err, off = ("—", "—") if key else ("not used for I/Q errors", "—")
            elif key == "off":
                err = f"{dbc(m['c']):.1f} dBc"
                off = f"≈ {gd['off']:.0f} LSB (vector A/B)"
            elif key in gd:
                val = m["g"] if key == "gain" else m["q"]
                err = f"{abs(val):.3f} dB" if key == "gain" else f"{abs(val):.2f}°"
                off = f"≈ {gd[key]:.0f} LSB" + (" of gainB - gainA" if key == "gain" else "")
            else:
                err, off = "BPSK: not measurable", "—"
            for c, t in enumerate((reg, err, off)):
                self.table.setItem(r, c, QtWidgets.QTableWidgetItem(t))
        self.table.resizeColumnsToContents()
        if m is not None:
            col = "#4fd18b" if converged(m) else "#e3b341"
            parts = [f"<b>{m['mod']}</b>: carrier offset <b>{dbc(m['c']):.1f} dBc</b>"]
            if math.isfinite(m["g"]):
                parts += [f"gain imbalance <b>{abs(m['g']):.3f} dB</b>", f"quadrature <b>{abs(m['q']):.2f}°</b>"]
            parts.append(f"MER {m['mer']:.1f} dB, ≈ {m['mer_corr']:.1f} dB once the I/Q errors are removed")
            self.meas.setText(f"<span style='color:{col}'>" + " · ".join(parts) +
                              (" — within the targets" if converged(m) else "") + "</span>")
        self.model_lbl.setText(
            (f"Register scales learned for '{self.serial()}' on {md['learned']}: " if md.get("learned") else
             "Register scales: data-sheet values (Auto-calibrate measures the unit's own): ") +
            f"offset {md['off_lsb']:.0f} LSB per unit offset ({md['off_lsb'] * 10 ** (-40 / 20):.0f} LSB = -40 dBc), "
            f"gain {md['gain_db_lsb'] * 1e3:.2f} mdB/LSB, phase {md['phase_deg_lsb'] * 1e3:.2f} m°/LSB")

    # ---- actions
    def _send(self, regs):
        import bos_protocol as bp
        data = bp.encode(0x50, regs)
        ok = self.st._send_now(0x50, data)
        self.say(f"Change_DAC_1 [{data.hex(' ').upper()}] " + ("sent" if ok else "NOT sent (CAN line not connected?)"))
        return ok

    def toggle_auto(self):
        if self.auto is not None:
            self.auto = None
            self.st._live_session = False
            self.b_auto.setText("▶ Auto-calibrate (live)")
            self.say("auto-calibration stopped - sending back the working point")
            self._send(self.regs)
            return
        st = self.st
        if st.running and not st.live_mode:
            self.say("the station is running a plan - wait until it ends")
            return
        self.auto = AutoCal(self.regs, self.model(), n_avg=self.opts["n_avg"], skip=self.opts["skip"])
        st._live_session = bool(self.opts["own_session"]) and st.ctl.get("method") == "protocol"
        self.b_auto.setText("■ Stop calibration")
        self.say(f"auto-calibration started (unit '{self.serial()}'), working point " +
                 ", ".join(f"{k} {v:+d}" for k, v in signed(self.regs).items()))
        self._send(self.regs)                           # make sure the unit runs the working point
        if not st.live_mode:
            if st.live_mod.currentText() == "CW":
                st.live_mod.setCurrentText("OQPSK")
            st.live_btn.setChecked(True)

    def live_step(self, res):
        """Called by the live diagnose loop after each capture: registers to send next, or None."""
        if self.auto is None:
            return None
        r = (res or {}).get("mod_OQPSK") or (res or {}).get("mod_BPSK")
        m = measurement(r)
        if m is None:
            self.say("no modulated measurement in this capture - waiting")
            return None
        n0 = len(self.auto.log)
        nxt = self.auto.feed(m)
        for line in self.auto.log[n0:]:
            self.say(line)
        if nxt == "wait":
            return None
        if self.auto.done:
            a, self.auto = self.auto, None
            self.b_auto.setText("▶ Auto-calibrate (live)")
            if a.model.get("learned"):
                self.models[self.serial()] = a.model
            self.st._live_session = False
            self.set_regs(a.regs)
            self.say("registers in the unit: " + ", ".join(f"{k} {v:+d} (0x{a.regs[k]:X})"
                                                          for k, v in signed(a.regs).items()))
            return a.regs if not a.ok else None        # failed: put the best working point back
        return nxt

    def to_md(self):
        tele = getattr(self.st.host, "telem", None)
        ed = next((e for e in getattr(tele, "md_editors", []) if e.kind == "settings"), None) if tele else None
        if ed is None:
            return self.say("MD_SETTINGS editor not available (Telemetry tab)")
        t = self.st.tlm.get("Temp_DT2")
        row, ok = QtWidgets.QInputDialog.getInt(self, "MD_SETTINGS", "DAC_offsets row (0-15):", 0, 0, 15)
        if not ok:
            return
        thr, ok = QtWidgets.QInputDialog.getInt(
            self, "MD_SETTINGS", "DAC temperature threshold, °C (the set is used above it)" +
            (f" - DAC now {t:.0f} °C:" if t is not None else ":"), int(t) - 5 if t is not None else -128, -128, 127)
        if not ok:
            return
        import bos_mdfiles as mf
        vals = {f"DAC_offsets_{row}.{k}": self.regs[k] for k in REGS}
        vals[f"DAC_offsets_temp_{row}"] = thr
        ed.data = mf.from_values("settings", vals, ed.data)
        ed.refresh()
        ed.msg(f"DAC_offsets_{row} written from the DAC calibration (above {thr} °C) - send it with ⇡", True)
        self.say(f"written to MD_SETTINGS DAC_offsets_{row} (above {thr} °C) - send it from Telemetry → BOS → "
                 f"MD_SETTINGS")
        tabs = getattr(self.st.host, "tabs", None)
        if tabs is not None:
            tabs.setCurrentWidget(tele)

    def to_ktr(self):
        k = getattr(self.st.host, "ktr", None)
        if k is None:
            return self.say("Control KTR tab not available")
        k.bos.select_code(0x50)
        k.bos.show_cmd(0x50, dict(self.regs))
        self.st.host.tabs.setCurrentWidget(k)
