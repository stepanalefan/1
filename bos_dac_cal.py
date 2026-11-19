# -*- coding: utf-8 -*-
"""
bos_dac_cal.py
==============
DAC38J82 QMC calibration of the BOS from FSW measurements (ПИЛВ 0102-004 v2: Change_DAC_1 0x50, table 4.43, and
the DAC_offsets sets of MD_SETTINGS, table 4.7).

Registers (DAC38J82): qmc_offseta / qmc_offsetb (13-bit two's complement, added to the A / B data - cancel the LO
leakage / carrier offset), qmc_gaina / qmc_gainb (11-bit, gain = code / 1024 - I/Q amplitude balance), qmc_phaseab
(12-bit two's complement - quadrature), coarse_dac (full-scale current, lowered first while the MER
holds - see AutoCal).

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


def _mono_font():
    """Bundled DejaVu Sans Mono (ui_fonts), else the system fixed font."""
    try:
        import ui_fonts
        return ui_fonts.mono()
    except Exception:
        return QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)

class RegSpin(QtWidgets.QSpinBox):
    """Register field shown in HEX / DEC / BIN (like the Frames tab); the value is always the signed number."""

    def __init__(self, nbits, signed):
        super().__init__()
        self.nbits, self.signed, self.fmt = nbits, signed, "DEC"

    def set_fmt(self, fmt):
        self.fmt = fmt
        v = self.value()
        self.setDisplayIntegerBase(10)
        self.lineEdit().setText(self.textFromValue(v))
        self.updateGeometry()

    def _raw(self, v):
        return v & ((1 << self.nbits) - 1)

    def textFromValue(self, v):
        if self.fmt == "HEX":
            return f"0x{self._raw(v):0{(self.nbits + 3) // 4}X}"
        if self.fmt == "BIN":
            return f"0b{self._raw(v):0{self.nbits}b}"
        return str(v)

    def valueFromText(self, text):
        t = text.strip().replace("_", "").replace(" ", "")
        try:
            if self.fmt == "HEX" or t.lower().startswith("0x"):
                raw = int(t[2:] if t.lower().startswith("0x") else t, 16)
            elif self.fmt == "BIN" or t.lower().startswith("0b"):
                raw = int(t[2:] if t.lower().startswith("0b") else t, 2)
            else:
                return int(t)
        except ValueError:
            return self.value()
        raw &= (1 << self.nbits) - 1
        if self.signed and raw >= 1 << (self.nbits - 1):
            raw -= 1 << self.nbits
        return raw

    def validate(self, text, pos):
        t = text.strip().replace("_", "").replace(" ", "")
        if t in ("", "-", "0x", "0X", "0b", "0B"):
            return (QtGui.QValidator.Intermediate, text, pos)
        try:
            if self.fmt == "HEX" or t.lower().startswith("0x"):
                ok = int(t[2:] if t.lower().startswith("0x") else t, 16) < 1 << self.nbits
            elif self.fmt == "BIN" or t.lower().startswith("0b"):
                ok = int(t[2:] if t.lower().startswith("0b") else t, 2) < 1 << self.nbits
            else:
                v = int(t)
                ok = self.minimum() <= v <= self.maximum()
        except ValueError:
            return (QtGui.QValidator.Invalid, text, pos)
        return (QtGui.QValidator.Acceptable if ok else QtGui.QValidator.Invalid, text, pos)


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
OPTS = {"n_avg": 3, "skip": 1, "settle_s": 1.0, "nsym": 32000, "own_session": True, "coarse": True,
        "coarse_min": 0, "max_drop_db": 6.0, "min_delta_db": 0.05}   # defaults of the panel


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
            "power": r.get("power", float("nan")), "mod": r.get("expected", "")}


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


def cost_db(m, reg="qmc"):
    """Optimisation metric (dB, lower = better).
    coarse_dac: -MER (the full-scale current changes compression / noise, which only the MER shows);
    QMC registers: sum of the squared QMC impairments - carrier offset |c|, gain imbalance,
    quadrature error, each relative to its target (0 dB = all at their targets) - measured directly and
    rotation-invariant (the MER is too noisy to see small QMC steps)."""
    if reg == "coarse_dac":
        return -m["mer"] if math.isfinite(m["mer"]) else float("inf")
    e = (abs(m["c"]) / 10 ** (TARGET["off_dbc"] / 20)) ** 2           # each impairment relative to its target
    if math.isfinite(m["g"]):
        e += (m["g"] / TARGET["gain_db"]) ** 2
    if math.isfinite(m["q"]):
        e += (m["q"] / TARGET["quad_deg"]) ** 2
    return 10 * math.log10(max(e, 1e-15))


# parameter order of the coordinate search, first step size, smallest step (LSB), allowed directions
COORD = [("coarse_dac", 1, 1, (-1,)),               # only lowered (full-scale current)
         ("qmc_phaseab", 32, 1, (1, -1)),
         ("qmc_gaina", 8, 1, (1, -1)),
         ("qmc_gainb", 8, 1, (1, -1)),
         ("qmc_offseta", 64, 2, (1, -1)),
         ("qmc_offsetb", 64, 2, (1, -1))]


class AutoCal:
    """Coordinate-search calibration driven by successive measurements (live diagnose), strictly:
    1. baseline - the metric (cost_db) of the starting registers is measured and is the first best result;
    2. one-at-a-time - every candidate changes ONE register by its step, all others stay at the best values;
    3. delta - each candidate's metric is compared with the current best: Δ = best − candidate (dB, > 0 = better);
    4. greedy pruning - a candidate with Δ ≤ `min_delta_db` (worse or within the noise) or breaking a constraint
       (register range, output drop > `max_drop_db`, coarse_dac below `coarse_min`) is discarded at once and the best
       registers are sent back; an accepted one becomes the new best and the search goes on in the same direction.
    A register whose both directions are discarded gets half the step; when one whole cycle over all registers at
    their smallest steps brings no improvement, the calibration is complete. Order: coarse_dac (lowered only),
    qmc_phaseab, qmc_gaina, qmc_gainb, qmc_offseta, qmc_offsetb (BPSK: coarse_dac and offsets)."""

    def __init__(self, regs_raw, model, n_avg=3, skip=1, coarse=True, coarse_min=0, max_drop_db=6.0,
                 min_delta_db=0.05, **_):
        self.n_avg, self.skip = max(1, int(n_avg)), max(0, int(skip))
        self.buf, self.to_skip = [], self.skip
        self.model = copy.deepcopy(model)
        self.coarse, self.coarse_min = bool(coarse), int(coarse_min)
        self.max_drop, self.min_delta = float(max_drop_db), float(min_delta_db)
        self.best = None                           # (cost, signed regs, measurement)
        self.center = signed(regs_raw)
        self.regs = dict(regs_raw)
        self.p0 = None                             # output power of the baseline (dBm)
        self.params = None                         # set after the baseline (BPSK: no gain / phase)
        self.step = {k: s0 for k, s0, _, _ in COORD}
        self.k = 0                                 # index of the register being searched
        self.dirs = []                             # directions still to try for it
        self.dir = None                            # direction that improved (kept while it improves)
        self.cand = None                           # (register, direction, signed regs) being measured
        self.improved_cycle = False
        self.n_eval = self.n_acc = 0
        self.history = []                          # (n, register, value, cost, delta, verdict)
        self.log = []
        self.done = self.ok = False
        self.phase = "baseline"

    def feed(self, m):
        """One capture with self.regs. Returns the registers to send next, None when the calibration is complete, or
        "wait" while captures of this setting are collected (first `skip` discarded, then `n_avg` averaged)."""
        if self.to_skip > 0:
            self.to_skip -= 1
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
        pw = [x.get("power", float("nan")) for x in ms]
        mer = [10 ** (-x["mer"] / 10) for x in ms if math.isfinite(x["mer"])]
        return {"c": complex(math.sqrt(float(np.mean(P))), 0.0),
                "g": math.sqrt(float(np.mean(G))) if G else float("nan"),
                "q": math.sqrt(float(np.mean(Q))) if Q else float("nan"),
                "mer": -10 * math.log10(float(np.mean(mer))) if mer else float("nan"),
                "mer_corr": float(np.mean([x["mer_corr"] for x in ms])),
                "power": float(np.mean(pw)) if all(math.isfinite(v) for v in pw) else float("nan"),
                "mod": ms[0]["mod"], "n": len(ms)}

    @staticmethod
    def _desc(m):
        return (f"offset {dbc(m['c']):.1f} dBc" +
                (f", gain {abs(m['g']):.4f} dB, quad {abs(m['q']):.3f}°" if math.isfinite(m["g"]) else "") +
                f", MER {m['mer']:.2f} dB" + (f", {m['power']:.2f} dBm" if math.isfinite(m.get("power", math.nan))
                                              else ""))

    # ---- search
    def _feed(self, m):
        reg = self.cand[0] if self.cand else "qmc"
        c = cost_db(m, reg)
        if self.phase == "baseline":
            c = cost_db(m)
            bpsk = not (math.isfinite(m["g"]) and math.isfinite(m["q"]))
            self.params = [x for x in COORD if (self.coarse or x[0] != "coarse_dac") and
                           not (bpsk and x[0] in ("qmc_phaseab", "qmc_gaina", "qmc_gainb"))]
            self.best = (c, dict(self.center), m)
            self.p0 = m.get("power", float("nan"))
            self.history.append((0, "baseline", "", c, 0.0, "baseline"))
            self.log.append(f"1. baseline: QMC error {c:.2f} dB, MER {m['mer']:.2f} dB ({self._desc(m)})")
            self.log.append("2.-4. coordinate search, one register per step: " +
                            " → ".join(x[0] for x in self.params) + f"; a candidate is kept only for Δ > "
                            f"{self.min_delta:g} dB and within the constraints")
            self.phase = "search"
            self._start_param()
            return self._next_candidate()
        # a candidate was measured
        reg, d, sregs = self.cand
        self.n_eval += 1
        delta = cost_db(self.best[2], reg) - c                    # > 0: better than the best (same metric)
        drop = (self.p0 - m.get("power", self.p0)) if math.isfinite(self.p0) and \
            math.isfinite(m.get("power", math.nan)) else 0.0
        viol = f"output {-drop:+.2f} dB (limit -{self.max_drop:g} dB)" if drop > self.max_drop else ""
        val = sregs[reg]
        if viol or delta <= self.min_delta:
            why = viol or ("worse" if delta < 0 else "no clear gain")
            self.history.append((self.n_eval, reg, val, c, delta, f"discarded ({why})"))
            self.log.append(f"  {reg} {val:+d}: metric {c:.2f} dB, Δ {delta:+.2f} dB → discarded ({why})")
            self.dir = None
            return self._next_candidate()
        self.n_acc += 1
        self.best = (cost_db(m), dict(sregs), m)
        self.center = dict(sregs)
        self.dir = d
        self.improved_cycle = True
        self.history.append((self.n_eval, reg, val, c, delta, "accepted"))
        self.log.append(f"  {reg} {val:+d}: metric {c:.2f} dB, Δ {delta:+.2f} dB → accepted (new best; "
                        f"{self._desc(m)})")
        self.dirs = [d]                                           # keep going while it improves
        return self._next_candidate()

    def _start_param(self):
        name, _, smin, dirs = self.params[self.k]
        self.dirs = list(dirs)
        self.dir = None

    def _next_candidate(self):
        while True:
            name, _, smin, _ = self.params[self.k]
            while self.dirs:
                d = self.dirs.pop(0)
                cand = dict(self.best[1])
                cand[name] += d * self.step[name]
                if name == "coarse_dac" and cand[name] < self.coarse_min:
                    self.log.append(f"  {name} {cand[name]:+d}: below the allowed minimum {self.coarse_min} → "
                                    f"discarded (constraint)")
                    continue
                if to_signed(name, to_raw(name, cand[name])) != cand[name]:
                    self.log.append(f"  {name} {cand[name]:+d}: outside the register range → discarded "
                                    f"(constraint)")
                    continue
                self.cand = (name, d, cand)
                self.regs = raw(cand)
                return self.regs
            # both directions done for this register: smaller step next time, next register
            if self.dir is None and self.step[name] > smin and name != "coarse_dac":
                self.step[name] = max(smin, self.step[name] // 2)
            self.k += 1
            if self.k >= len(self.params):
                self.k = 0
                at_min = all(self.step[n] <= smin_ for n, _, smin_, _ in self.params if n != "coarse_dac")
                if not self.improved_cycle and at_min:
                    return self._complete()
                self.log.append(f"cycle done ({self.n_eval} candidates, {self.n_acc} accepted, best metric "
                                f"{self.best[0]:.2f} dB); steps " +
                                ", ".join(f"{n} {self.step[n]}" for n, _, _, _ in self.params))
                self.improved_cycle = False
            self._start_param()

    def _complete(self):
        self.done = self.ok = True
        self.center = dict(self.best[1])
        self.regs = raw(self.center)
        self.cand = None
        self.log.append(f"✔ calibration complete: no register improves the metric by more than {self.min_delta:g} dB "
                        f"at the smallest steps - best metric {self.best[0]:.2f} dB after {self.n_eval} candidates "
                        f"({self.n_acc} accepted)")
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
        self.fmt = QtWidgets.QComboBox()
        self.fmt.addItems(["HEX", "DEC", "BIN"])
        self.fmt.setCurrentText("DEC")
        self.fmt.setToolTip("HEX / BIN: the raw register bits (two's complement for the offsets and phase), as in "
                            "the CAN frame;\nDEC: the signed value. Typing works in the shown format "
                            "(0x… / 0b… prefixes also accepted).")
        self.fmt.currentTextChanged.connect(self._set_fmt)
        f.addRow("Format", self.fmt)
        for k in REGS:
            n, sg = BITS[k]
            sp = RegSpin(n, sg)
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
        self.o_coarse = QtWidgets.QCheckBox("Lower coarse_dac first (while the MER holds)")
        self.o_coarse.setChecked(bool(self.opts["coarse"]))
        self.o_coarse.setToolTip("Stage 1: coarse_dac is lowered one step at a time; a step is kept while the MER does "
                                 "not get worse (0.1 dB tolerance)\nand the output has not dropped by more than the "
                                 "limit below. Then phase, gain and offsets are calibrated.")
        self.o_cmin = QtWidgets.QSpinBox()
        self.o_cmin.setRange(0, 15)
        self.o_cmin.setValue(int(self.opts["coarse_min"]))
        self.o_cmin.setToolTip("coarse_dac is never set below this code")
        self.o_drop = QtWidgets.QDoubleSpinBox()
        self.o_drop.setRange(0.0, 30.0)
        self.o_drop.setDecimals(1)
        self.o_drop.setSuffix(" dB")
        self.o_drop.setValue(float(self.opts["max_drop_db"]))
        self.o_drop.setToolTip("Largest output power drop allowed when lowering coarse_dac")
        self.o_delta = QtWidgets.QDoubleSpinBox()
        self.o_delta.setRange(0.0, 3.0)
        self.o_delta.setDecimals(2)
        self.o_delta.setSingleStep(0.05)
        self.o_delta.setSuffix(" dB")
        self.o_delta.setValue(float(self.opts["min_delta_db"]))
        self.o_delta.setToolTip("A candidate is kept only when it improves the metric by more than this (Δ); "
                                "0 = any improvement.\nA little above the measurement noise stops noise from "
                                "being taken for an improvement.")
        of.addRow("Keep a change only for Δ >", self.o_delta)
        self.o_delta.valueChanged.connect(self._opts_changed)
        of.addRow(self.o_coarse)
        of.addRow("coarse_dac not below", self.o_cmin)
        of.addRow("Max. output drop", self.o_drop)
        self.o_coarse.toggled.connect(self._opts_changed)
        self.o_drop.valueChanged.connect(self._opts_changed)
        for w in (self.o_avg, self.o_skip, self.o_nsym, self.o_cmin):
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
        self.b_auto.setToolTip("Coordinate search (Live diagnose is started if needed): baseline metric first, then one "
                               "register per step - coarse_dac (lowered only), qmc_phaseab, qmc_gaina, qmc_gainb,\n"
                               "qmc_offseta, qmc_offsetb (BPSK: coarse_dac and offsets). Each candidate's Δ against the "
                               "best is computed; worse / unclear / out-of-limit candidates are discarded at once.\n"
                               "Ends when no register improves any more at the smallest steps, or with Stop; the "
                               "final registers are written to bos_reports/dac_calibration. The BOS must be transmitting.")
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
        self.log.setFont(_mono_font())
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
                     "nsym": self.o_nsym.value(), "own_session": self.o_sess.isChecked(),
                     "coarse": self.o_coarse.isChecked(), "coarse_min": self.o_cmin.value(),
                     "max_drop_db": self.o_drop.value(),
                     "min_delta_db": self.o_delta.value()}
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
    def _set_fmt(self, fmt):
        for sp in self.ed.values():
            sp.set_fmt(fmt)

    def _write_result(self, a, regs, how):
        """File with the final DAC registers: bos_reports/dac_calibration/DAC_<serial>_<date>.txt"""
        import os
        import bos_protocol as bp
        try:
            import bos_station as bs
            folder = os.path.join(bs.REPORT_DIR, "dac_calibration")
        except Exception:
            folder = "dac_calibration"
        sn = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in self.serial()) or "unit"
        stamp = time.strftime("%Y-%m-%d_%H%M%S")
        path = os.path.join(folder, f"DAC_{sn}_{stamp}.txt")
        sg = signed(regs)
        L = [f"BOS DAC38J82 calibration - unit {self.serial()}", f"date        {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"result      {how}"]
        if a.best and a.history:
            L.append(f"metric      baseline {a.history[0][3]:.2f} dB -> final {a.best[0]:.2f} dB "
                     f"(total error power vs signal, lower = better)")
            L.append(f"final       {a._desc(a.best[2])}")
            L.append(f"candidates  {a.n_eval} measured, {a.n_acc} accepted; min. delta {a.min_delta:g} dB, "
                     f"max. output drop {a.max_drop:g} dB, coarse_dac >= {a.coarse_min}")
        L += ["", f"{'register':<14}{'signed':>8}{'hex':>9}  binary"]
        for k in REGS:
            n, _ = BITS[k]
            L.append(f"{k:<14}{sg[k]:>+8d}{f'0x{regs[k]:0{(n + 3) // 4}X}':>9}  {regs[k]:0{n}b}")
        try:
            data = bp.encode(0x50, regs)
            L += ["", f"Change_DAC_1 (0x50) data: {data.hex(' ').upper()}"]
        except Exception:
            pass
        if a.history:
            L += ["", "search history (Δ > 0 = better than the best at that moment):",
                  f"{'#':>4}  {'register':<14}{'value':>7}{'metric dB':>11}{'Δ dB':>8}  verdict"]
            for n, reg, val, c, d, v in a.history:
                L.append(f"{n:>4}  {reg:<14}{(f'{val:+d}' if val != '' else ''):>7}{c:>11.2f}{d:>+8.2f}  {v}")
        try:
            os.makedirs(folder, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(L) + "\n")
            self.say(f"final DAC registers written to {path}")
        except OSError as e:
            self.say(f"cannot write {path}: {e}")
        return path

    def _send(self, regs):
        import bos_protocol as bp
        data = bp.encode(0x50, regs)
        ok = self.st._send_now(0x50, data)
        self.say(f"Change_DAC_1 [{data.hex(' ').upper()}] " + ("sent" if ok else "NOT sent (CAN line not connected?)"))
        return ok

    def toggle_auto(self):
        if self.auto is not None:
            a, self.auto = self.auto, None
            self.st._live_session = False
            self.b_auto.setText("▶ Auto-calibrate (live)")
            keep = a.best[1] if a.best else a.center
            regs = raw(keep)
            self.set_regs(regs)
            self.say("auto-calibration stopped by the operator - best working point kept: " +
                     ", ".join(f"{k} {v:+d} (0x{regs[k]:X})" for k, v in signed(regs).items()))
            self._send(regs)
            self._write_result(a, regs, "stopped by the operator")
            return
        st = self.st
        if st.running and not st.live_mode:
            self.say("the station is running a plan - wait until it ends")
            return
        self.auto = AutoCal(self.regs, self.model(), n_avg=self.opts["n_avg"], skip=self.opts["skip"],
                            coarse=self.opts["coarse"], coarse_min=self.opts["coarse_min"],
                            max_drop_db=self.opts["max_drop_db"], min_delta_db=self.opts["min_delta_db"])
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
            self.st._live_session = False
            self.set_regs(a.regs)
            self.say("registers in the unit: " + ", ".join(f"{k} {v:+d} (0x{a.regs[k]:X})"
                                                          for k, v in signed(a.regs).items()))
            self._write_result(a, a.regs, "complete")
            return a.regs                               # the best registers back into the unit
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
