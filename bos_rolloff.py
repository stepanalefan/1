# -*- coding: utf-8 -*-
"""
bos_rolloff.py
==============
BOS Test Station results tab "Roll-off": which RRC roll-off α the unit really transmits (bos_analysis.
estimate_rolloff), how much the demodulation loses with the configured α, and buttons to use the estimate in the
station's software demodulation and in the FSW-K70 Tx / measurement filter.

Two estimates per OQPSK capture:
* spectrum - the 75 % / 50 % / 25 % power crossings of the raised-cosine spectrum (α = 6·Δf / symbol rate);
* matched filter - the α of the receive RRC that gives the highest MER (after removing the I/Q errors); only
  conclusive when the curve has a clear peak - when MER is limited by noise / phase noise the curve is flat and the
  roll-off does not matter for the measurement.
Live diagnose: the estimates of the last captures are averaged.
"""

from __future__ import annotations

import math
import time

import numpy as np
from PyQt5 import QtCore, QtWidgets

try:
    import pyqtgraph as pg
except Exception:                                      # pragma: no cover
    pg = None


def _fin(v):
    return v is not None and isinstance(v, (int, float)) and math.isfinite(v)


class RolloffPanel(QtWidgets.QWidget):
    def __init__(self, st):
        super().__init__()
        self.st = st
        self.hist = []                                 # (time, alpha_spec, alpha_mer, prominence)
        self.last = None
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        self.head = QtWidgets.QLabel("No OQPSK measurement yet - run a plan with OQPSK or Live diagnose (OQPSK).")
        self.head.setWordWrap(True)
        self.head.setStyleSheet("font-size:11pt;")
        v.addWidget(self.head)
        self.table = QtWidgets.QTableWidget(6, 2)
        self.table.setHorizontalHeaderLabels(["Quantity", "Value"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setFixedHeight(24 * 7 + 12)
        v.addWidget(self.table)
        if pg is not None:
            h = QtWidgets.QHBoxLayout()
            self.p_spec = pg.PlotWidget()
            self.p_spec.setLabel("bottom", "frequency offset", "Hz")
            self.p_spec.setLabel("left", "power", "dB")
            self.p_spec.showGrid(x=True, y=True, alpha=0.25)
            self.p_spec.addLegend(offset=(-10, -10))
            self.p_spec.getAxis("bottom").enableAutoSIPrefix(True)
            self.c_meas = self.p_spec.plot(pen=pg.mkPen("#c9d1d9", width=1), name="measured (smoothed)")
            self.c_fit = self.p_spec.plot(pen=pg.mkPen("#4fd18b", width=2), name="RC, estimated α")
            self.c_set = self.p_spec.plot(pen=pg.mkPen("#f0b03e", width=1, style=QtCore.Qt.DashLine), name="RC, set α")
            self.p_mer = pg.PlotWidget()
            self.p_mer.setLabel("bottom", "receive RRC roll-off α")
            self.p_mer.getAxis("bottom").enableAutoSIPrefix(False)
            self.p_mer.setLabel("left", "MER after I/Q correction", "dB")
            self.p_mer.showGrid(x=True, y=True, alpha=0.25)
            self.c_mer = self.p_mer.plot(pen=pg.mkPen("#58a6ff", width=2), symbol="o", symbolSize=5,
                                         symbolBrush="#58a6ff")
            self.l_set = pg.InfiniteLine(angle=90, pen=pg.mkPen("#f0b03e", style=QtCore.Qt.DashLine))
            self.l_best = pg.InfiniteLine(angle=90, pen=pg.mkPen("#4fd18b"))
            self.p_mer.addItem(self.l_set)
            self.p_mer.addItem(self.l_best)
            h.addWidget(self.p_spec, 3)
            h.addWidget(self.p_mer, 2)
            v.addLayout(h, 1)
        bt = QtWidgets.QHBoxLayout()
        self.live = QtWidgets.QCheckBox("Estimate during Live diagnose (every 5th capture, ≈ 1 s each)")
        self.live.setChecked(bool(st.data.get("rolloff_live", True)))
        self.live.toggled.connect(lambda on: (st.data.__setitem__("rolloff_live", on), st.save()))
        self.navg = QtWidgets.QSpinBox()
        self.navg.setRange(1, 50)
        self.navg.setValue(int(st.data.get("rolloff_navg", 10)))
        self.navg.setPrefix("average last ")
        self.navg.setSuffix(" captures")
        self.navg.valueChanged.connect(lambda n: (st.data.__setitem__("rolloff_navg", n), st.save(), self._show()))
        self.b_reset = QtWidgets.QPushButton("Reset average")
        self.b_reset.clicked.connect(lambda: (self.hist.clear(), self._show()))
        self.b_use = QtWidgets.QPushButton("Use estimated α in the station")
        self.b_use.setToolTip("Sets 'OQPSK RRC roll-off' (Unit / signal page): software demodulation, spectrum "
                              "limits and the FSW-K70 filter of the plans use it from now on")
        self.b_use.clicked.connect(self.use)
        self.b_k70 = QtWidgets.QPushButton("Set FSW-K70 filter to it")
        self.b_k70.setToolTip("DDEM:TFIL:STAT ON, DDEM:TFIL:NAME 'RRC', DDEM:TFIL:ALPH α, DDEM:MFIL:AUTO ON "
                              "(receive filter matched to the Tx filter); read back and checked")
        self.b_k70.clicked.connect(self.to_k70)
        for w in (self.live, self.navg, self.b_reset):
            bt.addWidget(w)
        bt.addStretch(1)
        bt.addWidget(self.b_use)
        bt.addWidget(self.b_k70)
        v.addLayout(bt)
        note = QtWidgets.QLabel(
            "Spectrum: α from the 75 % / 50 % / 25 % power crossings of the raised cosine on both sides (α = 6·Δf / "
            "symbol rate; PA regrowth and noise stay far below -6 dB, so they do not bias it; about ±0.03 per 16 000 "
            "symbols - averaging helps). Matched filter: the receive α with the highest MER after removing the I/Q "
            "errors - decisive only when the curve has a clear peak; a flat curve means the roll-off is not what "
            "limits the MER. When the signal is wider than the FSW bandwidth (131 Msym/s with B160) the plans add "
            "two captures tuned ±0.4·symbol rate off the carrier, one per transition band, and the MER peak only "
            "describes the band-limited capture.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#8a96a3;")
        v.addWidget(note)
        self.b_use.setEnabled(False)
        self.b_k70.setEnabled(False)

    def wanted(self) -> bool:
        """Estimate in this capture? Always in plans, in Live diagnose when ticked."""
        if not self.st.live_mode:
            return True
        # live: every 5th capture only - the estimate costs ≈ 1 s and would slow every capture
        return self.live.isChecked() and getattr(self.st, "_live_n", 1) % 5 == 1

    def show_result(self, res):
        r = (res or {}).get("mod_OQPSK") or {}
        ro = r.get("rolloff")
        if not ro or "error" in ro or ro is self.last:
            return
        self.last = ro
        self.hist.append((time.time(), ro.get("alpha_spec"), ro.get("alpha_mer"), ro.get("mer_prominence")))
        self.hist = self.hist[-50:]
        self._show()

    def estimate(self):
        """(α to recommend, basis, n captures) from the average of the last captures."""
        n = self.navg.value()
        h = self.hist[-n:]
        sp = [a for _, a, _, _ in h if _fin(a)]
        mm = [a for _, _, a, pr in h if _fin(a) and _fin(pr) and pr >= 0.5]
        if mm and len(mm) >= max(1, len(h) // 2) and not (self.last or {}).get("band_limited"):
            return float(np.median(mm)), "matched-filter MER peak", len(mm)
        if sp:
            return float(np.mean(sp)), "spectrum", len(sp)
        return None, "", 0

    def _show(self):
        ro = self.last
        if ro is None:
            return
        a_set = self.st.cfg.get("alpha", 0.25)
        est, basis, n = self.estimate()
        sp = [a for _, a, _, _ in self.hist[-self.navg.value():] if _fin(a)]
        rows = [
            ("Configured α (station)", f"{a_set:.3f}"),
            ("α from the spectrum (this capture)", f"{ro.get('alpha_spec', float('nan')):.3f}" +
             (f"   (sides / levels spread {ro['spec_spread']:.3f})" if _fin(ro.get('spec_spread')) else "") +
             ("   25 % points beyond the FSW band" if ro.get("edge_cut") else "")),
            (f"α from the spectrum, mean of {len(sp)}", f"{np.mean(sp):.3f} ± {np.std(sp) / max(1, math.sqrt(len(sp))):.3f}"
             if sp else "—"),
            ("α with the best MER (this capture)", f"{ro.get('alpha_mer', float('nan')):.3f}   (peak "
             f"{ro.get('mer_prominence', float('nan')):+.2f} dB above α ± 0.1)"),
            ("MER with the set α → best α", f"{ro.get('mer_set', float('nan')):.2f} dB → "
             f"{ro.get('mer_best', float('nan')):.2f} dB  ({ro.get('mer_best', 0) - ro.get('mer_set', 0):+.2f} dB)"),
            ("Recommended α", "—" if est is None else f"{est:.3f}  ({basis}, {n} capture(s))" +
             ("   — FSW bandwidth narrower than the signal: best receive α "
              f"{ro.get('alpha_mer', float('nan')):.3f} only describes this capture" if ro.get("band_limited") else "")),
        ]
        for i, (a, b) in enumerate(rows):
            self.table.setItem(i, 0, QtWidgets.QTableWidgetItem(a))
            self.table.setItem(i, 1, QtWidgets.QTableWidgetItem(b))
        self.table.resizeColumnToContents(0)
        if est is not None:
            diff = est - a_set
            gain = ro.get("mer_best", 0) - ro.get("mer_set", 0)
            if abs(diff) <= 0.03:
                txt, col = f"The configured α {a_set:.2f} fits the signal (estimate {est:.3f}).", "#4fd18b"
            else:
                txt = (f"The signal looks like α ≈ {est:.2f}, the station uses {a_set:.2f} "
                       f"({diff:+.2f}). ")
                txt += (f"Using it would raise the measured MER by {gain:.1f} dB." if gain >= 0.3 else
                        "The MER hardly changes (other errors dominate), but OBW / ACLR limits and the K70 filter "
                        "should use the real α.")
                col = "#e3b341"
            self.head.setText(f"<span style='color:{col}'>{txt}</span>")
        self.b_use.setEnabled(est is not None)
        self.b_k70.setEnabled(est is not None)
        if pg is not None and ro.get("plot"):
            pl = ro["plot"]
            ref = float(np.max(pl["fit"])) or 1.0
            db = lambda y: 10 * np.log10(np.maximum(np.asarray(y) / ref, 1e-12))
            self.c_meas.setData(pl["f"], db(pl["p"]))
            self.c_fit.setData(pl["f"], db(pl["fit"]))
            self.c_set.setData(pl["f"], db(pl["set"]))
            self.p_spec.setYRange(-45, 5)
        if pg is not None and ro.get("curve"):
            a, m = zip(*ro["curve"])
            self.c_mer.setData(a, m)
            self.l_set.setValue(a_set)
            self.l_best.setValue(ro.get("alpha_mer", a_set))

    def use(self):
        est, basis, n = self.estimate()
        if est is None:
            return
        est = round(est, 3)
        w = self.st.cfg_w.get("alpha")
        if w:
            w[0].setValue(est / w[2])
        self.st.cfg["alpha"] = est
        self.st._collect()
        self.st.log(f"BOS station: OQPSK roll-off set to α = {est:.3f} ({basis}, {n} capture(s))", "ok")
        self._show()

    def to_k70(self):
        est, _, _ = self.estimate()
        h = self.st.host
        dev = getattr(getattr(h, "worker", None), "dev", None)
        if est is None or dev is None:
            return
        if not getattr(dev, "k70", False):
            self.st.log("BOS station: FSW-K70 not installed - nothing to set", "warn")
            return
        a = round(est, 3)

        def job():
            base = dev.read_settings()
            s = dict(base, tfil_state=True, tfil_name="RRC", tfil_alpha=a, mfil_auto=True)
            cmds, errs, notes = dev.apply_settings(s, base)
            rb = dev.read_settings()
            return {"cmds": cmds, "errs": errs, "rb": rb}

        def done(tag, res):
            if tag != "rolloff":
                return
            try:
                h.worker.job_done.disconnect(done)
            except Exception:
                pass
            rb = res["rb"]
            ok = str(rb.get("tfil_name") or "").upper().strip("'\"").startswith("RRC") and \
                _fin(rb.get("tfil_alpha")) and abs(rb["tfil_alpha"] - a) < 1e-3 and rb.get("tfil_state") is not False
            self.st.log(f"BOS station: FSW-K70 Tx filter {rb.get('tfil_name')} α={rb.get('tfil_alpha')}, measurement "
                        f"filter auto {rb.get('mfil_auto')}" + ("" if ok else " - NOT as requested") +
                        (f"; errors: {'; '.join(res['errs'])}" if res["errs"] else ""), "ok" if ok else "err")
        h.worker.job_done.connect(done)
        h.worker.submit("rolloff", job)
