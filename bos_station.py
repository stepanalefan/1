# -*- coding: utf-8 -*-
"""
bos_station.py
==============
"BOS Test Station" tab of fsw_vsa_monitor.py: automated test and fault diagnosis of the BOS
modulator (MPF300T FPGA -> DAC38J82 -> ADMV1013 with LMX2594 LO -> ADRF5730 -> CHA3397).

The station drives the unit over CAN (commands from the CAN library, or operator prompts when a
command is not mapped), captures the output with the FSW (raw I/Q from the I/Q Analyzer - works
with or without FSW-K70), reads the BOS telemetry (PLL locks, JESD, FPGA rails, temperatures,
10 MHz reference) and analyses everything with bos_analysis.py:

* Test plans made of tests: pre-check, supply current, carrier (CW), BPSK, OQPSK, attenuator
  (ADRF5730 bit weights), PA linearity, LO harmonics, stability / drift.
* Every measurement is compared with its limit (Results); the diagnosis engine turns measurements
  and telemetry into findings with likely faulty blocks (block diagram coloured by suspicion) and
  concrete next steps; Corrections proposes DAC38J82 QMC, frequency and power adjustments.
* HTML report per run (with plots) and a history CSV for all units.
* Demo mode: a simulated BOS with injectable faults (operator training, checking the rules).

Settings: bos_station.json next to the program. Reports: bos_reports/.
"""

from __future__ import annotations

import base64
import csv
import datetime as _dt
import html
import json
import math
import os
import sys
import time

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

import bos_analysis as ba
import bos_protocol as bp
import bos_iqfile

try:
    import telemetry
except Exception:  # pragma: no cover
    telemetry = None


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


STORE_FILE = os.path.join(_app_dir(), "bos_station.json")
REPORT_DIR = os.path.join(_app_dir(), "bos_reports")
HISTORY_FILE = os.path.join(REPORT_DIR, "bos_history.csv")

C_OK, C_WARN, C_FAIL, C_DIM, C_INFO = "#4fd18b", "#f0b03e", "#ff6b6b", "#8a96a3", "#58a6ff"
SEV_COLOR = {"fail": C_FAIL, "warn": C_WARN, "info": C_INFO, "ok": C_OK}
SEV_ICON = {"fail": "✖", "warn": "▲", "info": "ℹ", "ok": "✔"}

# --------------------------------------------------------------------------- #
#  Settings                                                                    #
# --------------------------------------------------------------------------- #
# key, label, kind (f = float, i = int, b = bool), default, unit / scale, tooltip
CFG_FIELDS = [
    ("carrier", "Carrier frequency", "f", 37.75e9, "GHz", "RF output frequency of the BOS "
     "(with protocol control: 37650 + 0.125·N MHz from the frequency code on the Unit control page)"),
    ("sr_bpsk", "Symbol rate BPSK (УР)", "f", 64e3, "kHz", "УР: 2 × data rate (e.g. 32 kbit/s -> 64 ksym/s). "
     "With protocol control it follows the УР rate on the Unit control page"),
    ("sr_oqpsk", "Symbol rate OQPSK (ШР)", "f", 131.072e6, "Msym/s", "ШР: always 131.072 Msym/s, for every data rate"),
    ("alpha", "OQPSK RRC roll-off", "f", 0.25, "", "Roll-off of the OQPSK pulse shaping filter in the FPGA"),
    ("bpsk_rect", "BPSK rectangular pulses (SP-L)", "b", True, "", "BPSK (УР) is SP-L coded with ±90° modulation: "
     "constant envelope, no pulse shaping. Then OBW / ACLR / PAPR compression are not judged on BPSK"),
    ("oqpsk_rrc", "OQPSK RRC filtered", "b", True, "", "Untick if the ШР signal is not pulse-shaped"),
    ("sps_bpsk", "Samples / symbol BPSK", "i", 8, "", "FSW I/Q sample rate = symbol rate × this (2…16)"),
    ("fs_oqpsk", "OQPSK capture sample rate", "f", 524.288e6, "MHz", "FSW I/Q sample rate for the 131.072 Msym/s "
     "signal (524.288 = 4 samples/symbol). Any other rate is resampled to 4 samples/symbol for the demodulator"),
    ("fsw_bw_auto", "FSW bandwidth from options", "b", True, "", "Set bandwidth and OQPSK sample rate from the FSW "
     "options (*OPT?: B160, B320, B512, B1200 …) at the start of every plan"),
    ("fsw_bw", "FSW I/Q bandwidth", "f", 512e6, "MHz", "Analysis bandwidth of the FSW (bandwidth-extension option). "
     "The OQPSK signal occupies ~177 MHz; below that its edges are cut (OBW / PAPR then judged against this "
     "bandwidth; PA compression is harder to see)"),
    ("nsym", "Symbols per capture", "i", 4000, "", "Longer = more accurate, slower"),
    ("decode_ur", "Decode УР data (BPSK)", "b", True, "", "Frame sync 0x1ACFFC1D, RS(255,223)×5, transport frame "
     "CRC, idle-frame content, channel BER. The BPSK capture is lengthened to ≥ 3 КБД (64 000 symbols)"),
    ("shr_decode_min", "ШР: decode КБД from rate", "i", 512, "kbit/s", "КБД despreading + turbo decoding needs "
     "the whole КБД in one capture: 17912 bits × 262144/R chips. Below this rate only the preambles are checked"),
    ("save_iq", "Save every capture (.iq.tar)", "b", False, "", "Writes each FSW capture to bos_reports/captures "
     "(R&S iq.tar) - for re-analysis with 'Analyze I/Q file…'"),
    ("scid", "Spacecraft ID (SCID)", "i", 768, "", "Expected spacecraft identifier in the transport frames (default 0)"),
    ("ber_bits", "BER test: bits", "i", 1000000, "", "Code bits to collect in the BER test (TZ BER ≤ 1·10⁻⁷ needs "
     "≥ 3·10⁷ error-free bits for a 95 % statement)"),
    ("lo_mult", "ADMV1013 LO multiplier", "i", 4, "", "LMX2594 output × this = carrier"),
    ("power_nom", "Nominal output power", "f", -5.5, "dBm", "TZ: maximum output power −9…−2 dBm at the HF output "
     "(set −5.5 ± 3.5). At the technological connector: −30…−10 dBm (set −20 ± 10)"),
    ("att_code", "Nominal Atten_EIRP", "i", 0, "dB", "EIRP attenuation (0…20 dB, 1 dB steps, sets the ADRF5730) "
     "at which the nominal power is specified"),
    ("loss_db", "Cable / fixture loss", "f", 0.0, "dB", "Added to every power reading (BOS output -> FSW input)"),
    ("ext_ref", "External 10 MHz expected", "b", True, "", "External 10 MHz from the TFG6806. The BOS reports the "
     "source (Sour_10MHz) and its validity (Stat_10MHz) in ИОК_ОСН"),
    ("autolevel", "FSW auto level per capture", "b", True, "", "ADJ:LEV before each capture (recommended)"),
    ("ref_level", "FSW ref level (no auto)", "f", 0.0, "dBm", "Used when auto level is off"),
    ("cw_fs", "CW capture bandwidth", "f", 2e6, "MHz", "Sample rate of the carrier capture"),
    ("pn_lo", "Phase noise from", "f", 1e3, "kHz", "Lower limit of the integrated phase noise"),
    ("pn_hi", "Phase noise to", "f", 100e3, "kHz", "Upper limit of the integrated phase noise"),
    ("drift_n", "Stability: measurements", "i", 10, "", "Number of measurements of the stability test"),
    ("drift_s", "Stability: interval", "f", 30.0, "s", "Time between stability measurements"),
    ("lin_step", "Linearity: extra attenuation", "i", 6, "dB", "Atten_EIRP added for the linearity check"),
    ("qmc_gain_lsb_per_unit", "QMC gain LSB per ×1.0", "f", 1024, "", "DAC38J82 QMC gain scaling (check datasheet)"),
    ("qmc_phase_lsb_per_rad", "QMC phase LSB per rad", "f", 4096, "", "DAC38J82 QMC phase scaling (check datasheet)"),
    ("dac_offset_lsb_per_rms", "QMC offset LSB per RMS", "f", 8192, "", "DAC codes per RMS symbol amplitude"),
]
UNITS = {"GHz": 1e9, "MHz": 1e6, "Msym/s": 1e6, "kHz": 1e3}
SHR_SYMBOL_RATE = 131.072e6       # ШР (OQPSK): fixed for every data rate; УР (BPSK): 2 × data rate

LIM_FIELDS = [  # key, label, default, unit
    ("mer_min", "MER min", 20.0, "dB"),
    ("evm_max", "EVM max", 10.0, "%"),
    ("cfe_max_hz", "Carrier frequency error max (TZ 2·10⁻⁶)", 75500.0, "Hz"),
    ("srerr_max_ppm", "Symbol rate error max (TZ 5·10⁻⁵)", 50.0, "ppm"),
    ("power_tol_db", "Output power tolerance ±", 3.5, "dB"),
    ("iq_offset_max_dbc", "Carrier leakage max", -30.0, "dBc"),
    ("gain_imb_max", "I/Q gain imbalance max", 0.5, "dB"),
    ("quad_err_max", "Quadrature error max", 3.0, "°"),
    ("phase_err_max", "Phase error RMS max", 3.0, "°"),
    ("compression_max_db", "PA compression max (PAPR loss)", 0.8, "dB"),
    ("aclr_min", "ACLR min", 30.0, "dB"),
    ("asym_max_db", "Spectrum asymmetry max", 1.0, "dB"),
    ("spur_max_dbc", "Spur max", -40.0, "dBc"),
    ("pn_max_deg", "CW integrated phase noise max", 1.5, "°"),
    ("att_step_max_db", "Attenuator bit error max", 0.25, "dB"),
    ("lo_harm_max_dbc", "LO-related lines max", -40.0, "dBc"),
    ("pa_delta_i_min", "PA current rise min (RF on)", 0.05, "A"),
    ("i_max", "Supply current max, session (TZ, 14 V)", 1.3, "A"),
    ("i_standby_max", "Supply current max, standby (TZ, 14 V)", 0.5, "A"),
    ("rail_tol_pct", "FPGA rail tolerance ±", 3.0, "%"),
    ("temp_max", "Temperature max", 70.0, "°C"),
    ("err_ref_lo", "Err_Ref_LO max |code| (before a mode command)", 3.0, ""),
    ("pulsnumb_tol", "PulsNumb_10MHz: 10 000 000 ±", 5.0, ""),
    ("drift_power_db", "Power drift max", 1.0, "dB"),
    ("drift_cfe_hz", "Frequency drift max", 2000.0, "Hz"),
    ("tlm_stale_s", "Telemetry stale after", 10.0, "s"),
]

TESTS = [  # id, name, description
    ("ref", "Reference & BOS power-up", "TFG6806 to the configured reference (UTILity:CLOck:SOURce), its 10 MHz output to "
                                       "the BOS on, then BOS power on, then 10 MHz pulses (PulsNumb_10MHz) present "
                                       "(added automatically)"),
    ("link", "Control link & BOS setup", "NOP acknowledgement, standby, session limit, telemetry periods, "
                                         "stored КОНФИГ read back (protocol control only, added automatically)"),
    ("pre", "Pre-check (telemetry)", "BOS telemetry present, PLLs locked, JESD OK, rails, temperatures, reference"),
    ("supply", "Supply current", "Bench supply current with RF off and RF on (PA bias)"),
    ("cw", "Carrier (CW)", "Carrier frequency, power, integrated phase noise, spurs"),
    ("bpsk", "BPSK modulation", "MER / EVM / frequency / clock / leakage / compression / spectrum"),
    ("oqpsk", "OQPSK modulation", "As BPSK plus I/Q gain imbalance and quadrature error"),
    ("att", "Attenuator (Atten_EIRP / ADRF5730)", "Step weights 1…16 dB from 11 settings, stuck / weak steps"),
    ("lin", "PA linearity", "Compression and ACLR at nominal and higher attenuation"),
    ("harm", "LO harmonics", "Lines at carrier × k/4 (LO and multiplier products)"),
    ("drift", "Stability / drift", "Power, frequency and MER over time vs. temperature"),
    ("shr", "ШР packet (despread + decode)", "Preambles despread (PRS 3), preamble value, despread SNR; at high "
                                            "rates the КБД is captured from the RF onset, despread (PRS 1 ⊕ 2) and "
                                            "turbo decoded"),
    ("ber", "Data / BER (УР)", "Decoded КБД collected until the bit count is reached: BER, RS corrections, CRC"),
]
TEST_NAME = {t: n for t, n, _ in TESTS}
DEFAULT_PLANS = {
    "Full test": ["pre", "supply", "cw", "bpsk", "oqpsk", "shr", "att", "lin", "harm", "ber"],
    "Quick check": ["pre", "cw", "oqpsk"],
    "Modulation only": ["bpsk", "oqpsk"],
    "Data decoding": ["bpsk", "shr", "ber"],
    "Attenuator": ["att"],
    "Burn-in (stability)": ["pre", "drift"],
}
CTL_MODES = [("rf_off", "RF off"), ("cw", "Carrier (CW)"), ("bpsk", "BPSK"), ("oqpsk", "OQPSK")]


def _ru_on():
    try:
        import i18n
        return i18n.CURRENT == "ru"
    except Exception:
        return False


def _ru(text):
    """Russian via the interface dictionary (i18n) and the diagnosis templates (bos_ru)."""
    try:
        import i18n
        return i18n.tr(text, "ru")
    except Exception:
        return text


def _tr(text):
    try:
        import i18n
        return i18n.tr(text, i18n.CURRENT)
    except Exception:
        return text


def _mono():
    f = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
    return f


def _fin(v):
    return isinstance(v, (int, float)) and math.isfinite(v)


def fmt(v, spec="{:.2f}"):
    return spec.format(v) if _fin(v) else "—"


# --------------------------------------------------------------------------- #
#  FSW capture (runs in the FSW worker thread)                                 #
# --------------------------------------------------------------------------- #
def fsw_capture(dev, center, fs, n, autolevel=True, ref_level=0.0):
    """Raw I/Q from the FSW I/Q Analyzer. Only commands already verified in fsw_vsa_monitor.py."""
    dev.select_app("iq")
    dev.write(f"FREQ:CENT {center:.12g}")
    dev.write(f"TRAC:IQ:SRAT {fs:.10g}")
    dev.write(f"TRAC:IQ:RLEN {int(n)}")
    if autolevel:
        dev.write("ADJ:LEV")
        dev.query("*OPC?")
    else:
        dev.write(f"DISP:WIND:TRAC:Y:SCAL:RLEV {ref_level:.4g}")
    dev.query("*OPC?")
    errs = dev.errors()
    fs_real = dev._qf("TRAC:IQ:SRAT?") or fs
    dev.iq_fs = fs_real
    dev.write("*CLS;:INIT:IMM;*OPC")
    dev._wait_sweep(lambda: False)
    raw = np.asarray(dev.inst.query_binary_values("TRAC:IQ:DATA:MEM?", datatype="f", is_big_endian=False,
                                                  container=np.array), dtype=np.float64)
    m = raw.size // 2 * 2
    return raw[0:m:2] + 1j * raw[1:m:2], fs_real, errs


def fsw_arm(dev, center, fs, n, ref_level, trig_level):
    """Arm a single I/Q capture triggered by the IF power (RF onset). Returns (fs, errors)."""
    dev.select_app("iq")
    dev.write(f"FREQ:CENT {center:.12g}")
    dev.write(f"TRAC:IQ:SRAT {fs:.10g}")
    dev.write(f"TRAC:IQ:RLEN {int(n)}")
    dev.write(f"DISP:WIND:TRAC:Y:SCAL:RLEV {ref_level:.4g}")
    dev.write("TRIG:SOUR IFP")
    dev.write(f"TRIG:LEV:IFP {trig_level:.4g}")
    dev.query("*OPC?")
    errs = dev.errors()
    fs_real = dev._qf("TRAC:IQ:SRAT?") or fs
    dev.iq_fs = fs_real
    dev.write("*CLS;:INIT:IMM;*OPC")
    return fs_real, errs


def fsw_fetch(dev):
    try:
        dev._wait_sweep(lambda: False)
        raw = np.asarray(dev.inst.query_binary_values("TRAC:IQ:DATA:MEM?", datatype="f", is_big_endian=False,
                                                      container=np.array), dtype=np.float64)
    finally:
        try:
            dev.write("TRIG:SOUR IMM")
        except Exception:
            pass
    m = raw.size // 2 * 2
    return raw[0:m:2] + 1j * raw[1:m:2]


# --------------------------------------------------------------------------- #
#  Block diagram                                                               #
# --------------------------------------------------------------------------- #
class BlockDiagram(QtWidgets.QWidget):
    """Signal chain; each block coloured by its suspicion score. Click = filter the findings."""
    clicked = QtCore.pyqtSignal(str)
    # block -> (column, row)
    POS = {"REF": (0, 0), "CLK": (1, 0), "FPGA": (2, 0), "DAC": (3, 0), "MIX": (4, 0), "ATT": (5, 0),
           "PA": (6, 0), "OUT": (7, 0), "LMX": (4, 1), "PWR": (6, 1)}
    LINKS = [("REF", "CLK"), ("CLK", "FPGA"), ("FPGA", "DAC"), ("DAC", "MIX"), ("MIX", "ATT"), ("ATT", "PA"),
             ("PA", "OUT"), ("LMX", "MIX"), ("REF", "LMX")]

    def __init__(self):
        super().__init__()
        self.score = {b: 0.0 for b in ba.BLOCK_IDS}
        self.tested = False
        self.selected = ""
        self.rects = {}
        self.setMinimumHeight(190)
        self.setMouseTracking(True)

    def set_scores(self, s, tested=True):
        self.score, self.tested = dict(s), tested
        self.update()

    def _color(self, b):
        if not self.tested:
            return QtGui.QColor("#21262d")
        s = self.score.get(b, 0)
        if s < 0.1:
            return QtGui.QColor("#1b4d33")
        if s < 0.35:
            return QtGui.QColor("#5c4a12")
        if s < 0.65:
            return QtGui.QColor("#7a3b10")
        return QtGui.QColor("#8b1e1e")

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cols, bw = 8, max(70, (w - 20) / 8 - 14)
        bh = min(64, (h - 30) / 2 - 10)
        self.rects = {}
        for b, (c, r) in self.POS.items():
            x = 10 + c * (w - 20) / cols + ((w - 20) / cols - bw) / 2
            y = 12 + r * (bh + 34)
            self.rects[b] = QtCore.QRectF(x, y, bw, bh)
        pen = QtGui.QPen(QtGui.QColor("#58a6ff"), 2)
        p.setPen(pen)
        for a, b in self.LINKS:
            ra, rb = self.rects[a], self.rects[b]
            if a == "REF" and b == "LMX":
                pts = [QtCore.QPointF(ra.center().x(), ra.bottom()), QtCore.QPointF(ra.center().x(), rb.center().y()),
                       QtCore.QPointF(rb.left(), rb.center().y())]
                p.drawPolyline(QtGui.QPolygonF(pts))
                continue
            if a == "LMX":
                p.drawLine(QtCore.QPointF(ra.center().x(), ra.top()), QtCore.QPointF(rb.center().x(), rb.bottom()))
                continue
            p.drawLine(QtCore.QPointF(ra.right(), ra.center().y()), QtCore.QPointF(rb.left(), rb.center().y()))
        lbl = QtGui.QPen(QtGui.QColor("#8a96a3"))
        f = p.font()
        for b, rect in self.rects.items():
            sel = b == self.selected
            p.setPen(QtGui.QPen(QtGui.QColor("#ffffff" if sel else "#30363d"), 2 if sel else 1))
            p.setBrush(self._color(b))
            p.drawRoundedRect(rect, 6, 6)
            name, part = next((x[1], x[2]) for x in ba.BLOCKS if x[0] == b)
            name = _tr(name)
            p.setPen(QtGui.QColor("#ffffff"))
            f.setBold(True)
            f.setPointSizeF(9)
            p.setFont(f)
            p.drawText(rect.adjusted(3, 3, -3, -rect.height() / 2), QtCore.Qt.AlignCenter, name)
            f.setBold(False)
            f.setPointSizeF(7.5)
            p.setFont(f)
            p.setPen(lbl)
            txt = part + (f"\n{self.score.get(b, 0) * 100:.0f} %" if self.tested else "")
            p.drawText(rect.adjusted(3, rect.height() / 2 - 6, -3, -2), QtCore.Qt.AlignHCenter | QtCore.Qt.AlignTop, txt)
        p.setPen(QtGui.QColor("#8a96a3"))
        f.setPointSizeF(8)
        p.setFont(f)
        p.drawText(QtCore.QRectF(0, h - 18, w - 8, 16), QtCore.Qt.AlignRight,
                   _tr("suspicion: green < 10 % · yellow · orange · red > 65 %   (click a block to filter the findings)"))

    def mousePressEvent(self, ev):
        for b, r in self.rects.items():
            if r.contains(ev.pos()):
                self.selected = "" if self.selected == b else b
                self.update()
                self.clicked.emit(self.selected)
                return

    def mouseMoveEvent(self, ev):
        for b, r in self.rects.items():
            if r.contains(ev.pos()):
                desc = next(x[3] for x in ba.BLOCKS if x[0] == b)
                self.setToolTip(f"<b>{ba.BLOCK_NAME[b]}</b><br>{desc}")
                return
        self.setToolTip("")


# --------------------------------------------------------------------------- #
#  Small dialogs                                                               #
# --------------------------------------------------------------------------- #
class PlanDialog(QtWidgets.QDialog):
    def __init__(self, parent, name, tests):
        super().__init__(parent)
        self.setWindowTitle("Test plan")
        f = QtWidgets.QFormLayout(self)
        self.name = QtWidgets.QLineEdit(name)
        f.addRow("Name", self.name)
        self.chk = {}
        for tid, tname, desc in TESTS:
            c = QtWidgets.QCheckBox(tname)
            c.setToolTip(desc)
            c.setChecked(tid in tests)
            f.addRow(c)
            self.chk[tid] = c
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def value(self):
        return self.name.text().strip(), [t for t, c in self.chk.items() if c.isChecked()]


# --------------------------------------------------------------------------- #
#  Station                                                                     #
# --------------------------------------------------------------------------- #
def run_busy(parent, text, fn):
    """Run fn(progress) in a background thread behind a modal progress dialog, so the window keeps
    repainting (no "not responding" on multi-GB files). progress(f) takes 0…1. Returns fn's result or
    re-raises its exception."""
    import threading
    box = {"p": 0.0}

    def prog(f):
        box["p"] = float(f)

    def run():
        try:
            box["r"] = fn(prog)
        except BaseException as e:                     # noqa: B902 - re-raised in the GUI thread
            box["e"] = e
    th = threading.Thread(target=run, daemon=True)
    th.start()
    dlg = QtWidgets.QProgressDialog(_tr(text), None, 0, 1000, parent)
    dlg.setWindowTitle("KTR TEST")
    dlg.setWindowModality(QtCore.Qt.WindowModal)
    dlg.setMinimumDuration(400)
    dlg.setCancelButton(None)
    t0 = time.time()
    while th.is_alive():
        QtWidgets.QApplication.processEvents(QtCore.QEventLoop.AllEvents, 40)
        dlg.setValue(int(min(1.0, box["p"]) * 1000))
        dlg.setLabelText(f"{_tr(text)}  ({time.time() - t0:.0f} s)")
        th.join(0.03)
    dlg.close()
    if "e" in box:
        raise box["e"]
    return box.get("r")


def envelope(x, nb, progress=None):
    """Mean and peak power (|x|²) in nb equal bins over the whole record, read in pieces."""
    n = len(x)
    b = max(1, n // nb)
    m = n // b * b
    nbin = m // b
    pmean = np.empty(nbin, np.float64)
    pmax = np.empty(nbin, np.float64)
    per = max(1, (1 << 23) // b)                      # bins per piece (~8 M samples)
    for i in range(0, nbin, per):
        e = min(nbin, i + per)
        v = np.asarray(x[i * b:e * b], np.complex64)
        p = (v.real ** 2 + v.imag ** 2).reshape(e - i, b)
        pmean[i:e] = p.mean(axis=1)
        pmax[i:e] = p.max(axis=1)
        if progress:
            progress(e / nbin)
    return b, pmean, pmax


class PlanView(QtWidgets.QWidget):
    """Column next to the settings: the selected plan step by step - commands to the BOS (CAN ID + data),
    FSW SCPI and capture sizes, supply / telemetry readings, operator dialogs, waits, PC analysis.
    Rebuilt whenever the plan or a setting changes; the running test is highlighted. Click a line with
    ▸ / ▾ to open or close its details."""

    def __init__(self, station):
        super().__init__()
        import bos_plan
        self.bp = bos_plan
        self.st = station
        self._sig = None
        self._plan = None
        self._desc = []
        self._open = set()                               # paths of opened nodes
        self._closed = set()                             # tests closed by the operator
        self._state = {}                                 # test index -> run / done
        self._follow = False
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        h = QtWidgets.QHBoxLayout()
        self.title = QtWidgets.QLabel()
        self.title.setStyleSheet("font-weight:600;")
        h.addWidget(self.title, 1)
        self.b_exp = QtWidgets.QToolButton()
        self.b_exp.setText("⊞")
        self.b_exp.clicked.connect(self.expand_all)
        self.b_col = QtWidgets.QToolButton()
        self.b_col.setText("⊟")
        self.b_col.clicked.connect(self.collapse_all)
        for b in (self.b_exp, self.b_col):
            b.setAutoRaise(True)
            h.addWidget(b)
        v.addLayout(h)
        self.view = QtWidgets.QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(self._clicked)
        self.view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        v.addWidget(self.view, 1)
        self.legend = QtWidgets.QLabel()
        self.legend.setWordWrap(True)
        v.addWidget(self.legend)
        self.timer = QtCore.QTimer(self, interval=1000, timeout=self.refresh)
        self.timer.start()

    @staticmethod
    def _lang():
        try:
            import i18n
            return i18n.CURRENT
        except Exception:
            return "en"

    def refresh(self, force=False):
        st = self.st
        if st.running and not force:
            return
        plan = st.plan_cb.currentText() if hasattr(st, "plan_cb") else st.data.get("plan", "")
        try:
            sig = json.dumps([plan, st.cfg, st.lim, st.ctl, st.data.get("supply"), st.data["plans"].get(plan),
                              self._lang(), st._is_demo()], sort_keys=True, default=str)
        except Exception:
            sig = None
        if sig == self._sig and not force:
            return
        if plan != self._plan:
            self._state = {}
            self._open = set()
            self._closed = set()
        self._plan = plan
        self._sig = sig
        try:
            self._desc = self.bp.describe(st, plan)
        except Exception as e:
            self._desc = [("", f"{type(e).__name__}: {e}", [])]
        self._render()

    # ---- expand / collapse
    def _clicked(self, url):
        p = url.toString().split(":", 1)[-1]
        if p.count(".") == 0:                           # a test: open by default, remember closing
            (self._closed.discard if p in self._closed else self._closed.add)(p)
        else:
            (self._open.discard if p in self._open else self._open.add)(p)
        self._render()

    def expand_all(self):
        self._closed = set()

        def walk(steps, path):
            for i, s in enumerate(steps):
                q = f"{path}.{i}"
                if s.children:
                    self._open.add(q)
                    walk(s.children, q)
        for k, (_, _, steps) in enumerate(self._desc):
            walk(steps, str(k))
        self._render()

    def collapse_all(self):
        self._open = set()
        self._closed = {str(k) for k in range(len(self._desc))}
        self._render()

    # ---- HTML
    def _render(self):
        bp_ = self.bp
        L = bp_.L
        plan = self._plan or ""
        self.title.setText(L(f"Plan sequence: {plan}", f"Последовательность плана: {plan}"))
        self.b_exp.setToolTip(L("Open all details", "Развернуть всё"))
        self.b_col.setToolTip(L("Close all", "Свернуть всё"))
        self.legend.setText(" ".join(f"<b style='color:{v[2]}'>{bp_.tag(k)}</b>" for k, v in bp_.KINDS.items()
                                     if k != "info"))
        self.legend.setToolTip("\n".join(f"{bp_.tag(k)} — {bp_.tag_meaning(k)}" for k, v in bp_.KINDS.items()
                                          if k != "info"))
        e = html.escape
        mono = "font-family:Consolas,'DejaVu Sans Mono',monospace;"
        rows = []
        k_test = -1
        for k, (tid, title, steps) in enumerate(self._desc):
            if tid:
                k_test += 1
                title = f"{k_test + 1}. {_tr(TEST_NAME.get(tid, tid))}"
                st = self._state.get(k_test)
                mark = "▶ " if st == "run" else "✓ " if st == "done" else ""
                bg = "background:#1f3a5f;" if st == "run" else ""
                n = bp_.count_steps(steps)
                cnt = L(f"{n} steps", f"шагов: {n}")
            else:
                mark, bg, cnt = "", "", ""
            opened = str(k) not in self._closed
            arrow = "▾" if opened else "▸"
            if tid and self._state.get(k_test) == "run":
                rows.append("<a name='run'></a>")
            rows.append(f"<table width='100%' cellspacing='0' cellpadding='2' style='margin-top:6px;{bg}'><tr>"
                        f"<td width='14'><a href='t:{k}' style='text-decoration:none;color:#c9d1d9'>{arrow}</a></td>"
                        f"<td><a href='t:{k}' style='text-decoration:none;color:#e6edf3'><b>{e(mark + title)}</b></a>"
                        f" <span style='color:{C_DIM}'>{e(cnt)}</span></td></tr></table>")
            if opened:
                self._rows(rows, steps, str(k), 1, mono)
        sb = self.view.verticalScrollBar()
        pos = sb.value()
        self.view.setHtml("<div style='font-size:9pt'>" + "".join(rows) + "</div>")
        if self._follow:
            self._follow = False
            self.view.scrollToAnchor("run")
        else:
            sb.setValue(pos)

    def _rows(self, rows, steps, path, depth, mono):
        bp_ = self.bp
        e = html.escape
        for i, s in enumerate(steps):
            q = f"{path}.{i}"
            col = bp_.KINDS.get(s.kind, bp_.KINDS["info"])[2]
            has = bool(s.children)
            opened = q in self._open
            arrow = (f"<a href='s:{q}' style='text-decoration:none;color:#c9d1d9'>{'▾' if opened else '▸'}</a>"
                     if has else "")
            txt = e(s.text)
            if has:
                txt = f"<a href='s:{q}' style='text-decoration:none;color:#c9d1d9'>{txt}</a>"
            is_code = s.kind == "info" and s.text.startswith(("0x", "ID ", "INST", "FREQ", "TRAC", "ADJ", "*", "UTIL",
                                                              "OUTP", "SOUR", "SYST", "DISP", "TRIG"))
            style = (mono + f"color:{C_DIM};") if is_code else (f"color:{C_DIM};" if s.kind == "info" else "")
            rows.append(f"<table width='100%' cellspacing='0' cellpadding='1'><tr>"
                        f"<td width='{14 * depth}'></td><td width='12'>{arrow}</td>"
                        f"<td width='44' style='color:{col};font-weight:600'>{e(bp_.tag(s.kind))}</td>"
                        f"<td style=\"{style}\">{txt}</td></tr></table>")
            if has and opened:
                self._rows(rows, s.children, q, depth + 1, mono)

    # ---- live state while a plan runs
    def mark(self, k, state):
        """Test k (0 = first test of the plan) is 'run' / 'done'. The running test is scrolled into view."""
        self._state[k] = state
        self._follow = state == "run"
        self._render()

    def reset_marks(self):
        self._state = {}
        self._render()


class FileDialog(QtWidgets.QDialog):
    """Sample rate / centre (from the file if present), signal type, rates."""

    def __init__(self, parent, path, cfg):
        super().__init__(parent)
        self.setWindowTitle("Analyze I/Q file")
        self.path = path
        f = QtWidgets.QFormLayout(self)
        f.addRow(QtWidgets.QLabel(f"<b>{os.path.basename(path)}</b>"))
        self.dtype = QtWidgets.QComboBox()
        self.dtype.addItems(["float32", "int16", "int32", "float64", "int8"])
        self.order = QtWidgets.QComboBox()
        self.order.addItem("I,Q pairs", "pairs")
        self.order.addItem("I block, then Q block", "blocks")
        raw = not path.lower().endswith((".tar", ".csv", ".txt", ".npy", ".npz"))
        if raw:
            f.addRow("Raw data type", self.dtype)
            f.addRow("Sample order", self.order)
        self.iq = None
        try:
            if not raw:
                self.iq = run_busy(parent, "Loading I/Q file …", lambda pr: bos_iqfile.load(path, progress=pr))
        except Exception as e:
            f.addRow(QtWidgets.QLabel(f"cannot read: {e}"))
        self.fs = QtWidgets.QDoubleSpinBox()
        self.fs.setRange(0, 1e5)
        self.fs.setDecimals(6)
        self.fs.setSuffix(" MS/s")
        self.fs.setValue((self.iq.fs or 0) / 1e6 if self.iq else 0)
        self.center = QtWidgets.QDoubleSpinBox()
        self.center.setRange(0, 100)
        self.center.setDecimals(9)
        self.center.setSuffix(" GHz")
        self.center.setValue(((self.iq.center if self.iq else None) or cfg["carrier"]) / 1e9)
        f.addRow("Sample rate", self.fs)
        f.addRow("Centre frequency", self.center)
        if self.iq:
            f.addRow("Samples", QtWidgets.QLabel(f"{len(self.iq.x):,}  ({self.iq.meta.get('format', '')})"))
        self.mode = QtWidgets.QComboBox()
        for k, txt in (("auto", "Auto (from the spectrum)"), ("cw", "Carrier (CW)"), ("bpsk", "УР - BPSK"),
                       ("oqpsk", "ШР - OQPSK")):
            self.mode.addItem(txt, k)
        f.addRow("Signal", self.mode)
        self.ur = QtWidgets.QComboBox()
        self.ur.addItem("auto (best MER)", None)
        for r in (8, 16, 32, 64, 128, 256, 512):
            self.ur.addItem(f"{r} kbit/s", r)
        self.shr = QtWidgets.QComboBox()
        self.shr.addItem("auto (search)", None)
        for r in (16, 32, 64, 128, 256, 512, 1024):
            self.shr.addItem(f"{r} kbit/s", r)
        f.addRow("УР rate", self.ur)
        f.addRow("ШР rate", self.shr)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def result_data(self):
        iq = self.iq or run_busy(self, "Loading I/Q file …", lambda pr: bos_iqfile.load(
            self.path, self.dtype.currentText(), self.order.currentData(), progress=pr))
        iq.fs = self.fs.value() * 1e6
        iq.center = self.center.value() * 1e9 or None
        if not iq.fs:
            raise ValueError("enter the sample rate")
        return iq, self.mode.currentData(), self.ur.currentData(), self.shr.currentData()


class SegmentDialog(QtWidgets.QDialog):
    """Power envelope of the whole recording with a draggable selection (like an audio editor):
    only the selected part is analysed, so noise before / after the transmission is left out."""

    def __init__(self, parent, iq):
        super().__init__(parent)
        self.setWindowTitle(f"Select the part to analyse - {iq.name}")
        self.resize(1100, 520)
        self.iq = iq
        self.n = len(iq.x)
        self.fs = iq.fs
        v = QtWidgets.QVBoxLayout(self)
        # envelope: mean and max power per bin (dBm), ≤ 4000 bins
        nb = min(4000, max(1, self.n // 16))
        self.bin, pm, px = run_busy(parent, "Reading the recording …", lambda pr: envelope(iq.x, nb, pr))
        self.pw_bins = pm / 50.0 * 1e3                    # mW per bin, for the selection power
        self.t = (np.arange(len(pm)) + 0.5) * self.bin / self.fs
        self.pmean = 10 * np.log10(self.pw_bins + 1e-20)
        pmax = 10 * np.log10(px / 50.0 * 1e3 + 1e-20)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("bottom", "time", units="s")
        self.plot.setLabel("left", "power", units="dBm")
        self.plot.getPlotItem().setMenuEnabled(False)
        self.plot.plot(self.t, pmax, pen=pg.mkPen("#2f6fb0", width=1))
        self.plot.plot(self.t, self.pmean, pen=pg.mkPen("#58a6ff", width=1.5))
        a, b = self._auto()
        self.region = pg.LinearRegionItem(values=(a / self.fs, b / self.fs), brush=pg.mkBrush(63, 185, 80, 50),
                                          pen=pg.mkPen("#3fb950", width=2))
        self.region.setBounds((0, self.n / self.fs))
        self.region.sigRegionChanged.connect(self._update)
        self.plot.addItem(self.region)
        v.addWidget(self.plot, 1)
        h = QtWidgets.QHBoxLayout()
        self.start = QtWidgets.QDoubleSpinBox()
        self.stop = QtWidgets.QDoubleSpinBox()
        for sb in (self.start, self.stop):
            sb.setDecimals(6)
            sb.setRange(0, self.n / self.fs)
            sb.setSuffix(" s")
            sb.setSingleStep(max(1e-6, self.n / self.fs / 1000))
            sb.valueChanged.connect(self._spin)
        self.info = QtWidgets.QLabel()
        for txt, fn in (("Auto (signal only)", self._set_auto), ("Whole recording", self._set_all),
                        ("Zoom to selection", self._zoom), ("Show all", lambda: self.plot.autoRange())):
            bt = QtWidgets.QPushButton(txt)
            bt.clicked.connect(fn)
            h.addWidget(bt)
        h.addWidget(QtWidgets.QLabel("Start"))
        h.addWidget(self.start)
        h.addWidget(QtWidgets.QLabel("Stop"))
        h.addWidget(self.stop)
        h.addWidget(self.info, 1)
        v.addLayout(h)
        v.addWidget(QtWidgets.QLabel("Drag the green edges (or the whole region) to choose the part to analyse; "
                                     "dark line = peak, light line = mean power per point. For a ШР packet keep the "
                                     "start of the transmission (preambles) inside the selection."))
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.button(QtWidgets.QDialogButtonBox.Ok).setText("Analyse selection")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._update()

    def _auto(self):
        """Samples where the mean power is within 10 dB of the strongest part (+ small margins)."""
        if self.pmean.size < 3:
            return 0, self.n
        on = np.where(self.pmean > np.percentile(self.pmean, 99) - 10)[0]
        if not on.size:
            return 0, self.n
        a = (on[0] + 1) * self.bin                         # skip the bin where the RF switches on
        b = on[-1] * self.bin
        return (a, b) if b - a > 1000 else (0, self.n)

    def _set_auto(self):
        a, b = self._auto()
        self.region.setRegion((a / self.fs, b / self.fs))

    def _set_all(self):
        self.region.setRegion((0, self.n / self.fs))

    def _zoom(self):
        a, b = self.region.getRegion()
        pad = (b - a) * 0.05
        self.plot.setXRange(a - pad, b + pad, padding=0)

    def _spin(self, *_):
        if self.start.value() < self.stop.value():
            self.region.blockSignals(True)
            self.region.setRegion((self.start.value(), self.stop.value()))
            self.region.blockSignals(False)
            self._update(spin=False)

    def selection(self):
        a, b = self.region.getRegion()
        return max(0, int(a * self.fs)), min(self.n, int(b * self.fs))

    def _update(self, *_, spin=True):
        a, b = self.selection()
        if spin:
            for sb, val in ((self.start, a / self.fs), (self.stop, b / self.fs)):
                sb.blockSignals(True)
                sb.setValue(val)
                sb.blockSignals(False)
        # mean power from the envelope bins (instant; the record may be GBs)
        i0, i1 = a // self.bin, max(a // self.bin + 1, -(-b // self.bin))
        sel = self.pw_bins[i0:i1]
        pw = 10 * math.log10(float(np.mean(sel)) + 1e-20) if b > a and sel.size else float("nan")
        self.info.setText(f"{(b - a) / self.fs:.6f} s, {b - a:,} samples, mean {pw:.2f} dBm")


class BosStation(QtWidgets.QWidget):
    job_done = QtCore.pyqtSignal(object)            # ("ok" | "err", result) of a job on another device's thread

    def __init__(self, host):
        super().__init__()
        self.host = host
        self.data = {"cfg": {k: d for k, _, _, d, _, _ in CFG_FIELDS},
                     "lim": {k: d for k, _, d, _ in LIM_FIELDS},
                     "ctl": {"method": "protocol", "sender": 0x18, "line": "BOS", "freq_n": 800, "rate_ur": 2, "rate_shr": 4,
                             "bpsk_cmd": 0xA1, "oqpsk_cmd": 0xA4, "time_snr": 255, "keepalive": True,
                             "tm_enable": True,
                             "cmd": {}, "att_mode": "prompt", "att_id": "", "att_data": "XX", "att_line": "BOS",
                             "settle_ms": 1500, "prompt": True,
                             "tfg_setup": True, "tfg_src": "EXT", "tfg_feed": 1, "power_up": "prompt",
                             "tfg_ref_apply": True, "tfg_ref_amp": 2.0, "tfg_ref_offs": 2.0,
                             "ref_wait_s": 30},
                     "supply": "none", "plans": dict(DEFAULT_PLANS), "plan": "Full test",
                     "unit": {"serial": "", "operator": ""}}
        try:
            with open(STORE_FILE, encoding="utf-8") as fh:
                d = json.load(fh)
            for k in ("cfg", "lim", "ctl", "unit"):
                self.data[k].update(d.get(k, {}))
            for k in ("supply", "plan"):
                self.data[k] = d.get(k, self.data[k])
            if d.get("plans"):
                self.data["plans"] = d["plans"]
            if d.get("lim_ver", 1) < 2:                   # Err_Ref_LO limit: was ±1000, now ≤ 3 before a mode
                if self.data["lim"].get("err_ref_lo") == 1000.0:
                    self.data["lim"]["err_ref_lo"] = 3.0
        except FileNotFoundError:
            pass
        except Exception as e:
            msg = f"BOS station: cannot read {STORE_FILE}: {e}"
            QtCore.QTimer.singleShot(0, lambda: self.log(msg, "err"))
        self.data["lim_ver"] = 2
        self.cfg, self.lim, self.ctl = self.data["cfg"], self.data["lim"], self.data["ctl"]
        self.sim = ba.BosSimulator(self.cfg)
        self.job_done.connect(self._on_ext_job)
        self._ref_failed = False
        self._gate_rows = set()
        self.demo = False
        # telemetry
        self.rx = telemetry.Reassembler() if telemetry else None
        self.tlm: dict = {}
        self.tlm_t = {"dop": None, "osn": None}
        # run state
        self.gen = None
        self.running = False
        self.abort_flag = False
        self.pending = None               # what the runner waits for
        self.res: dict = {}
        self.rows: list = []
        self.findings: list = []
        self.run_info: dict = {}
        self.verdict = ""
        self.on_done = None
        self.cur_test = ""
        self.live_mode = False
        self.last_report = ""
        self.cfg_rx = None                # (time, decoded ИОК_КОНФИГ)
        self.cur_mode = None              # mode the station put the unit in (protocol control)
        self.cur_att = 0
        self._ack_code = None
        self._build()
        self._wire_host()
        self.tick = QtCore.QTimer(self, interval=1000, timeout=self._tick)
        self.tick.start()
        self.wait_timer = QtCore.QTimer(self, singleShot=True, timeout=lambda: self._advance(None))
        self.ka_timer = QtCore.QTimer(self, interval=500, timeout=self._keepalive)
        self.ka_timer.start()

    # ================================================================ build
    def _build(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        # ---- top bar
        bar = QtWidgets.QHBoxLayout()
        self.serial = QtWidgets.QLineEdit(self.data["unit"].get("serial", ""))
        self.serial.setPlaceholderText("unit serial number")
        self.serial.setMaximumWidth(170)
        self.operator = QtWidgets.QLineEdit(self.data["unit"].get("operator", ""))
        self.operator.setPlaceholderText("operator")
        self.operator.setMaximumWidth(140)
        self.plan_cb = QtWidgets.QComboBox()
        self.plan_cb.setMinimumWidth(170)
        self._fill_plans()
        self.plan_cb.currentTextChanged.connect(self._plan_changed)
        plan_menu = QtWidgets.QToolButton()
        plan_menu.setText("Plans ▾")
        plan_menu.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        m = QtWidgets.QMenu(plan_menu)
        m.addAction("Edit plan…", self.edit_plan)
        m.addAction("New plan…", self.new_plan)
        m.addAction("Delete plan", self.delete_plan)
        plan_menu.setMenu(m)
        self.run_btn = QtWidgets.QPushButton("▶ Run test plan")
        self.run_btn.setStyleSheet("font-weight:700;padding:6px 14px;")
        self.run_btn.clicked.connect(self.toggle_run)
        self.live_btn = QtWidgets.QPushButton("◎ Live diagnose")
        self.live_btn.setCheckable(True)
        self.live_btn.setToolTip("Capture and diagnose continuously what the unit transmits now (no unit control):\n"
                                 "useful while adjusting or probing the board")
        self.live_btn.toggled.connect(self.toggle_live)
        self.live_mod = QtWidgets.QComboBox()
        self.live_mod.addItems(["OQPSK", "BPSK", "CW"])
        self.live_mod.setToolTip("What the unit is transmitting during live diagnose")
        self.file_btn = QtWidgets.QPushButton("📂 Analyze I/Q file…")
        self.file_btn.setToolTip("Load an FSW I/Q recording (.iq.tar, .iqw, .csv, raw) and diagnose it like a capture")
        self.file_btn.clicked.connect(self.analyze_file)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setFixedWidth(150)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%v/%m")
        self.verdict_lbl = QtWidgets.QLabel("NOT TESTED")
        self.verdict_lbl.setAlignment(QtCore.Qt.AlignCenter)
        self.verdict_lbl.setMinimumWidth(150)
        self._show_verdict("")
        for w in (QtWidgets.QLabel("Unit"), self.serial, self.operator, QtWidgets.QLabel("Plan"), self.plan_cb,
                  plan_menu, self.run_btn, self.live_btn, self.live_mod, self.file_btn, self.progress):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(self.verdict_lbl)
        root.addLayout(bar)
        self.step_lbl = QtWidgets.QLabel("Ready. Connect the FSW (Spectrum Analyzer tab) and the BOS CAN line (CAN tab).")
        self.step_lbl.setStyleSheet(f"color:{C_DIM};")
        root.addWidget(self.step_lbl)

        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        root.addWidget(split, 1)
        # ---- left: setup
        left = QtWidgets.QTabWidget()
        left.addTab(self._scroll(self._build_signal()), "Unit / signal")
        left.addTab(self._scroll(self._build_limits()), "Limits")
        left.addTab(self._scroll(self._build_control()), "Unit control")
        self.sim_page = self._scroll(self._build_sim())
        left.addTab(self.sim_page, "Simulator")
        left.setMinimumWidth(400)
        split.addWidget(left)
        # ---- middle: what the selected plan does, step by step
        self.plan_view = PlanView(self)
        self.plan_view.setMinimumWidth(260)
        split.addWidget(self.plan_view)
        # ---- right: results
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_overview(), "Diagnosis")
        self.tabs.addTab(self._build_results(), "Measurements")
        self.tabs.addTab(self._build_signal_view(), "Signal")
        self.tabs.addTab(self._build_att_view(), "Attenuator")
        self.tabs.addTab(self._build_frames_view(), "Frames")
        self.tabs.addTab(self._build_tlm_view(), "BOS telemetry")
        self.tabs.addTab(self._build_corr_view(), "Corrections")
        self.tabs.addTab(self._build_history(), "History")
        split.addWidget(self.tabs)
        split.setCollapsible(1, True)
        self._split = split
        QtCore.QTimer.singleShot(0, self._split_sizes)

    def _split_sizes(self):
        w = max(1200, self._split.width())
        self._split.setSizes([int(w * 0.24), int(w * 0.28), w - int(w * 0.24) - int(w * 0.28)])

    @staticmethod
    def _scroll(w):
        sa = QtWidgets.QScrollArea()
        sa.setWidget(w)
        sa.setWidgetResizable(True)
        sa.setFrameShape(QtWidgets.QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        return sa

    def _build_signal(self):
        w = QtWidgets.QWidget()
        f = QtWidgets.QFormLayout(w)
        self.cfg_w = {}
        for key, label, kind, _, unit, tip in CFG_FIELDS:
            v = self.cfg.get(key)
            if kind == "b":
                wdg = QtWidgets.QCheckBox()
                wdg.setChecked(bool(v))
                wdg.toggled.connect(lambda *_: self._collect())
            elif kind == "i":
                wdg = QtWidgets.QSpinBox()
                wdg.setRange(0, 10_000_000)
                wdg.setValue(int(v))
                if unit:
                    wdg.setSuffix(f" {unit}")
                wdg.valueChanged.connect(lambda *_: self._collect())
            else:
                sc = UNITS.get(unit, 1.0)
                wdg = QtWidgets.QDoubleSpinBox()
                wdg.setDecimals(9 if unit == "GHz" else 4)
                wdg.setRange(-1e6, 1e9)
                wdg.setValue(float(v) / sc)
                if unit:
                    wdg.setSuffix(f" {unit}")
                wdg.valueChanged.connect(lambda *_: self._collect())
            wdg.setToolTip(tip)
            f.addRow(label, wdg)
            self.cfg_w[key] = (wdg, kind, UNITS.get(unit, 1.0))
        sup = QtWidgets.QComboBox()
        sup.addItem("none", "none")
        for ch in (1, 2, 3, 4):
            sup.addItem(f"HMP4040 CH{ch}", f"hmp:{ch}")
        for ch in (1, 2, 3, 4):
            sup.addItem(f"N6701C CH{ch}", f"n67:{ch}")
        sup.setCurrentIndex(max(0, sup.findData(self.data.get("supply", "none"))))
        sup.setToolTip("Bench supply output that powers the BOS (supply current test, PA bias check)")
        sup.currentIndexChanged.connect(lambda *_: self._collect())
        self.supply_cb = sup
        f.addRow("BOS supply output", sup)
        return w

    def _build_limits(self):
        w = QtWidgets.QWidget()
        f = QtWidgets.QFormLayout(w)
        self.lim_w = {}
        for key, label, _, unit in LIM_FIELDS:
            s = QtWidgets.QDoubleSpinBox()
            s.setRange(-1e6, 1e6)
            s.setDecimals(3)
            s.setValue(float(self.lim.get(key)))
            if unit:
                s.setSuffix(f" {unit}")
            s.valueChanged.connect(lambda *_: self._collect())
            f.addRow(label, s)
            self.lim_w[key] = s
        b = QtWidgets.QPushButton("Restore default limits")
        b.clicked.connect(self._default_limits)
        f.addRow(b)
        return w

    def _build_control(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        g = QtWidgets.QGroupBox("Control method")
        f = QtWidgets.QFormLayout(g)
        self.method = QtWidgets.QComboBox()
        for k, txt in (("protocol", "BOS protocol (ПИЛВ, direct CAN commands)"),
                       ("library", "Commands from the CAN library"), ("prompt", "Ask the operator")):
            self.method.addItem(txt, k)
        self.method.setCurrentIndex(max(0, self.method.findData(self.ctl.get("method", "protocol"))))
        self.method.currentIndexChanged.connect(lambda *_: (self._collect(), self._method_changed()))
        f.addRow(self.method)
        v.addWidget(g)
        self.proto_box = QtWidgets.QGroupBox("BOS protocol")
        f = QtWidgets.QFormLayout(self.proto_box)
        self.p_sender = QtWidgets.QComboBox()
        for a, name in bp.SENDERS:
            self.p_sender.addItem(f"0x{a:02X}  {name}", a)
        self.p_sender.setCurrentIndex(max(0, self.p_sender.findData(self.ctl.get("sender", 0x18))))
        self.p_sender.setToolTip("Sender address of the station. The BOS executes commands only from allowed\n"
                                 "addresses and answers КВ- otherwise - then try ВМ 1 (0x20).")
        self.p_line = QtWidgets.QLineEdit(self.ctl.get("line", "BOS"))
        self.p_line.setToolTip("CAN line (CAN tab) the BOS is connected to")
        self.p_freq = QtWidgets.QSpinBox()
        self.p_freq.setRange(656, 944)
        self.p_freq.setValue(int(self.ctl.get("freq_n", 800)))
        self.p_freq.setToolTip("Freq = 37650 + 0.125·N MHz. 656…944 is valid for every mode (carrier / УР allow 8…1592)")
        self.p_freq_lbl = QtWidgets.QLabel()
        self.p_bpsk = QtWidgets.QComboBox()
        self.p_oqpsk = QtWidgets.QComboBox()
        for cb, key in ((self.p_bpsk, "bpsk_cmd"), (self.p_oqpsk, "oqpsk_cmd")):
            for code, name in bp.SR_COMMANDS.items():
                if code != 0xA0:
                    cb.addItem(f"0x{code:02X}  {name}", code)
            cb.setCurrentIndex(max(0, cb.findData(self.ctl.get(key))))
            cb.setToolTip("Session command whose signal has this modulation")
        self.p_rate_ur, self.p_rate_shr = QtWidgets.QComboBox(), QtWidgets.QComboBox()
        for cb, key, rates in ((self.p_rate_ur, "rate_ur", range(0, 6)), (self.p_rate_shr, "rate_shr", range(1, 8))):
            for k in rates:
                cb.addItem(f"{bp.RATES[k]} kbit/s", k)
            cb.setCurrentIndex(max(0, cb.findData(self.ctl.get(key, 2))))
        self.p_rate_ur.setToolTip("УР (BPSK) data rate; symbol rate = 2 × data rate (set automatically)")
        self.p_rate_shr.setToolTip("ШР (OQPSK) data rate; the symbol rate stays 131.072 Msym/s")
        self.p_tsnr = QtWidgets.QSpinBox()
        self.p_tsnr.setRange(0, 255)
        self.p_tsnr.setSuffix(" s")
        self.p_tsnr.setSpecialValueText("don't change")
        self.p_tsnr.setValue(int(self.ctl.get("time_snr", 255)))
        self.p_tsnr.setToolTip("SET_Time_SNR before the test: the BOS ends a session after this time\n"
                               "(factory default 30 s). Not saved to flash.")
        self.p_keep = QtWidgets.QCheckBox("Send target designation every 0.5 s (0°, 0°)")
        self.p_keep.setChecked(bool(self.ctl.get("keepalive", True)))
        self.p_keep.setToolTip("The BOS transmits only while it has a valid target designation\n"
                               "(Angle_ErdStation_BKU, valid for 3 s)")
        self.p_tm = QtWidgets.QCheckBox("Enable ИОК_ДОП / ОСН / СОСТОЯНИЕ and read ИОК_КОНФИГ")
        self.p_tm.setChecked(bool(self.ctl.get("tm_enable", True)))
        for wd in (self.p_sender, self.p_bpsk, self.p_oqpsk, self.p_rate_ur, self.p_rate_shr):
            wd.currentIndexChanged.connect(lambda *_: self._collect())
        for wd in (self.p_freq, self.p_tsnr):
            wd.valueChanged.connect(lambda *_: self._collect())
        for wd in (self.p_keep, self.p_tm):
            wd.toggled.connect(lambda *_: self._collect())
        self.p_line.editingFinished.connect(self._collect)
        f.addRow("Sender address", self.p_sender)
        f.addRow("CAN line", self.p_line)
        f.addRow("Frequency code N", self.p_freq)
        f.addRow("", self.p_freq_lbl)
        f.addRow("BPSK signal from", self.p_bpsk)
        f.addRow("OQPSK signal from", self.p_oqpsk)
        f.addRow("УР (BPSK) rate", self.p_rate_ur)
        f.addRow("ШР (OQPSK) rate", self.p_rate_shr)
        f.addRow("Session limit", self.p_tsnr)
        f.addRow(self.p_keep)
        f.addRow(self.p_tm)
        hb = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Test link")
        b.setToolTip("NOP, standby, telemetry setup, read the stored КОНФИГ")
        b.clicked.connect(lambda: self.start_plan("(link test)", tests=["link"]))
        hb.addWidget(b)
        b = QtWidgets.QPushButton("RF off (standby)")
        b.clicked.connect(lambda: self._send_now(0x01))
        hb.addWidget(b)
        f.addRow(hb)
        v.addWidget(self.proto_box)
        g = QtWidgets.QGroupBox("Modes (commands from the CAN library)")
        self.lib_box = g
        f = QtWidgets.QFormLayout(g)
        self.ctl_w = {}
        for key, label in CTL_MODES:
            cb = QtWidgets.QComboBox()
            cb.setToolTip("CAN library command that switches the BOS to this mode.\n"
                          "'(ask operator)' shows a prompt instead.")
            cb.currentIndexChanged.connect(lambda *_: self._collect())
            f.addRow(label, cb)
            self.ctl_w[key] = cb
        rb = QtWidgets.QPushButton("Reload CAN library")
        rb.clicked.connect(self._fill_ctl)
        f.addRow(rb)
        v.addWidget(g)
        g = QtWidgets.QGroupBox("Attenuator control (library / operator method)")
        self.att_box = g
        f = QtWidgets.QFormLayout(g)
        self.att_mode = QtWidgets.QComboBox()
        for k, t in (("prompt", "Ask the operator"), ("can", "CAN command template"), ("none", "Not adjustable (skip)")):
            self.att_mode.addItem(t, k)
        self.att_mode.setCurrentIndex(max(0, self.att_mode.findData(self.ctl.get("att_mode", "prompt"))))
        self.att_id = QtWidgets.QLineEdit(self.ctl.get("att_id", ""))
        self.att_id.setPlaceholderText("29-bit ID hex, e.g. 1A4612A0")
        self.att_data = QtWidgets.QLineEdit(self.ctl.get("att_data", "XX"))
        self.att_data.setPlaceholderText("data hex, XX = attenuation in dB (0…20), e.g. 05 XX 00")
        self.att_data.setToolTip("Data bytes in hex; XX is replaced by the attenuation in dB.")
        self.att_line = QtWidgets.QLineEdit(self.ctl.get("att_line", "BOS"))
        for wd in (self.att_mode,):
            wd.currentIndexChanged.connect(lambda *_: self._collect())
        for wd in (self.att_id, self.att_data, self.att_line):
            wd.editingFinished.connect(self._collect)
        f.addRow("Control", self.att_mode)
        f.addRow("CAN ID", self.att_id)
        f.addRow("Data", self.att_data)
        f.addRow("CAN line", self.att_line)
        v.addWidget(g)
        g = QtWidgets.QGroupBox("10 MHz reference (TFG6806) and BOS power-up")
        f = QtWidgets.QFormLayout(g)
        self.r_tfg = QtWidgets.QCheckBox("Set up the TFG6806 before the plan")
        self.r_tfg.setChecked(bool(self.ctl.get("tfg_setup", True)))
        self.r_tfg.setToolTip("First step of every plan, before the BOS gets power")
        self.r_src = QtWidgets.QComboBox()
        for k, txt in (("EXT", "External (UTILity:CLOck:SOURce EXTernal)"),
                       ("INT", "Internal (UTILity:CLOck:SOURce INTernal)"), ("", "don't change")):
            self.r_src.addItem(txt, k)
        self.r_src.setCurrentIndex(max(0, self.r_src.findData(self.ctl.get("tfg_src", "EXT"))))
        self.r_src.setToolTip("Reference of the TFG6806 itself (rear 10 MHz In/Out port). With 'External' the TFG\n"
                              "switches to an external clock only when it finds a qualified one and falls back to\n"
                              "its internal clock automatically when it is missing or removed (Users' Guide 3.24).")
        self.r_feed = QtWidgets.QComboBox()
        for k, txt in ((1, "TFG CH1 output"), (2, "TFG CH2 output"),
                       (0, "TFG rear 10 MHz Out (internal clock)"), (-1, "don't touch the TFG outputs")):
            self.r_feed.addItem(txt, k)
        self.r_feed.setCurrentIndex(max(0, self.r_feed.findData(int(self.ctl.get("tfg_feed", 1)))))
        self.r_feed.setToolTip("Where the BOS 10 MHz comes from: that output is switched on (its frequency must be\n"
                               "10 MHz - waveform and amplitude are not changed). The rear port can only output\n"
                               "the internal clock (UTILity:INSide:CLOck ON), so it needs clock source 'Internal'.")
        self.r_std = QtWidgets.QCheckBox("Set the standard 10 MHz on that output (sine, 10 MHz)")
        self.r_std.setChecked(bool(self.ctl.get("tfg_ref_apply", True)))
        self.r_std.setToolTip("Before its output is switched on: waveform sine, continuous, 10 MHz,\n"
                              "amplitude and offset below (standard for the BOS external reference input)")
        self.r_amp = QtWidgets.QDoubleSpinBox()
        self.r_amp.setRange(0.01, 10.0)
        self.r_amp.setDecimals(2)
        self.r_amp.setSuffix(" Vpp")
        self.r_amp.setValue(float(self.ctl.get("tfg_ref_amp", 2.0)))
        self.r_offs = QtWidgets.QDoubleSpinBox()
        self.r_offs.setRange(-5.0, 5.0)
        self.r_offs.setDecimals(2)
        self.r_offs.setSuffix(" V")
        self.r_offs.setValue(float(self.ctl.get("tfg_ref_offs", 2.0)))
        self.r_pwr = QtWidgets.QComboBox()
        for k, txt in (("prompt", "Ask the operator to switch it on"),
                       ("supply", "Switch the selected supply output on"),
                       ("none", "Already on (check only)")):
            self.r_pwr.addItem(txt, k)
        self.r_pwr.setCurrentIndex(max(0, self.r_pwr.findData(self.ctl.get("power_up", "prompt"))))
        self.r_pwr.setToolTip("After the TFG: power the BOS. 'Supply' switches on the output chosen on the\n"
                              "Unit / signal page with the voltage / current set on its tab (HMP4040: + master).")
        self.r_wait = QtWidgets.QSpinBox()
        self.r_wait.setRange(3, 600)
        self.r_wait.setSuffix(" s")
        self.r_wait.setValue(int(self.ctl.get("ref_wait_s", 30)))
        self.r_wait.setToolTip("Before every mode command the station waits up to this long for\n"
                               "PulsNumb_10MHz = 10 000 000 ± tol and |Err_Ref_LO| ≤ limit (Limits page)")
        self.r_tfg.toggled.connect(lambda *_: self._collect())
        self.r_std.toggled.connect(lambda *_: self._collect())
        for wd in (self.r_amp, self.r_offs):
            wd.valueChanged.connect(lambda *_: self._collect())
        for wd in (self.r_src, self.r_feed, self.r_pwr):
            wd.currentIndexChanged.connect(lambda *_: self._collect())
        self.r_wait.valueChanged.connect(lambda *_: self._collect())
        f.addRow(self.r_tfg)
        f.addRow("TFG clock source", self.r_src)
        f.addRow("BOS 10 MHz from", self.r_feed)
        f.addRow(self.r_std)
        f.addRow("Amplitude", self.r_amp)
        f.addRow("Offset", self.r_offs)
        f.addRow("BOS power", self.r_pwr)
        f.addRow("Reference wait", self.r_wait)
        v.addWidget(g)
        g = QtWidgets.QGroupBox("Timing")
        f = QtWidgets.QFormLayout(g)
        self.settle = QtWidgets.QSpinBox()
        self.settle.setRange(0, 60000)
        self.settle.setSuffix(" ms")
        self.settle.setValue(int(self.ctl.get("settle_ms", 1500)))
        self.settle.setToolTip("Wait after every mode / attenuator change before measuring")
        self.settle.valueChanged.connect(lambda *_: self._collect())
        f.addRow("Settle time", self.settle)
        v.addWidget(g)
        v.addWidget(QtWidgets.QLabel("Library method: without a mapped command the station asks\n"
                                     "the operator to switch the unit (semi-automatic test)."))
        v.addStretch(1)
        self._fill_ctl()
        self._method_changed()
        return w

    def _method_changed(self):
        m = self.ctl.get("method", "protocol")
        self.proto_box.setVisible(m == "protocol")
        self.lib_box.setVisible(m == "library")
        self.att_box.setVisible(m != "protocol")
        if "carrier" in getattr(self, "cfg_w", {}):
            self.cfg_w["carrier"][0].setEnabled(m != "protocol")
        self._sync_carrier()

    def _sync_carrier(self):
        n = int(self.ctl.get("freq_n", 800))
        if hasattr(self, "p_freq_lbl"):
            self.p_freq_lbl.setText(f"= {bp.freq_hz(n) / 1e9:.6f} GHz")
        proto = self.ctl.get("method") == "protocol"
        if proto and "carrier" in getattr(self, "cfg_w", {}):
            self.cfg["carrier"] = bp.freq_hz(n)
            # BPSK = УР: 2 × data rate; OQPSK = ШР: 131.072 Msym/s
            ur = self.ctl.get("bpsk_cmd", 0xA1) == 0xA1
            if ur:
                self.cfg["sr_bpsk"] = 2e3 * bp.RATES[int(self.ctl.get("rate_ur", 2))]
            self.cfg["sr_oqpsk"] = SHR_SYMBOL_RATE
            for key in ("carrier", "sr_bpsk", "sr_oqpsk"):
                wdg, _, sc = self.cfg_w[key]
                wdg.blockSignals(True)
                wdg.setValue(self.cfg[key] / sc)
                wdg.blockSignals(False)
        for key in ("sr_bpsk", "sr_oqpsk"):
            if key in getattr(self, "cfg_w", {}):
                self.cfg_w[key][0].setEnabled(not proto)

    def _build_sim(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        lab = QtWidgets.QLabel("Faults of the simulated BOS (demo mode only: start with --demo).\n"
                               "Use it to train operators and to see what each fault looks like.")
        lab.setWordWrap(True)
        v.addWidget(lab)
        g = QtWidgets.QGridLayout()
        self.fault_w = {}
        for r, (key, label, default, unit) in enumerate(ba.FAULTS):
            c = QtWidgets.QCheckBox(label)
            s = QtWidgets.QDoubleSpinBox()
            s.setRange(-1e6, 1e6)
            s.setDecimals(5 if unit == "BER" else 2)
            s.setValue(default)
            s.setSuffix(f" {unit}" if unit else "")
            s.setVisible(bool(unit))
            c.toggled.connect(self._faults_changed)
            s.valueChanged.connect(self._faults_changed)
            g.addWidget(c, r, 0)
            g.addWidget(s, r, 1)
            self.fault_w[key] = (c, s)
        v.addLayout(g)
        b = QtWidgets.QPushButton("Clear all faults")
        b.clicked.connect(lambda: [c.setChecked(False) for c, _ in self.fault_w.values()])
        v.addWidget(b)
        b = QtWidgets.QPushButton("Random fault (blind test)")
        b.setToolTip("Inject one random fault without showing which - run the plan and see if the diagnosis finds it")
        b.clicked.connect(self._random_fault)
        v.addWidget(b)
        self.blind_lbl = QtWidgets.QLabel("")
        self.blind_lbl.setWordWrap(True)
        v.addWidget(self.blind_lbl)
        v.addStretch(1)
        return w

    def _build_overview(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(2, 2, 2, 2)
        self.diagram = BlockDiagram()
        self.diagram.clicked.connect(self._filter_findings)
        v.addWidget(self.diagram)
        h = QtWidgets.QHBoxLayout()
        self.find_hdr = QtWidgets.QLabel("<b>Findings</b>")
        h.addWidget(self.find_hdr)
        h.addStretch(1)
        self.report_btn = QtWidgets.QPushButton("Open last report")
        self.report_btn.setEnabled(False)
        self.report_btn.clicked.connect(self._open_report)
        h.addWidget(self.report_btn)
        v.addLayout(h)
        sp = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.find_list = QtWidgets.QListWidget()
        self.find_list.setWordWrap(True)
        self.find_list.currentRowChanged.connect(self._show_finding)
        sp.addWidget(self.find_list)
        self.find_detail = QtWidgets.QTextBrowser()
        self.find_detail.setOpenExternalLinks(False)
        sp.addWidget(self.find_detail)
        sp.setSizes([520, 520])
        v.addWidget(sp, 1)
        return w

    def _build_results(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(2, 2, 2, 2)
        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Test", "Parameter", "Value", "Unit", "Limit", "Result"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        for c, wd in enumerate((150, 260, 110, 60, 150)):
            self.table.setColumnWidth(c, wd)
        self.table.horizontalHeader().setStretchLastSection(True)
        v.addWidget(self.table)
        return w

    def _plot(self, title, xl="", yl=""):
        p = pg.PlotWidget(title=title)
        p.showGrid(x=True, y=True, alpha=0.25)
        p.getPlotItem().setMenuEnabled(False)
        if xl:
            p.setLabel("bottom", xl)
        if yl:
            p.setLabel("left", yl)
        return p

    def _build_signal_view(self):
        w = QtWidgets.QWidget()
        g = QtWidgets.QGridLayout(w)
        g.setContentsMargins(2, 2, 2, 2)
        self.const = self._plot("Constellation (after carrier / clock recovery)")
        self.const.setAspectLocked(True)
        self.const_pts = pg.ScatterPlotItem(size=3, pen=None, brush=pg.mkBrush(255, 196, 0, 140))
        self.const.addItem(self.const_pts)
        self.const.setRange(xRange=(-1.6, 1.6), yRange=(-1.6, 1.6))
        self.spec = self._plot("Spectrum of the capture", "offset from centre, Hz", "dB")
        self.spec_curve = self.spec.plot([], [], pen=pg.mkPen("#58a6ff", width=1))
        self.phase = self._plot("Phase error per symbol", "symbol", "deg")
        self.phase_curve = self.phase.plot([], [], pen=pg.mkPen("#f0883e", width=1))
        self.phase.getAxis("left").enableAutoSIPrefix(False)
        self.sig_info = QtWidgets.QTextBrowser()
        self.sig_info.setFont(_mono())
        self.sig_src = QtWidgets.QComboBox()
        self.sig_src.currentIndexChanged.connect(self._draw_signal)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Capture"))
        top.addWidget(self.sig_src, 1)
        g.addLayout(top, 0, 0, 1, 2)
        g.addWidget(self.const, 1, 0)
        g.addWidget(self.spec, 1, 1)
        g.addWidget(self.phase, 2, 0)
        g.addWidget(self.sig_info, 2, 1)
        g.setRowStretch(1, 3)
        g.setRowStretch(2, 2)
        return w

    def _build_att_view(self):
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        self.att_plot = self._plot("Output power vs attenuator code", "code", "dBm")
        self.att_meas = pg.ScatterPlotItem(size=8, brush=pg.mkBrush("#f0b03e"), pen=None, name="measured")
        self.att_nom = self.att_plot.plot([], [], pen=pg.mkPen("#58a6ff", width=1, style=QtCore.Qt.DashLine))
        self.att_plot.addItem(self.att_meas)
        h.addWidget(self.att_plot, 3)
        self.att_table = QtWidgets.QTableWidget(6, 4)
        self.att_table.setHorizontalHeaderLabels(["Nominal", "Measured", "Error", "State"])
        self.att_table.setVerticalHeaderLabels([f"bit {b}" for b in range(6)])
        self.att_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.att_table.horizontalHeader().setStretchLastSection(True)
        h.addWidget(self.att_table, 2)
        return w

    def _build_tlm_view(self):
        w = QtWidgets.QWidget()
        g = QtWidgets.QGridLayout(w)
        self.tlm_w = {}
        items = [("pll_lmk", "Clock synth PLL (LMK)"), ("pll_lmx (DAC)", "DAC clock PLL"),
                 ("pll_lmx (mod)", "LO synth LMX2594"), ("pll_jesd", "FPGA JESD PLL"), ("pll_all", "FPGA PLLs"),
                 ("jesd_err", "JESD204B link"), ("Stat_10MHz", "External 10 MHz"), ("Sour_10MHz", "Reference source"),
                 ("vdd_1v", "FPGA 1.0 V"), ("vdd_1_8v", "FPGA 1.8 V"), ("vdd_2_5v", "FPGA 2.5 V"),
                 ("Temp_DT1", "Temperature 1"), ("Temp_DT2", "Temperature 2"), ("Temp_DT3", "Temperature 3"),
                 ("Err_Ref_LO", "Err_Ref_LO"), ("Mode", "Mode"), ("Mode_SNR", "Sub-mode"),
                 ("Soft_MORE_Ver", "FPGA firmware")]
        for k, (key, label) in enumerate(items):
            r, c = k % 9, (k // 9) * 3
            led = QtWidgets.QLabel("●")
            led.setStyleSheet(f"color:{C_DIM};font-size:16px;")
            val = QtWidgets.QLabel("—")
            val.setFont(_mono())
            g.addWidget(led, r, c)
            g.addWidget(QtWidgets.QLabel(f"<b>{label}</b>"), r, c + 1)
            g.addWidget(val, r, c + 2)
            self.tlm_w[key] = (led, val)
        self.tlm_age = QtWidgets.QLabel("no BOS telemetry yet")
        self.tlm_age.setStyleSheet(f"color:{C_DIM};")
        g.addWidget(self.tlm_age, 10, 0, 1, 6)
        g.setColumnStretch(2, 1)
        g.setColumnStretch(5, 1)
        g.setRowStretch(11, 1)
        return w

    def _build_corr_view(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        lb = QtWidgets.QLabel("Suggested adjustments from the last run. Register scaling factors are on the "
                              "'Unit / signal' page - check them against the DAC38J82 datasheet before writing.")
        lb.setWordWrap(True)
        v.addWidget(lb)
        self.corr = QtWidgets.QTableWidget(0, 3)
        self.corr.setHorizontalHeaderLabels(["Adjustment", "Value", "How"])
        self.corr.verticalHeader().hide()
        self.corr.setWordWrap(True)
        self.corr.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.corr.setColumnWidth(0, 280)
        self.corr.setColumnWidth(1, 200)
        self.corr.horizontalHeader().setStretchLastSection(True)
        self.corr.verticalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        v.addWidget(self.corr, 1)
        b = QtWidgets.QPushButton("Copy to clipboard")
        b.clicked.connect(self._copy_corr)
        v.addWidget(b, 0, QtCore.Qt.AlignLeft)
        return w

    def _build_frames_view(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(2, 2, 2, 2)
        h = QtWidgets.QHBoxLayout()
        self.fr_info = QtWidgets.QLabel("No decoded frames yet - run a BPSK / ШР test or analyze an I/Q file.")
        self.fr_info.setWordWrap(True)
        self.fr_info.setMinimumWidth(120)
        h.addWidget(self.fr_info, 1)
        self.fr_hide_idle = QtWidgets.QCheckBox("Hide idle frames")
        self.fr_hide_idle.toggled.connect(lambda *_: self._fill_frames())
        h.addWidget(self.fr_hide_idle)
        v.addLayout(h)
        h = QtWidgets.QHBoxLayout()                       # second row: keeps the page narrow enough
        for txt, fn in (("Save frames (.bin)", self._save_frames_bin), ("Save list (.csv)", self._save_frames_csv),
                        ("Save raw bit stream (.bin)", self._save_bitstream)):
            b = QtWidgets.QPushButton(txt)
            b.clicked.connect(fn)
            h.addWidget(b)
        h.addStretch(1)
        v.addLayout(h)
        sp = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.fr_table = QtWidgets.QTableWidget(0, 11)
        self.fr_table.setHorizontalHeaderLabels(["#", "Source", "SCID", "VC", "MC cnt", "VC cnt", "Type", "RS corr.",
                                                 "CRC", "Bit err.", "Content"])
        self.fr_table.verticalHeader().hide()
        self.fr_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.fr_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.fr_table.horizontalHeader().setStretchLastSection(True)
        self.fr_table.currentCellChanged.connect(lambda r, *_: self._show_frame(r))
        sp.addWidget(self.fr_table)
        # Miller-style matrix: one frame per row, one byte per column
        mw = QtWidgets.QWidget()
        mv = QtWidgets.QVBoxLayout(mw)
        mv.setContentsMargins(0, 0, 0, 0)
        bar = QtWidgets.QHBoxLayout()
        self.fr_fmt = QtWidgets.QComboBox()
        self.fr_fmt.addItems(["HEX", "DEC", "BIN"])
        self.fr_fmt.currentIndexChanged.connect(lambda *_: self._fill_matrix())
        self.fr_hl = QtWidgets.QCheckBox("Highlight changing columns")
        self.fr_hl.setChecked(True)
        self.fr_hl.toggled.connect(lambda *_: self._fill_matrix())
        self.fr_cell = QtWidgets.QLabel("")
        self.fr_cell.setMinimumWidth(40)
        self.fr_cell.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        for wd in (QtWidgets.QLabel("Format"), self.fr_fmt, self.fr_hl, self.fr_cell):
            bar.addWidget(wd)
        bar.addStretch(1)
        mv.addLayout(bar)
        self.fr_hex = QtWidgets.QPlainTextEdit()          # header of the selected frame
        self.fr_hex.setReadOnly(True)
        self.fr_hex.setFont(_mono())
        self.fr_hex.setMaximumHeight(56)
        mv.addWidget(self.fr_hex)
        self.fr_mat = QtWidgets.QTableWidget(0, 0)
        self.fr_mat.setFont(_mono())
        self.fr_mat.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.fr_mat.horizontalHeader().setDefaultSectionSize(26)
        self.fr_mat.verticalHeader().setDefaultSectionSize(18)
        self.fr_mat.currentCellChanged.connect(self._matrix_cell)
        mv.addWidget(self.fr_mat, 3)
        self.fr_img = pg.PlotWidget()
        self.fr_img.getPlotItem().setMenuEnabled(False)
        self.fr_img.invertY(True)
        self.fr_img.setLabel("bottom", "bit")
        self.fr_img.setLabel("left", "frame")
        self.fr_imgitem = pg.ImageItem(axisOrder="row-major")
        self.fr_img.addItem(self.fr_imgitem)
        self.fr_cursor = pg.InfiniteLine(angle=90, pen=pg.mkPen("#3fb950", width=2))
        self.fr_img.addItem(self.fr_cursor)
        mv.addWidget(self.fr_img, 2)
        sp.addWidget(mw)
        sp.setSizes([220, 600])
        v.addWidget(sp, 1)
        self._frames = []
        return w

    def _collect_frames(self):
        out = []
        for label, r in self.res.get("_caps", []):
            d = (r.get("decode") if isinstance(r, dict) else None) or (r.get("shr") if isinstance(r, dict) else None)
            for f in (d or {}).get("frames", []):
                if f.get("tf"):
                    out.append((label, f))
        seen = {id(f) for _, f in out}
        for key, label in (("shr", "ШР"), ("ber", "BER test")):
            for f in (self.res.get(key) or {}).get("frames", []):
                if f.get("tf") and id(f) not in seen:
                    out.append((label, f))
        return out

    def _fill_frames(self):
        allf = self._collect_frames()
        fr = [x for x in allf if not (self.fr_hide_idle.isChecked() and x[1].get("idle"))]
        self._frames = fr
        tb = self.fr_table
        tb.setRowCount(len(fr))
        for i, (src, f) in enumerate(fr):
            rs = f.get("rs")
            rs_txt = "fail" if f.get("rs_ok") is False else (str(sum(n for n in rs if n > 0)) if rs else "turbo")
            cont = {True: "ok", False: "DIFFERS"}.get(f.get("idle_content_ok"), "—" if f.get("idle") else "data")
            vals = (i + 1, src, f.get("scid"), f.get("vcid"), f.get("mcfc"), f.get("vcfc"),
                    "idle" if f.get("idle") else "data", rs_txt, "ok" if f.get("crc_ok") else "BAD",
                    f.get("raw_bit_err", "—"), cont)
            for c, v in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(str(v))
                if (c == 8 and not f.get("crc_ok")) or (c == 7 and rs_txt == "fail") or (c == 10 and cont == "DIFFERS"):
                    it.setForeground(QtGui.QColor(C_FAIL))
                tb.setItem(i, c, it)
        tb.resizeColumnsToContents()
        n_idle = sum(1 for _, f in allf if f.get("idle"))
        self.fr_info.setText(f"{len(allf)} frame(s) decoded ({n_idle} idle, {len(allf) - n_idle} data). "
                             "Select a frame to see its 1115 bytes." if allf else
                             "No decoded frames yet - run a BPSK / ШР test or analyze an I/Q file.")
        self._fill_matrix()
        if fr:
            tb.selectRow(0)
            self._show_frame(0)
        else:
            self.fr_hex.clear()

    def _fill_matrix(self):
        fr = getattr(self, "_frames", [])
        m = self.fr_mat
        m.setUpdatesEnabled(False)
        if not fr:
            m.setRowCount(0)
            m.setColumnCount(0)
            self.fr_imgitem.clear()
            m.setUpdatesEnabled(True)
            return
        a = np.array([np.frombuffer(f["tf"], np.uint8) for _, f in fr])
        fmt = self.fr_fmt.currentText()
        m.setRowCount(len(fr))
        m.setColumnCount(a.shape[1])
        m.setHorizontalHeaderLabels([str(i) for i in range(a.shape[1])])
        m.setVerticalHeaderLabels([str(i + 1) for i in range(len(fr))])
        m.horizontalHeader().setDefaultSectionSize({"HEX": 26, "DEC": 32, "BIN": 70}[fmt])
        changing = (a != a[0]).any(axis=0) if self.fr_hl.isChecked() and len(fr) > 1 else np.zeros(a.shape[1], bool)
        yellow = QtGui.QBrush(QtGui.QColor("#5c5000"))
        for r in range(a.shape[0]):
            for c in range(a.shape[1]):
                b = int(a[r, c])
                it = QtWidgets.QTableWidgetItem(f"{b:02X}" if fmt == "HEX" else f"{b}" if fmt == "DEC" else f"{b:08b}")
                if changing[c]:
                    it.setBackground(yellow)
                m.setItem(r, c, it)
        m.setUpdatesEnabled(True)
        bits = np.unpackbits(a, axis=1)                    # 1 = dark, like Miller
        self.fr_imgitem.setImage((1 - bits).astype(np.float32) * 255, levels=(0, 255))
        self.fr_img.setXRange(0, min(bits.shape[1], 2000), padding=0)
        nchg = int(changing.sum())
        self.fr_cell.setText(f"{a.shape[0]} frames × {a.shape[1]} bytes; {nchg} byte column(s) change between frames"
                             + (f" (first: {int(np.argmax(changing))})" if nchg else ""))

    def _matrix_cell(self, row, col, *_):
        fr = getattr(self, "_frames", [])
        if 0 <= row < len(fr) and col >= 0:
            b = fr[row][1]["tf"][col]
            self.fr_cursor.setValue(col * 8)
            self.fr_cell.setText(f"frame {row + 1}, byte {col}: 0x{b:02X} = {b} = {b:08b}b   (bits {col * 8}…{col * 8 + 7})")
            if self.fr_table.currentRow() != row:
                self.fr_table.selectRow(row)

    def _save_bitstream(self):
        streams = []
        for label, r in self.res.get("_caps", []):
            d = (r.get("decode") or r.get("shr") or {}) if isinstance(r, dict) else {}
            if d.get("bitstream"):
                streams.append((label, d["bitstream"]))
        if not streams:
            QtWidgets.QMessageBox.information(self, "Raw bit stream", "No demodulated bit stream in this run.")
            return
        label, data = streams[-1]
        p, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save raw bit stream", "bos_bitstream.bin", "Binary (*.bin)")
        if p:
            with open(p, "wb") as fh:
                fh.write(data)
            self.log(f"BOS station: bit stream of '{label}' ({len(data) * 8:,} bits, MSB first, after SP-L decoding "
                     f"and polarity correction) saved to {p}")

    def _show_frame(self, row):
        if not 0 <= row < len(self._frames):
            return
        src, f = self._frames[row]
        self.fr_hex.setPlainText(
            f"{src}   SCID {f.get('scid')}  VCID {f.get('vcid')}  OCF {f.get('ocf')}  MC count {f.get('mcfc')}  "
            f"VC count {f.get('vcfc')}  FHP 0x{f.get('fhp', 0):03X} ({'idle' if f.get('idle') else 'data'})  "
            f"CRC {'ok' if f.get('crc_ok') else 'BAD'}\n"
            f"bytes 0-5 primary header, 6-17 secondary / CLCW, 18-1112 data field, 1113-1114 CRC")
        if self.fr_mat.currentRow() != row and row < self.fr_mat.rowCount():
            self.fr_mat.blockSignals(True)
            self.fr_mat.setCurrentCell(row, max(0, self.fr_mat.currentColumn()))
            self.fr_mat.blockSignals(False)

    def _save_frames_bin(self):
        sel = sorted({i.row() for i in self.fr_table.selectedIndexes()})
        rows = sel if len(sel) > 1 else list(range(len(self._frames)))   # one (auto-)selected row = save all
        if not self._frames:
            return
        p, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save frames", "bos_frames.bin", "Binary (*.bin)")
        if p:
            with open(p, "wb") as fh:
                for r in rows:
                    fh.write(self._frames[r][1]["tf"])
            self.log(f"BOS station: {len(rows)} frame(s) × 1115 bytes saved to {p}")

    def _save_frames_csv(self):
        if not self._frames:
            return
        p, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save frame list", "bos_frames.csv", "CSV (*.csv)")
        if p:
            with open(p, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["n", "source", "scid", "vcid", "mcfc", "vcfc", "idle", "crc_ok", "rs", "bit_err", "hex"])
                for i, (src, f) in enumerate(self._frames):
                    w.writerow([i + 1, src, f.get("scid"), f.get("vcid"), f.get("mcfc"), f.get("vcfc"), f.get("idle"),
                                f.get("crc_ok"), f.get("rs"), f.get("raw_bit_err"), f["tf"].hex()])
            self.log(f"BOS station: frame list saved to {p}")

    def _build_history(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        h = QtWidgets.QHBoxLayout()
        self.hist_filter = QtWidgets.QLineEdit()
        self.hist_filter.setPlaceholderText("filter (serial, verdict, plan …)")
        self.hist_filter.textChanged.connect(self._load_history)
        rb = QtWidgets.QPushButton("Reload")
        rb.clicked.connect(self._load_history)
        ob = QtWidgets.QPushButton("Open report")
        ob.clicked.connect(self._open_hist_report)
        fb = QtWidgets.QPushButton("Reports folder")
        fb.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(REPORT_DIR)))
        for x in (self.hist_filter, rb, ob, fb):
            h.addWidget(x)
        v.addLayout(h)
        self.hist = QtWidgets.QTableWidget(0, 0)
        self.hist.verticalHeader().hide()
        self.hist.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.hist.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.hist.itemDoubleClicked.connect(lambda *_: self._open_hist_report())
        v.addWidget(self.hist, 1)
        self.fail_stats = QtWidgets.QLabel("")
        self.fail_stats.setWordWrap(True)
        v.addWidget(self.fail_stats)
        QtCore.QTimer.singleShot(0, self._load_history)
        return w

    # ---- summary for the Main tab
    def summary_panel(self):
        g = QtWidgets.QGroupBox("BOS unit")
        v = QtWidgets.QVBoxLayout(g)
        v.setContentsMargins(6, 2, 6, 4)
        self.sum_verdict = QtWidgets.QLabel("NOT TESTED")
        self.sum_verdict.setStyleSheet(f"font-weight:700;color:{C_DIM};")
        self.sum_text = QtWidgets.QLabel("")
        self.sum_text.setWordWrap(True)
        self.sum_text.setStyleSheet(f"color:{C_DIM};")
        self.sum_leds = QtWidgets.QLabel("")
        self.sum_leds.setWordWrap(True)
        v.addWidget(self.sum_verdict)
        v.addWidget(self.sum_leds)
        v.addWidget(self.sum_text, 1)
        return g

    def status_line(self):
        """(state, info, activity) for the Main-tab device row."""
        if self.running:
            return "ok", f"testing: {TEST_NAME.get(self.cur_test, self.cur_test)}", "TEST"
        age = self._age("dop")
        if age is None:
            info = "no telemetry"
        else:
            locks = all(self.tlm.get(k) != 0 for k in ("pll_lmk", "pll_lmx (DAC)", "pll_lmx (mod)", "pll_jesd",
                                                      "pll_all", "jesd_err"))
            info = ("PLLs locked, JESD OK" if locks else "PLL / JESD fault") + f" ({age:.0f} s ago)"
        state = "ok" if age is not None and age < self.lim["tlm_stale_s"] else "off"
        if self.verdict == "FAIL":
            state = "err"
        return state, info, self.verdict

    # ================================================================ settings
    def _collect(self):
        for key, (wdg, kind, sc) in self.cfg_w.items():
            if kind == "b":
                self.cfg[key] = wdg.isChecked()
            elif kind == "i":
                self.cfg[key] = wdg.value()
            else:
                self.cfg[key] = wdg.value() * sc
        for key, s in self.lim_w.items():
            self.lim[key] = s.value()
        if hasattr(self, "ctl_w"):
            self.ctl["cmd"] = {k: cb.currentData() for k, cb in self.ctl_w.items() if cb.currentData()}
        if hasattr(self, "att_mode"):
            self.ctl.update(att_mode=self.att_mode.currentData(), att_id=self.att_id.text().strip(),
                            att_data=self.att_data.text().strip(), att_line=self.att_line.text().strip(),
                            settle_ms=self.settle.value())
        if hasattr(self, "p_freq"):
            self.ctl.update(method=self.method.currentData(), sender=self.p_sender.currentData(),
                            line=self.p_line.text().strip() or "BOS", freq_n=self.p_freq.value(),
                            bpsk_cmd=self.p_bpsk.currentData(), oqpsk_cmd=self.p_oqpsk.currentData(),
                            rate_ur=self.p_rate_ur.currentData(), rate_shr=self.p_rate_shr.currentData(),
                            time_snr=self.p_tsnr.value(),
                            keepalive=self.p_keep.isChecked(), tm_enable=self.p_tm.isChecked())
            self._sync_carrier()
        if hasattr(self, "r_wait"):
            self.ctl.update(tfg_setup=self.r_tfg.isChecked(), tfg_src=self.r_src.currentData(),
                            tfg_feed=self.r_feed.currentData(), power_up=self.r_pwr.currentData(),
                            ref_wait_s=self.r_wait.value(), tfg_ref_apply=self.r_std.isChecked(),
                            tfg_ref_amp=self.r_amp.value(), tfg_ref_offs=self.r_offs.value())
        if hasattr(self, "supply_cb"):
            self.data["supply"] = self.supply_cb.currentData()
        self.data["unit"] = {"serial": self.serial.text().strip(), "operator": self.operator.text().strip()} \
            if hasattr(self, "operator") else self.data["unit"]
        self.save()

    def save(self):
        try:
            tmp = STORE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=1, ensure_ascii=False)
            os.replace(tmp, STORE_FILE)
        except OSError as e:
            self.log(f"cannot save {STORE_FILE}: {e}", "err")

    def _default_limits(self):
        for key, _, d, _ in LIM_FIELDS:
            self.lim_w[key].setValue(d)

    def _fill_ctl(self):
        cmds = self._can_cmds()
        for key, cb in self.ctl_w.items():
            cb.blockSignals(True)
            cb.clear()
            cb.addItem("(ask operator)", "")
            for c in cmds:
                cb.addItem(f"{c.label()}  {c.id_str()}: {c.data_str()}", c.uid)
            cb.setCurrentIndex(max(0, cb.findData(self.ctl.get("cmd", {}).get(key, ""))))
            cb.blockSignals(False)

    def _can_cmds(self):
        c = getattr(self.host, "can_win", None)
        return list(c.store.commands) if c and hasattr(c, "store") else []

    def _fill_plans(self):
        self.plan_cb.blockSignals(True)
        self.plan_cb.clear()
        self.plan_cb.addItems(list(self.data["plans"]))
        self.plan_cb.setCurrentText(self.data.get("plan", ""))
        self.plan_cb.blockSignals(False)
        self._plan_tip()

    def _plan_tip(self):
        tests = self.data["plans"].get(self.plan_cb.currentText(), [])
        self.plan_cb.setToolTip("\n".join(f"{k + 1}. {TEST_NAME.get(t, t)}" for k, t in enumerate(tests)))
        if hasattr(self, "plan_view"):
            self.plan_view.refresh()

    def _plan_changed(self, name):
        self.data["plan"] = name
        self._plan_tip()
        self.save()

    def plan_names(self):
        return list(self.data["plans"])

    def edit_plan(self):
        name = self.plan_cb.currentText()
        dlg = PlanDialog(self, name, self.data["plans"].get(name, []))
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        new, tests = dlg.value()
        if not new or not tests:
            return
        plans = {(new if k == name else k): (tests if k == name else v) for k, v in self.data["plans"].items()}
        self.data["plans"], self.data["plan"] = plans, new
        self.save()
        self._fill_plans()

    def new_plan(self):
        dlg = PlanDialog(self, f"Plan {len(self.data['plans']) + 1}", ["pre", "cw", "oqpsk"])
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        name, tests = dlg.value()
        if name and tests:
            self.data["plans"][name] = tests
            self.data["plan"] = name
            self.save()
            self._fill_plans()

    def delete_plan(self):
        if len(self.data["plans"]) <= 1:
            return
        self.data["plans"].pop(self.plan_cb.currentText(), None)
        self.data["plan"] = next(iter(self.data["plans"]))
        self.save()
        self._fill_plans()

    # ================================================================ host
    def _wire_host(self):
        """Connect to the FSW worker's result signals (the worker is created after the tabs)."""
        w = getattr(self.host, "worker", None)
        if w is not None and getattr(self, "_wired", None) is not w:
            w.job_done.connect(self._on_job_done)
            w.failed.connect(self._on_job_failed)
            self._wired = w

    def log(self, text, level="info"):
        if hasattr(self.host, "logmsg"):
            self.host.logmsg(text, level)

    def demo_setup(self):
        self.demo = True
        self._faults_changed()

    def _is_demo(self):
        dev = getattr(getattr(self.host, "worker", None), "dev", None)
        return dev is not None and not hasattr(dev, "inst")

    def _faults_changed(self, *_):
        self.sim.faults = {k: s.value() for k, (c, s) in self.fault_w.items() if c.isChecked()}

    def _random_fault(self):
        for c, _ in self.fault_w.values():
            c.blockSignals(True)
            c.setChecked(False)
            c.blockSignals(False)
        key = ba.FAULTS[int(np.random.default_rng().integers(len(ba.FAULTS)))][0]
        self.sim.faults = {key: self.fault_w[key][1].value()}
        self._blind = key
        self.blind_lbl.setText("A hidden fault is injected. Run the plan, then press 'Clear all faults' to reveal it.")
        try:
            self.fault_w[next(iter(self.fault_w))][0].toggled.disconnect(self._reveal)
        except TypeError:
            pass
        for c, _ in self.fault_w.values():
            c.toggled.connect(self._reveal)

    def _reveal(self, *_):
        key = getattr(self, "_blind", None)
        if key:
            label = next(x[1] for x in ba.FAULTS if x[0] == key)
            self.blind_lbl.setText(f"The hidden fault was: <b>{label}</b>")
            self._blind = None

    # ---- CAN / telemetry
    def on_frames(self, frames):
        if self.pending == "ack":
            for fr in frames:
                a = bp.parse_ack(fr.can_id, fr.ext, fr.data, self.ctl.get("sender", 0x18))
                if a and a["code"] == self._ack_code:
                    self.pending = "ack-received"
                    self.wait_timer.stop()
                    QtCore.QTimer.singleShot(0, lambda a=a: self._advance(a))
                    break
        if self.rx is None or self._is_demo():          # demo: telemetry comes from the simulated BOS
            return
        for fr in frames:
            r = self.rx.feed(fr)
            if r is None:
                continue
            base, t, data, ts = r
            if base >> 16 != 0x1A46:
                continue
            if t == 0xFA and len(data) >= 8 + bp.CONFIG_LEN:
                try:
                    self.cfg_rx = (time.time(), bp.decode_config(data[8:8 + bp.CONFIG_LEN]))
                except ValueError:
                    pass
                continue
            layout = {0xFB: telemetry.BOS_DOP, 0xFC: telemetry.BOS_OSN, 0xFD: telemetry.BOS_SOST}.get(t)
            if not layout:
                continue
            self._decode(layout, data)
            self.tlm_t[{0xFB: "dop", 0xFC: "osn", 0xFD: "sost"}[t]] = time.time()

    def _decode(self, fields, data):
        for f in fields:
            if f.kind == "res" or f.name.startswith("byte "):
                continue
            v = f.raw(data)
            if v is None:
                continue
            if f.kind == "fix3":
                self.tlm[f.name] = (v & 0x7FFF) / 8 * (-1 if v & 0x8000 else 1) / 1000
            else:
                self.tlm[f.name] = v

    def _age(self, key):
        t = self.tlm_t.get(key)
        return None if t is None else time.time() - t

    def tlm_snapshot(self):
        if self._is_demo():
            self._tick()
        d = dict(self.tlm)
        d["age_dop"], d["age_osn"] = self._age("dop"), self._age("osn")
        return d

    def _tick(self):
        if self._is_demo():
            # demo: telemetry of the simulated BOS (real BOS arrays are ignored, see on_frames)
            self.tlm.update(self.sim.telemetry())
            self.tlm_t["dop"] = self.tlm_t["osn"] = self.tlm_t["sim"] = time.time()
        self._show_tlm()

    def _show_tlm(self):
        good = {"pll_lmk": 1, "pll_lmx (DAC)": 1, "pll_lmx (mod)": 1, "pll_jesd": 1, "pll_all": 1, "jesd_err": 1,
                "Stat_10MHz": 1}
        for key, (led, val) in self.tlm_w.items():
            v = self.tlm.get(key)
            if v is None:
                continue
            color = C_OK
            if key in good:
                text = {"jesd_err": {0: "ERROR", 1: "OK"}, "Stat_10MHz": {0: "no signal", 1: "present"}}.get(
                    key, {0: "UNLOCKED", 1: "locked"}).get(v, str(v))
                color = C_OK if v == good[key] else C_FAIL
            elif key.startswith("vdd"):
                nom = {"vdd_1v": 1.0, "vdd_1_8v": 1.8, "vdd_2_5v": 2.5}[key]
                text = f"{v:.3f} V"
                color = C_OK if abs(v - nom) / nom * 100 <= self.lim["rail_tol_pct"] else C_FAIL
            elif key.startswith("Temp"):
                text = f"{v} °C" if isinstance(v, int) else f"{v:.1f} °C"
                color = C_OK if v <= self.lim["temp_max"] else C_FAIL
            elif key == "Sour_10MHz":
                text = {0: "internal", 1: "external"}.get(v, str(v))
                color = C_INFO
            elif key == "Err_Ref_LO":
                text = f"{v}"
                color = C_OK if abs(v) <= self.lim["err_ref_lo"] else C_WARN
            elif key == "Mode":
                text = {0: "standby", 1: "session (SR)"}.get(v, str(v))
                color = C_INFO
            elif key == "Mode_SNR":
                text = f"0x{v:02X}" + {0x40: " carrier", 0: " none"}.get(v, "")
                color = C_INFO
            else:
                text = str(v)
                color = C_INFO
            led.setStyleSheet(f"color:{color};font-size:16px;")
            val.setText(text)
        age = self._age("dop")
        self.tlm_age.setText("no BOS telemetry yet (IOK DOP)" if age is None else
                             f"IOK DOP {age:.0f} s ago" + (" (simulated)" if self.tlm_t.get("sim") else ""))
        if hasattr(self, "sum_leds"):
            keys = ("pll_lmk", "pll_lmx (DAC)", "pll_lmx (mod)", "pll_jesd", "pll_all", "jesd_err")
            names = ("LMK", "DACclk", "LMX", "JESDpll", "FPGA", "JESD")
            parts = []
            for k, n in zip(keys, names):
                v = self.tlm.get(k)
                c = C_DIM if v is None or age is None else (C_OK if v else C_FAIL)
                parts.append(f'<span style="color:{c}">●</span> {n}')
            self.sum_leds.setText("&nbsp; ".join(parts))

    def on_live(self, r):
        """Every FSW result drawn on the Spectrum Analyzer tab (not used for the tests)."""
        return

    # ================================================================ run control
    def toggle_run(self):
        if self.running:
            self.abort("aborted by the operator")
            return
        self.start_plan(self.plan_cb.currentText())

    def run_plan_by_name(self, name, done=None):
        """Used by the Main-tab sequence step. done(verdict, summary) is called at the end."""
        if name not in self.data["plans"]:
            if done:
                done("ABORTED", f"no test plan '{name}'")
            return
        self.plan_cb.setCurrentText(name)
        self.start_plan(name, done)

    def toggle_live(self, on):
        if on:
            if self.running:
                self.live_btn.setChecked(False)
                return
            self.start_plan("(live)", tests=["live"])
        elif self.running and self.live_mode:
            self.abort("live diagnose stopped")

    def start_plan(self, name, done=None, tests=None):
        if self.running:
            if done:
                done("ABORTED", "the station is busy")
            return
        tests = list(tests or self.data["plans"].get(name, []))
        offline = tests == ["file"]
        if self.ctl.get("method") == "protocol" and tests not in (["live"], ["file"]) and "link" not in tests:
            tests.insert(0, "link")
        if tests not in (["live"], ["file"], ["link"]) and "ref" not in tests:
            tests.insert(0, "ref")                         # TFG reference + BOS power-up before anything else
        self._ref_failed = False
        self._gate_rows = set()
        if not offline and not getattr(self.host, "connected", False):
            msg = "FSW not connected - connect it on the Spectrum Analyzer tab first"
            self.log(f"BOS station: {msg}", "err")
            self.step_lbl.setText(msg)
            if done:
                done("ABORTED", msg)
            self.live_btn.setChecked(False)
            return
        self._collect()
        self._wire_host()
        if not offline:
            self._auto_bandwidth()
        self.live_mode = tests == ["live"]
        if hasattr(self.host, "run_btn") and self.host.run_btn.isChecked():
            self.host.run_btn.setChecked(False)          # stop the live FSW view while testing
        self.res, self.rows, self.findings = {}, [], []
        self.cur_att = int(self.cfg.get("att_code", 0))
        self.cur_mode = None
        self.table.setRowCount(0)
        self.find_list.clear()
        self.find_detail.clear()
        self.diagram.set_scores({}, tested=False)
        self.sig_src.blockSignals(True)
        self.sig_src.clear()
        self.sig_src.blockSignals(False)
        self.on_done = done
        self.abort_flag = False
        self.running = True
        self.cur_test = ""
        self.run_info = {"plan": name, "tests": tests, "serial": self.serial.text().strip(),
                         "operator": self.operator.text().strip(), "start": time.time(),
                         "demo": self._is_demo() and not offline,
                         "faults": dict(self.sim.faults) if self._is_demo() and not offline else {}}
        self.run_btn.setText("■ Abort")
        self.progress.setRange(0, max(1, len(tests)))
        self.progress.setValue(0)
        self._show_verdict("RUNNING")
        self.log(f"BOS station: plan '{name}' started ({', '.join(TEST_NAME.get(t, t) for t in tests)})", "info")
        self.gen = self._plan_gen(tests)
        self._advance(None)

    def _auto_bandwidth(self):
        """FSW I/Q bandwidth from *OPT?: 160 MHz -> capture OQPSK at 200 MS/s (edges cut, resampled);
        bandwidth extension -> 524.288 MS/s = 4 samples/symbol."""
        if not self.cfg.get("fsw_bw_auto", True):
            return
        dev = getattr(getattr(self.host, "worker", None), "dev", None)
        bw = ba.fsw_bandwidth(getattr(dev, "options", "")) if dev is not None else None
        if not bw:
            self.log("BOS station: no bandwidth option in the FSW *OPT? answer - using the manual "
                     "'FSW I/Q bandwidth' setting", "warn")
            return
        need = self.cfg["sr_oqpsk"] * (1 + self.cfg["alpha"])
        fs = min(self.cfg["sr_oqpsk"] * ba.DEMOD_SPS, bw * 1.25)     # 160 MHz: 200 MS/s; extension: 524.288 MS/s
        changed = (bw, fs) != (self.cfg.get("fsw_bw"), self.cfg.get("fs_oqpsk"))
        self.cfg["fsw_bw"], self.cfg["fs_oqpsk"] = bw, fs
        for key in ("fsw_bw", "fs_oqpsk"):
            wdg, _, sc = self.cfg_w[key]
            wdg.blockSignals(True)
            wdg.setValue(self.cfg[key] / sc)
            wdg.blockSignals(False)
        if changed:
            self.save()
        self.log(f"BOS station: FSW I/Q bandwidth {bw / 1e6:g} MHz -> OQPSK captured at {fs / 1e6:g} MS/s"
                 + ("" if bw >= need else f" (signal ~{need / 1e6:.0f} MHz: edges cut, PA compression judged on BPSK)"),
                 "info" if bw >= need else "warn")

    def abort(self, why):
        self.abort_flag = True
        self.step_lbl.setText(why)
        if self.pending in ("sleep",):
            self.wait_timer.stop()
            self._advance(None)
        elif self.pending is None:
            self._finish("ABORTED", why)

    def _advance(self, value):
        """Resume the plan generator with the result of the last request."""
        self.pending = None
        if not self.running:
            return
        if self.abort_flag:
            try:
                self.gen.close()
            finally:
                self._finish("ABORTED", self.step_lbl.text())
            return
        try:
            req = self.gen.send(value)
        except StopIteration:
            self._conclude()
            return
        except Exception as e:
            self.log(f"BOS station: {type(e).__name__}: {e}", "err")
            self._finish("ABORTED", f"{type(e).__name__}: {e}")
            return
        self._handle(req)

    def _handle(self, req):
        kind = req[0]
        if kind == "sleep":
            self.pending = "sleep"
            self.wait_timer.start(max(1, int(req[1] * 1000)))
        elif kind == "ack":
            self.pending = "ack"
            self.wait_timer.start(max(1, int(req[1] * 1000)))
        elif kind == "fsw":
            self.pending = "fsw"
            self.host.worker.submit("bos", req[1])
        elif kind == "job":                                 # run fn on another device's worker thread
            self.pending = "job"
            submit, fn = req[1], req[2]

            def run():
                try:
                    r = ("ok", fn())
                except Exception as e:
                    r = ("err", f"{type(e).__name__}: {e}")
                self.job_done.emit(r)
            submit(run)
        elif kind == "prompt":
            r = QtWidgets.QMessageBox.question(self, "BOS test station", req[1],
                                               QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel)
            if r != QtWidgets.QMessageBox.Ok:
                self.abort_flag = True
                self.step_lbl.setText("aborted by the operator")
            QtCore.QTimer.singleShot(0, lambda: self._advance(r == QtWidgets.QMessageBox.Ok))
        else:
            QtCore.QTimer.singleShot(0, lambda: self._advance(None))

    def _on_ext_job(self, r):
        if self.pending == "job":
            QtCore.QTimer.singleShot(0, lambda: self._advance(r))

    def _on_job_done(self, tag, result):
        if tag == "bos" and self.pending == "fsw":
            QtCore.QTimer.singleShot(0, lambda: self._advance(("ok", result)))

    def _on_job_failed(self, tag, msg):
        if tag == "bos" and self.pending == "fsw":
            QtCore.QTimer.singleShot(0, lambda: self._advance(("err", msg)))

    # ================================================================ plan
    def _plan_gen(self, tests):
        pv = getattr(self, "plan_view", None) if self.run_info.get("plan") == self.plan_cb.currentText() else None
        if pv is not None:
            pv.refresh(force=True)
            pv.reset_marks()
        for k, t in enumerate(tests):
            self.cur_test = t
            if pv is not None:
                if k:
                    pv.mark(k - 1, "done")
                pv.mark(k, "run")
            self.progress.setValue(k)
            self.step_lbl.setText(f"Test {k + 1}/{len(tests)}: {TEST_NAME.get(t, 'Live diagnose')}")
            fn = getattr(self, f"_t_{t}")
            try:
                yield from fn()
            except _Skip as e:
                self._row(TEST_NAME.get(t, t), "skipped", None, "", "", "SKIP", note=str(e))
            except _TestError as e:
                self._row(TEST_NAME.get(t, t), "error", None, "", "", "ERROR", note=str(e))
                self.log(f"BOS station: {TEST_NAME.get(t, t)}: {e}", "err")
            self._update_diagnosis()
        if pv is not None and tests:
            pv.mark(len(tests) - 1, "done")
        self.progress.setValue(len(tests))

    # ---- unit control helpers (generators)
    # ---- BOS protocol
    def _sim_map(self):
        return {self.ctl.get("bpsk_cmd", 0xA1): "BPSK", self.ctl.get("oqpsk_cmd", 0xA4): "OQPSK"}

    def _frame(self, code, data=b"", ack=True, receiver=bp.BOS):
        c = getattr(self.host, "can_win", None)
        can_id, payload = bp.command(code, self.ctl.get("sender", 0x18), data, ack, receiver)
        if c is None:
            return False
        return c.send_frame(c.FRAME(0, can_id, True, False, None, payload, bus=self.ctl.get("line", "BOS")))

    def _send_now(self, code, data=b""):
        """Fire-and-forget (RF off button, keep-alive, end of a test)."""
        if self._is_demo():
            self.sim.handle(code, bytes(data) + bytes(8), self._sim_map())
            return True
        ok = self._frame(code, data, ack=False, receiver=bp.GROUP_ANGLE if code == 0x0C else bp.BOS)
        if code == 0x01:
            self.log("BOS station: MODE_D (standby, RF off) sent" if ok else
                     "BOS station: MODE_D not sent - BOS CAN line not connected", "info" if ok else "err")
        return ok

    def _keepalive(self):
        if self.ctl.get("method") == "protocol" and self.ctl.get("keepalive", True) and \
                self.cur_mode not in (None, "rf_off"):
            self._send_now(0x0C, bp.p_angle(0.0, 0.0))

    def _proto(self, code, data=b"", what=""):
        """Send a command with acknowledgement request; returns the acknowledgement dict."""
        name = bp.COMMANDS[code][0]
        if self._is_demo():
            ok = self.sim.handle(code, bytes(data) + bytes(8), self._sim_map())
            yield ("sleep", 0.02)
            a = {"code": code, "negative": not ok, "unknown": not ok, "can2": False, "byte": 0, "array": False}
        else:
            self._ack_code = code
            if not self._frame(code, data):
                raise _TestError(f"{name}: CAN send failed - line '{self.ctl.get('line')}' not connected (CAN tab)")
            a = yield ("ack", 0.3)
        if a is None:
            raise _TestError(f"{name}: no acknowledgement within 300 ms (BOS off, wrong CAN line, "
                             f"or bus problem - check the CAN tab)")
        self.log(f"BOS station: {what or name} → {bp.ack_text(a)}", "info" if not a["negative"] else "err")
        if a["negative"]:
            hint = "" if a["unknown"] else (" — the sender address may not be allowed, or the command is only "
                                             "executed in standby (ДЖР); try sender ВМ 1 (0x20)")
            raise _TestError(bp.ack_text(a) + hint)
        return a

    def _session(self):
        """(Re)start the session for the current mode / attenuation."""
        if self.cur_mode in (None, "rf_off"):
            yield from self._proto(0x01)
            return
        code = {"cw": 0xA0, "bpsk": self.ctl.get("bpsk_cmd", 0xA1), "oqpsk": self.ctl.get("oqpsk_cmd", 0xA4),
                "shr_pre": 0xA2, "shr_kbd": 0xA4}[self.cur_mode]
        data = bp.p_session(code, int(self.ctl.get("freq_n", 800)), min(bp.ATTEN_MAX_DB, self.cur_att),
                            coding=1 if code == 0xA4 else 0, continuous=True,
                            count=2 if self.cur_mode == "shr_kbd" else 2047,
                            rate=int(self.ctl.get("rate_ur" if code == 0xA1 else "rate_shr", 2)),
                            reg=5 if code == 0xA1 else 1)
        yield from self._ref_gate(bp.COMMANDS[code][0])      # 10 MHz reference ready before a mode command
        self._keepalive()                                   # valid target designation before the session
        yield from self._proto(code, data, f"{bp.COMMANDS[code][0]} (N={self.ctl.get('freq_n')}, "
                                           f"att {self.cur_att} dB)")
        self._sr_t = time.monotonic()

    # ---- 10 MHz reference (TFG6806) and BOS power-up
    def _fresh_osn(self, after=None, timeout=3.0):
        """ИОК_ОСН telemetry received after `after` (time.time()), or not older than 2.5 s. In protocol mode
        REQ_TM ИОК_ОСН is sent when none arrives. Returns the telemetry dict or None after `timeout`."""
        if self._is_demo():
            self._tick()
            yield ("sleep", 0.05)
            return dict(self.tlm)
        t_end = time.monotonic() + timeout
        t_req = 0.0
        while True:
            t_rx = self.tlm_t.get("osn")
            if t_rx is not None and ((after is not None and t_rx > after) or
                                     (after is None and time.time() - t_rx <= 2.5)):
                return dict(self.tlm)
            if time.monotonic() > t_end:
                return None
            if self.ctl.get("method") == "protocol" and time.monotonic() - t_req > 2.0:
                self._frame(0x11, bp.p_req_tm(0xFC), ack=False)       # REQ_TM ИОК_ОСН, no КВ
                t_req = time.monotonic()
            yield ("sleep", 0.25)

    def _ref_gate(self, what):
        """Before a mode command: PulsNumb_10MHz = 10 000 000 ± tol and |Err_Ref_LO| ≤ limit (fresh ИОК_ОСН).
        Waits up to 'Reference wait' for the BOS to settle on the reference, else the test stops with an error
        and no mode command is sent."""
        if not self.cfg.get("ext_ref", True):
            return
        tol, emax = self.lim.get("pulsnumb_tol", 5.0), self.lim.get("err_ref_lo", 3.0)
        wait = 3.0 if self._ref_failed else float(self.ctl.get("ref_wait_s", 30))
        t_end = time.monotonic() + wait
        p = e = None
        while True:
            tl = yield from self._fresh_osn(None, 3.0)
            if tl is not None:
                p, e = tl.get("PulsNumb_10MHz"), tl.get("Err_Ref_LO")
                if p is not None and e is not None and abs(p - 10_000_000) <= tol and abs(e) <= emax:
                    if self.cur_test not in self._gate_rows:
                        self._gate_rows.add(self.cur_test)
                        self._row(TEST_NAME.get(self.cur_test, self.cur_test), "Reference before mode command",
                                  None, "", f"10 000 000 ± {tol:g}, |Err| ≤ {emax:g}", "PASS",
                                  text=f"PulsNumb {p:,}, Err_Ref_LO {e:+d}")
                    self._ref_failed = False
                    return
            if time.monotonic() > t_end:
                self._ref_failed = True
                got = "no ИОК_ОСН telemetry" if tl is None else \
                    f"PulsNumb_10MHz = {p if p is None else f'{p:,}'}, Err_Ref_LO = {e}"
                raise _TestError(f"10 MHz reference not ready before {what} ({wait:g} s): {got}; required "
                                 f"PulsNumb_10MHz 10 000 000 ± {tol:g} and |Err_Ref_LO| ≤ {emax:g}. "
                                 f"Mode command not sent.")
            self.step_lbl.setText(f"{TEST_NAME.get(self.cur_test, self.cur_test)}: waiting for the 10 MHz reference "
                                  f"before {what} (PulsNumb {p}, Err_Ref_LO {e}) …")
            yield ("sleep", 1.0 if not self._is_demo() else 0.05)

    def _tfg(self):
        tfg = getattr(self.host, "tfg", None)
        return tfg if tfg is not None and tfg.is_connected else None

    def _bos_power_on(self, T):
        """Switch the selected supply output on with the voltage / current set on its tab."""
        src = self.data.get("supply", "none")
        if self._is_demo():
            self.sim.mode = "off"
            self._row(T, "BOS power on", None, "", "", "PASS", text="simulated supply")
            yield ("sleep", 0.05)
            return
        if src == "none":
            raise _TestError("BOS power-up by supply: no supply output selected (Unit / signal page)")
        kind, ch = src.split(":")
        ch = int(ch)
        if kind == "hmp":
            b = getattr(self.host, "bench", None)
            if not b or not b._hmp_ok:
                raise _TestError("HMP4040 not connected")
            w, name = b.hmp, f"HMP4040 OUT{ch}"

            def job():
                v, i, _ = w.dev.read_channel(ch)
                w.set_channel(ch, v, i, True)
                w.set_master(True)
                return v, i
        else:
            n = getattr(self.host, "n67", None)
            if not n or not n.ok:
                raise _TestError("N6701C not connected")
            w, name = n.worker, f"N6701C output {ch}"

            def job():
                v, i = w.dev.read_channel(ch)[:2]
                w.set_channel(ch, v, i, True)
                return v, i
        st, r = yield ("job", w.submit, job)
        if st == "err":
            raise _TestError(f"BOS power on ({name}): {r}")
        self._row(T, "BOS power on", None, "", "", "PASS", text=f"{name}: {r[0]:.2f} V, limit {r[1]:.2f} A")

    def _t_ref(self):
        T = TEST_NAME["ref"]
        c = self.ctl
        demo = self._is_demo()
        # 1) TFG6806 first: it must give the 10 MHz before the BOS gets power
        if c.get("tfg_setup", True):
            tfg = self._tfg()
            if tfg is None:
                raise _TestError("TFG6806 not connected (TFG6806 tab) - it must give the BOS its 10 MHz "
                                 "before the BOS is powered")
            src, feed = c.get("tfg_src", "EXT"), int(c.get("tfg_feed", 1))
            std = bool(c.get("tfg_ref_apply", True))
            amp, offs = float(c.get("tfg_ref_amp", 2.0)), float(c.get("tfg_ref_offs", 2.0))
            w = tfg.worker

            def job():
                out = {}
                if src in ("INT", "EXT"):
                    w.command(f"UTILity:CLOck:SOURce {'EXTernal' if src == 'EXT' else 'INTernal'}",
                              f"clock source {'external' if src == 'EXT' else 'internal'} (BOS station)")
                if feed == 0:
                    w.command("UTILity:INSide:CLOck ON", "internal 10 MHz on the rear port (BOS station)")
                elif src == "EXT":
                    w.command("UTILity:INSide:CLOck OFF")       # the rear 10 MHz port is the input now
                out["src"] = w.command("UTILity:CLOck:SOURce?")
                out["inside"] = w.command("UTILity:INSide:CLOck?")
                if feed in (1, 2):
                    s = {"out": True}
                    if std:                                    # standard BOS 10 MHz, set before the output goes on
                        s = {"wave": "SINusoid", "mode": "CONTinuous", "freq": 10e6, "ampl": amp, "offs": offs,
                             "out": True}
                    w.apply(feed, s, prev={}, label="10 MHz to the BOS")
                    for k in ("wave", "freq", "ampl", "offs", "out"):
                        out[k] = w.dev.get(feed, k)
                return out
            st, r = yield ("job", w.submit, job)
            if st == "err":
                raise _TestError(f"TFG6806: {r}")
            ext = str(r.get("src", "")).strip().upper().startswith("EXT")
            if src in ("INT", "EXT"):
                self._row(T, "TFG6806 clock source (UTILity:CLOck:SOURce?)", None, "",
                          "external" if src == "EXT" else "internal",
                          "PASS" if ext == (src == "EXT") else "FAIL", text=str(r.get("src", "")).strip() or "?",
                          note="setting only: the TFG has no query for 'external clock present'; without a "
                               "qualified clock it runs on its internal one (status bar 'Ext Ref On')"
                          if src == "EXT" else "")
            if feed in (1, 2):
                f = r.get("freq")
                self._row(T, f"TFG6806 CH{feed} frequency (10 MHz to the BOS)", f / 1e6 if f else None, "MHz",
                          "10", "PASS" if f and abs(f - 10e6) <= 1 else "FAIL", spec="{:.6f}")
                if std:
                    wv = str(r.get("wave") or "")
                    self._row(T, f"TFG6806 CH{feed} waveform", None, "", "sine",
                              "PASS" if wv.upper().startswith("SIN") else "FAIL", text=wv or "?")
                    for k, nm, u, want in (("ampl", "amplitude", "Vpp", amp), ("offs", "offset", "V", offs)):
                        v = r.get(k)
                        self._row(T, f"TFG6806 CH{feed} {nm}", v, u, f"{want:g}",
                                  "PASS" if v is not None and abs(v - want) <= 0.02 * max(1.0, abs(want)) else "FAIL",
                                  spec="{:.3f}")
                self._row(T, f"TFG6806 CH{feed} output", None, "", "on", "PASS" if r.get("out") else "FAIL",
                          text="on" if r.get("out") else "OFF")
            else:
                self._row(T, "TFG6806 internal 10 MHz on the rear port (UTILity:INSide:CLOck?)", None, "",
                          "on" if feed == 0 else "", "INFO", text=str(r.get("inside", "")).strip())
        # 2) BOS power
        pu = c.get("power_up", "prompt")
        t_on = None
        if pu == "prompt" and not demo:
            ok = yield ("prompt", "The TFG6806 gives the 10 MHz now.<br><br>Switch the <b>BOS power on</b>, "
                                  "then press OK.")
            if not ok:
                return
            t_on = time.time()
        elif pu == "supply":
            yield from self._bos_power_on(T)
            t_on = time.time()
        # 3) 10 MHz pulses counted by the BOS (ИОК_ОСН after power-up)
        if not self.cfg.get("ext_ref", True):
            self._row(T, "10 MHz pulses (PulsNumb_10MHz)", None, "", "", "INFO", text="internal reference expected")
            return
        tl = yield from self._fresh_osn(t_on, max(10.0, float(c.get("ref_wait_s", 30))))
        if tl is None:
            self._row(T, "ИОК_ОСН after power-up", None, "", "received", "FAIL", text="not received",
                      note="BOS not powered / booting, telemetry off, or wrong CAN line")
            return
        p = tl.get("PulsNumb_10MHz")
        self._row(T, "10 MHz pulses present (PulsNumb_10MHz)", p, "", "> 0", "PASS" if p else "FAIL", spec="{:,.0f}")
        if tl.get("Stat_10MHz") is not None:
            self._row(T, "External 10 MHz valid (Stat_10MHz)", None, "", "norm",
                      "PASS" if tl["Stat_10MHz"] else "FAIL", text="norm" if tl["Stat_10MHz"] else "no signal")

    def _t_link(self):
        T = TEST_NAME["link"]
        a = yield from self._proto(0x00, what="NOP (link check)")
        self._row(T, "Control link (NOP)", None, "", "КВ+", "PASS", text=f"КВ+ via CAN{2 if a['can2'] else 1}")
        self.cur_mode = "rf_off"
        yield from self._proto(0x01, what="MODE_D (standby)")
        self._row(T, "Standby (MODE_D)", None, "", "КВ+", "PASS", text="КВ+")
        ts = int(self.ctl.get("time_snr", 255))
        if ts:
            yield from self._proto(0x04, bp.p_byte(ts), f"SET_Time_SNR {ts} s")
            self._row(T, "Session limit (SET_Time_SNR)", ts, "s", "КВ+", "PASS", spec="{:.0f}")
        if not self.ctl.get("tm_enable", True):
            return
        for tm, per in ((0xFB, 1.0), (0xFC, 1.0), (0xFD, 10.0)):
            yield from self._proto(0x10, bp.p_period_tm(tm, per), f"SET_Period_TM {bp.TM_TYPES[tm]} {per:g} s")
        self.cfg_rx = None
        yield from self._proto(0x11, bp.p_req_tm(0xFA), "REQ_TM ИОК_КОНФИГ")
        if self._is_demo():
            self.cfg_rx = (time.time(), bp.decode_config(self.sim.config()))
        for _ in range(30):                                  # ИОК_КОНФИГ: 18 CAN frames, after the current ИОК
            if self.cfg_rx:
                break
            yield ("sleep", 0.1)
        if not self.cfg_rx:
            self._row(T, "ИОК_КОНФИГ received", None, "", "within 3 s", "FAIL", text="not received")
            return
        c = self.cfg_rx[1]
        self.res["config"] = c
        self._row(T, "ИОК_КОНФИГ checksum", None, "", "CRC-16 ok", "PASS" if c["crc_ok"] else "FAIL",
                  text=f"0x{c['Checksum']:04X}" + ("" if c["crc_ok"] else f" (calc 0x{c['crc_calc']:04X})"))
        for k, txt in (("Config_Type", {0: "factory", 1: "user"}), ("Sour_10MHz", {0: "internal", 1: "external"}),
                       ("Data_Random", {0: "off", 1: "on"})):
            self._row(T, k, None, "", "", "INFO", text=txt[c[k]])
        for k in ("Time_SNR", "Numb_Preamb", "Ttlm_main_FC", "Ttlm_dop_FB", "Ttlm_stat_FD", "Temp_Max_DT1",
                  "Temp_Max_DT2", "Temp_Max_DT3"):
            self._row(T, k, c[k], "", "", "INFO", spec="{:.0f}")
        self._row(T, "Preamble", None, "", "", "INFO", text=f"0x{c['Preamb']:08X}")

    def _set_mode(self, mode):
        """mode: rf_off / cw / bpsk / oqpsk."""
        if self.ctl.get("method") == "protocol":
            self.cur_mode = mode
            yield from self._session()
            yield ("sleep", self.ctl.get("settle_ms", 1500) / 1000 if not self._is_demo() else 0.05)
            return
        if self._is_demo():
            self.sim.mode = {"rf_off": "off", "cw": "cw", "bpsk": "BPSK", "oqpsk": "OQPSK", "shr_pre": "OQPSK",
                             "shr_kbd": "OQPSK"}[mode]
            yield ("sleep", 0.05)
            return
        if mode != "rf_off":
            yield from self._ref_gate(dict(CTL_MODES)[mode])
        uid = self.ctl.get("cmd", {}).get(mode)
        c = getattr(self.host, "can_win", None)
        cmd = c.store.by_uid(uid) if (c and uid) else None
        label = dict(CTL_MODES)[mode]
        if cmd is not None:
            if not c.send_frame(cmd.to_frame(c.FRAME)):
                raise _TestError(f"CAN send failed for '{label}' (BOS line not connected?)")
            self.log(f"BOS station: {label} → CAN {cmd.id_str()}: {cmd.data_str()}")
        else:
            ok = yield ("prompt", f"Switch the BOS to: <b>{label}</b><br><br>Press OK when it is done.")
            if not ok:
                return
        yield ("sleep", self.ctl.get("settle_ms", 1500) / 1000)

    def _set_att(self, code):
        code = int(max(0, min(bp.ATTEN_MAX_DB, code)))
        if self.ctl.get("method") == "protocol":
            self.cur_att = code
            if self.cur_mode not in (None, "rf_off"):
                yield from self._session()
                yield ("sleep", self.ctl.get("settle_ms", 1500) / 1000 if not self._is_demo() else 0.02)
            return
        if self._is_demo():
            self.sim.att_code = code
            yield ("sleep", 0.02)
            return
        mode = self.ctl.get("att_mode", "prompt")
        if mode == "none":
            raise _Skip("attenuator control not configured (Unit control page)")
        if mode == "can":
            c = getattr(self.host, "can_win", None)
            try:
                can_id = int(self.ctl["att_id"], 16)
                data = bytes(int(code if b.upper() == "XX" else int(b, 16)) for b in self.ctl["att_data"].split())
            except Exception:
                raise _TestError("invalid attenuator CAN template (ID / data)")
            if not c or not c.send_frame(c.FRAME(0, can_id, can_id > 0x7FF, False, None, data, bus=self.ctl["att_line"])):
                raise _TestError("CAN send failed for the attenuator command")
        else:
            ok = yield ("prompt", f"Set the EIRP attenuation (Atten_EIRP) to <b>{code} dB</b>."
                                  f"<br><br>Press OK when it is done.")
            if not ok:
                return
        yield ("sleep", self.ctl.get("settle_ms", 1500) / 1000)

    def _capture(self, what, center, fs, n, analyze, label, sr=None, filt=None):
        """Submit capture + analysis to the FSW worker; returns the analysis dict."""
        cfg = dict(self.cfg)
        sim = self.sim
        demo = self._is_demo()

        def job():
            dev = self.host.worker.dev
            if dev is None:
                raise RuntimeError("FSW not connected")
            if demo:
                time.sleep(0.05)
                x, fs_real, errs = sim.capture(center, fs, n, sr, filt), fs, []
            else:
                x, fs_real, errs = fsw_capture(dev, center, fs, n, cfg["autolevel"], cfg["ref_level"])
            if cfg.get("save_iq"):
                try:
                    os.makedirs(os.path.join(REPORT_DIR, "captures"), exist_ok=True)
                    nm = "".join(ch if ch.isalnum() else "_" for ch in label)[:40]
                    p = os.path.join(REPORT_DIR, "captures", f"{time.strftime('%Y%m%d_%H%M%S')}_{nm}.iq.tar")
                    bos_iqfile.write_iq_tar(p, x, fs_real, center, f"BOS station capture: {label}")
                except Exception:
                    pass
            r = analyze(x, fs_real)
            r["fsw_errors"] = errs
            r["t"] = time.time()
            return r
        if self.ctl.get("method") == "protocol" and self.cur_mode not in (None, "rf_off") and \
                time.monotonic() - getattr(self, "_sr_t", 0) > 20 and not self.live_mode:
            yield from self._session()                      # keep the session inside Time_SNR
            yield ("sleep", 0.6 if not self._is_demo() else 0.02)
        self.step_lbl.setText(f"{TEST_NAME.get(self.cur_test, self.cur_test)}: capturing {label} …")
        st, r = yield ("fsw", job)
        if st == "err":
            raise _TestError(f"capture {label}: {r}")
        for e in r.get("fsw_errors") or []:
            self.log(f"BOS station: FSW: {e}", "warn")
        return r

    def _mcfg(self, mod):
        """Settings for one modulation: its symbol rate and samples per symbol."""
        if mod == "BPSK":
            sr = self.cfg["sr_bpsk"]
            nsym = max(self.cfg["nsym"], 64000) if self.cfg.get("decode_ur", True) else self.cfg["nsym"]
            return dict(self.cfg, symbol_rate=sr, fs=sr * self.cfg["sps_bpsk"], nsym=nsym,
                        filter="NONE" if self.cfg.get("bpsk_rect", True) else "RRC")
        sr = self.cfg["sr_oqpsk"]
        return dict(self.cfg, symbol_rate=sr, fs=min(self.cfg["fs_oqpsk"], sr * 8),
                    filter="RRC" if self.cfg.get("oqpsk_rrc", True) else "NONE")

    def _obw_nom(self, mod):
        """Expected occupied bandwidth: symbol rate × (1 + roll-off), at most the FSW I/Q bandwidth."""
        nom = self._mcfg(mod)["symbol_rate"] * (1 + self.cfg["alpha"])
        return min(nom, self.cfg.get("fsw_bw") or nom)

    @staticmethod
    def _mod_n(c):
        """Samples of a modulation capture: nsym symbols, whole resampling blocks if resampled."""
        from fractions import Fraction
        fs = c["fs"]
        n = int(math.ceil(c["nsym"] * fs / c["symbol_rate"]))
        if abs(fs / c["symbol_rate"] - round(fs / c["symbol_rate"])) > 1e-6:      # resampled: whole blocks
            den = Fraction(c["symbol_rate"] * ba.DEMOD_SPS / fs).limit_denominator(1_000_000).denominator
            n = int(math.ceil(n / den)) * den
        return n

    def _mod_capture(self, mod, label=None):
        c = self._mcfg(mod)
        fs = c["fs"]
        n = self._mod_n(c)
        r = yield from self._capture("mod", c["carrier"], fs, n,
                                     lambda x, f: ba.analyze_modulated(x, f, c, mod), label or mod,
                                     sr=c["symbol_rate"], filt=c["filter"])
        return r

    def _supply_current(self):
        src = self.data.get("supply", "none")
        if self._is_demo():
            return self.sim.supply_current()
        if src == "none":
            return None
        kind, ch = src.split(":")
        ch = int(ch)
        if kind == "hmp":
            b = getattr(self.host, "bench", None)
            if b and b._hmp_ok and ch in b.hmp_last_meas:
                return b.hmp_last_meas[ch][1]
        else:
            n = getattr(self.host, "n67", None)
            if n and n.ok and ch in n.last_meas:
                return n.last_meas[ch][1]
        return None

    # ---- tests
    def _t_pre(self):
        T = TEST_NAME["pre"]
        yield ("sleep", 0.1)
        t = self.tlm_snapshot()
        if t.get("age_dop") is None:
            self._row(T, "BOS telemetry (IOK DOP)", None, "", "received", "FAIL", note="no BOS arrays received")
            return
        self._row(T, "Telemetry age", t["age_dop"], "s", f"≤ {self.lim['tlm_stale_s']:g}",
                  "PASS" if t["age_dop"] <= self.lim["tlm_stale_s"] else "FAIL")
        for k, name in (("pll_lmk", "Clock synth PLL"), ("pll_lmx (DAC)", "DAC clock PLL"),
                        ("pll_lmx (mod)", "LO synth LMX2594"), ("pll_jesd", "FPGA JESD PLL"),
                        ("pll_all", "FPGA PLLs"), ("jesd_err", "JESD204B link")):
            v = t.get(k)
            if v is not None:
                self._row(T, name, None, "", "locked / OK", "PASS" if v else "FAIL",
                          text=("locked" if v else "UNLOCKED") if k != "jesd_err" else ("OK" if v else "ERROR"))
        for k, nom in (("vdd_1v", 1.0), ("vdd_1_8v", 1.8), ("vdd_2_5v", 2.5)):
            v = t.get(k)
            if v is not None:
                tol = self.lim["rail_tol_pct"] / 100 * nom
                self._lim_row(T, f"FPGA rail {nom:g} V", v, "V", nom - tol, nom + tol, "{:.3f}")
        for k in ("Temp_DT1", "Temp_DT2", "Temp_DT3"):
            if t.get(k) is not None:
                self._lim_row(T, k.replace("Temp_DT", "Temperature "), t[k], "°C", None, self.lim["temp_max"], "{:.0f}")
        if t.get("Stat_10MHz") is not None and self.cfg.get("ext_ref"):
            self._row(T, "External 10 MHz (Stat_10MHz)", None, "", "valid", "PASS" if t["Stat_10MHz"] else "FAIL",
                      text="valid" if t["Stat_10MHz"] else "missing / invalid")
        if t.get("Sour_10MHz") is not None:
            want = 1 if self.cfg.get("ext_ref") else 0
            name = {0: "internal", 1: "external"}
            self._row(T, "10 MHz source in use (Sour_10MHz)", None, "", name[want],
                      "PASS" if t["Sour_10MHz"] == want else "FAIL", text=name.get(t["Sour_10MHz"], "?"))
        if t.get("PulsNumb_10MHz") is not None and self.cfg.get("ext_ref"):
            tol = self.lim.get("pulsnumb_tol", 5.0)
            self._lim_row(T, "PulsNumb_10MHz", t["PulsNumb_10MHz"], "", 10_000_000 - tol, 10_000_000 + tol, "{:,.0f}")
        if t.get("Err_Ref_LO") is not None:
            self._lim_row(T, "Err_Ref_LO", t["Err_Ref_LO"], "", -self.lim["err_ref_lo"], self.lim["err_ref_lo"], "{:.0f}")

    def _t_supply(self):
        T = TEST_NAME["supply"]
        if not self._is_demo() and self.data.get("supply", "none") == "none":
            raise _Skip("no supply output selected (Unit / signal page)")
        yield from self._set_mode("rf_off")
        i_off = self._supply_current()
        yield from self._set_mode("cw")
        yield ("sleep", 0.5)
        i_on = self._supply_current()
        if i_off is None or i_on is None:
            raise _TestError("supply not connected / no current reading")
        self.res["supply"] = {"i_off": i_off, "i_on": i_on}
        self._lim_row(T, "Current, RF off (standby)", i_off, "A", None, self.lim["i_standby_max"], "{:.3f}")
        self._lim_row(T, "Current, RF on (CW)", i_on, "A", None, self.lim["i_max"], "{:.3f}")
        self._lim_row(T, "PA current rise", i_on - i_off, "A", self.lim["pa_delta_i_min"], None, "{:.3f}")

    def _t_cw(self):
        T = TEST_NAME["cw"]
        c = self.cfg
        yield from self._set_mode("cw")
        fs = c["cw_fs"]
        r = yield from self._capture("cw", c["carrier"], fs, 1 << 15,
                                     lambda x, f: ba.analyze_cw(x, f, c, (c["pn_lo"], c["pn_hi"])), "carrier")
        self.res["cw"] = r
        self._add_capture("CW carrier", r)
        self._cw_rows(T, r)

    def _cw_rows(self, T, r):
        c = self.cfg
        if not r.get("present"):
            self._row(T, "Carrier present", None, "", "yes", "FAIL", text="NO SIGNAL")
            return
        self._lim_row(T, "Output power", r["power"], "dBm", c["power_nom"] - self.lim["power_tol_db"],
                      c["power_nom"] + self.lim["power_tol_db"])
        self._lim_row(T, "Carrier frequency error", r["cfe"], "Hz", -self.lim["cfe_max_hz"], self.lim["cfe_max_hz"], "{:+.1f}")
        self._row(T, "Carrier frequency error", r["cfe_ppm"], "ppm", "", "INFO", spec="{:+.4f}")
        self._lim_row(T, f"Integrated phase noise {r['pn_band'][0] / 1e3:g}…{r['pn_band'][1] / 1e3:g} kHz",
                      r["pn_deg"], "° RMS", None, self.lim["pn_max_deg"], "{:.3f}")
        self._lim_row(T, "Largest spur", r["spur_max_dbc"], "dBc", None, self.lim["spur_max_dbc"], "{:.1f}", none_ok=True)

    def _mod_test(self, mod):
        T = TEST_NAME[mod.lower()]
        yield from self._set_mode(mod.lower())
        r = yield from self._mod_capture(mod)
        self.res[f"mod_{mod}"] = r
        self._add_capture(mod, r)
        self._mod_rows(T, mod, r)

    def _mod_rows(self, T, mod, r):
        L = self.lim
        if not r.get("present"):
            self._row(T, "Signal present", None, "", "yes", "FAIL", text="NO SIGNAL")
            return
        self._lim_row(T, "Output power", r["power"], "dBm", self.cfg["power_nom"] - L["power_tol_db"],
                      self.cfg["power_nom"] + L["power_tol_db"])
        self._row(T, "Detected modulation", None, "", mod, "PASS" if r.get("detected") == mod else "FAIL",
                  text=r.get("detected", "?"))
        if "mer" not in r:
            self._row(T, "Demodulation", None, "", "", "FAIL", text=r.get("error", "failed"))
            return
        self._lim_row(T, "MER", r["mer"], "dB", L["mer_min"], None)
        self._lim_row(T, "EVM RMS", r["evm"], "%", None, L["evm_max"])
        self._lim_row(T, "Carrier frequency error", r["cfe"], "Hz", -L["cfe_max_hz"], L["cfe_max_hz"], "{:+.1f}")
        self._lim_row(T, "Symbol rate error", r["srerr"], "ppm", -L["srerr_max_ppm"], L["srerr_max_ppm"], "{:+.2f}")
        self._lim_row(T, "Carrier leakage (I/Q offset)", r["offset_dbc"], "dBc", None, L["iq_offset_max_dbc"], "{:.1f}")
        if _fin(r.get("gain_imb")):
            self._lim_row(T, "I/Q gain imbalance", r["gain_imb"], "dB", -L["gain_imb_max"], L["gain_imb_max"], "{:+.3f}")
            self._lim_row(T, "Quadrature error", r["quad_err"], "°", -L["quad_err_max"], L["quad_err_max"], "{:+.2f}")
        self._lim_row(T, "Phase error RMS (after I/Q corr.)", r["phase_err_res_deg"], "°", None, L["phase_err_max"])
        self._row(T, "Magnitude error RMS", r["mag_err_pct"], "%", "", "INFO")
        fw = r.get("freq_wander")
        if fw:
            self._row(T, "Carrier frequency wander (10 ms windows)", fw["pp_hz"], "Hz p-p", "", "INFO", spec="{:.0f}",
                      note=f"max {fw['max_rate_hz_s']:.0f} Hz/s over {fw['span_s']:.1f} s")
        self._row(T, "MER after I/Q correction", r["mer_corr"], "dB", "", "INFO")
        self._lim_row(T, "PA compression (PAPR loss)", r["compression_db"], "dB", None, L["compression_max_db"],
                      none_ok=r.get("compression_note", "n/a"))
        sp = r["spectrum"]
        rect = self._mcfg(mod)["filter"] == "NONE"
        self._lim_row(T, "ACLR (worst side)", float("nan") if rect else sp["aclr"], "dB", L["aclr_min"], None, "{:.1f}",
                      none_ok="n/a (rectangular pulses)" if rect else "n/a (capture narrower than 3 channels)")
        self._lim_row(T, "Occupied bandwidth 99 %", float("nan") if rect else sp["obw"] / 1e6, "MHz",
                      0.85 * self._obw_nom(mod) / 1e6, 1.15 * self._obw_nom(mod) / 1e6, "{:.3f}",
                      none_ok="n/a (rectangular pulses)")
        self._lim_row(T, "Spectrum asymmetry", sp["asym_db"], "dB", -L["asym_max_db"], L["asym_max_db"], "{:+.2f}")
        self._lim_row(T, "Largest spur", sp["spur_max_dbc"], "dBc", None, L["spur_max_dbc"], "{:.1f}", none_ok=True)

    def _t_bpsk(self):
        yield from self._mod_test("BPSK")
        r = self.res.get("mod_BPSK") or {}
        if r.get("decode"):
            self._decode_rows(TEST_NAME["bpsk"], r["decode"])

    def _decode_rows(self, T, d):
        if "error" in d:
            self._row(T, "Data decoding", None, "", "", "ERROR", note=d["error"])
            return
        self._row(T, "Frames (КБД) decoded", d["n_frames"], "", "≥ 1", "PASS" if d["n_frames"] else "FAIL",
                  spec="{:.0f}", note="" if d["n_frames"] else "synchromarker 0x1ACFFC1D not found")
        if not d["n_frames"]:
            self._row(T, "SP-L (Manchester) violations", 100 * d["manchester_violations"], "%", "", "INFO")
            return
        self._row(T, "RS uncorrectable frames", d["rs_fail"], "", "0", "PASS" if not d["rs_fail"] else "FAIL", spec="{:.0f}")
        self._row(T, "RS corrected bytes", d["rs_corrected"], "", "", "INFO", spec="{:.0f}")
        self._row(T, "Transport frame CRC errors", d["crc_fail"], "", "0", "PASS" if not d["crc_fail"] else "FAIL", spec="{:.0f}")
        if d["raw_bits"]:
            ok = d["ber"] <= 1e-7 or (d["ber"] == 0)
            self._row(T, "Channel BER (raw code bits)", d["ber"], "", "≤ 1e-7", "PASS" if ok else "FAIL", spec="{:.2e}",
                      note=f"{d['raw_bits']} bits" + (f", < {d['ber_upper']:.1e} (95 %)" if d["ber"] == 0 else ""))
        nchk = d.get("idle_checked", sum(f.get("idle_content_ok") is not None for f in d["frames"]))
        if nchk:
            self._row(T, "Idle frame content (RS-decoded frames)", None, "", "as table 4.4",
                      "PASS" if not d["idle_bad"] else "FAIL", text=f"{nchk - d['idle_bad']}/{nchk} ok")
        if d["scid"] is not None:
            self._row(T, "Spacecraft ID", d["scid"], "", f"{self.cfg.get('scid', 0)}", "PASS" if d["scid_ok"] else "FAIL",
                      spec="{:.0f}")
        self._row(T, "Frame counter gaps", d["vcfc_gaps"], "", "0", "PASS" if not d["vcfc_gaps"] else "FAIL", spec="{:.0f}")

    def _shr_rate(self):
        return bp.RATES[int(self.ctl.get("rate_shr", 4))]

    def _shr_rows(self, T, d):
        if "error" in d:
            self._row(T, "ШР despreading", None, "", "", "ERROR", note=d["error"])
            return
        self._row(T, "Preambles found (PRS 3)", d.get("preamble_found", 0), "", "≥ 1",
                  "PASS" if d.get("preamble_found") else "FAIL", spec="{:.0f}",
                  note=f"{d.get('rate', '?')} kbit/s, preamble {d.get('preamble_value', '')}")
        if d.get("despread_snr_db") is not None:
            self._row(T, "Despread SNR per preamble bit", d["despread_snr_db"], "dB", "", "INFO", spec="{:.1f}")
        if d.get("despread_eff") is not None:
            self._lim_row(T, "Despreading efficiency (preamble PRS)", 100 * d["despread_eff"], "%", 70, None, "{:.0f}")
        if d.get("marker") is not None and d.get("preamble_found"):
            self._row(T, "End-of-preamble marker 0xDA2B1E5E", None, "", "found", "PASS" if d["marker"] else "INFO",
                      text="found" if d["marker"] else "not in capture")
        if d.get("frames") is not None and d.get("marker") and "n_frames" in d:
            self._row(T, "КБД decoded (turbo 1/2)", d["n_frames"], "", "≥ 1", "PASS" if d["n_frames"] else "FAIL",
                      spec="{:.0f}")
            if d["n_frames"]:
                self._row(T, "Despread SNR per КБД bit", d.get("cbd_snr_db"), "dB", "", "INFO", spec="{:.1f}")
                self._lim_row(T, "Despreading efficiency (КБД PRS)", 100 * d.get("cbd_eff", float("nan")), "%", 70,
                              None, "{:.0f}")
                self._row(T, "Transport frame CRC errors", d["crc_fail"], "", "0", "PASS" if not d["crc_fail"] else "FAIL",
                          spec="{:.0f}")
                self._row(T, "Code-bit BER before turbo decoding", d["ber"], "", "", "INFO", spec="{:.2e}")
                self._row(T, "Idle frame content", None, "", "as table 4.4", "PASS" if not d["idle_bad"] else "FAIL",
                          text=f"{d['n_frames'] - d['idle_bad']}/{d['n_frames']} ok")
                self._row(T, "Frame counter gaps", d["vcfc_gaps"], "", "0", "PASS" if not d["vcfc_gaps"] else "FAIL",
                          spec="{:.0f}")
        elif d.get("note"):
            self._row(T, "КБД", None, "", "", "INFO", text=d["note"])

    def _t_shr(self):
        import bos_shr
        T = TEST_NAME["shr"]
        c = self._mcfg("OQPSK")
        fs, sr, rate = c["fs"], c["symbol_rate"], self._shr_rate()
        pre = (self.res.get("config") or {}).get("Preamb") or bos_shr.PREAMBLE
        n_pre = (self.res.get("config") or {}).get("Numb_Preamb") or 5
        spc = fs / sr / 2                                 # samples per chip
        sfp, sf = bos_shr.chips_per_bit(rate, True), bos_shr.chips_per_bit(rate)
        alpha = self.cfg.get("alpha", 0.25)

        def analyze(x, f):
            chips, _ = bos_shr.chips_from_iq(x, f, sr, alpha)
            return {"kind": "shr", "shr": bos_shr.decode_shr(chips, rate, pre, max_cbd=2)}
        # 1) preambles only (PRS 3 restarts every bit: any part of the stream can be despread)
        self.cur_mode = "shr_pre"
        if self.ctl.get("method") == "protocol":
            yield from self._session()
            yield ("sleep", 0.6 if not self._is_demo() else 0.02)
        elif not self._is_demo():
            ok = yield ("prompt", "Start a ШР session with <b>preambles only</b> (many preambles).")
            if not ok:
                return
        n = int(72 * sfp * spc)                          # ≥ 2 whole preambles at any phase
        if self._is_demo():
            sim = self.sim

            def job():
                x = sim.capture_shr(fs, n, rate, "pre", pre=pre)
                return analyze(x, fs)
            st, r = yield ("fsw", job)
        else:
            r = yield from self._capture("shr", c["carrier"], fs, n, analyze, f"ШР preambles {rate} kbit/s")
            st = "ok"
        if st == "err":
            raise _TestError(str(r))
        d = r["shr"]
        d["marker"] = None                               # preamble-only packet: no marker expected
        self.res["shr"] = d
        self._shr_rows(T + " · preambles", d)
        if rate < self.cfg.get("shr_decode_min", 512) or not d.get("preamble_found"):
            if rate < self.cfg.get("shr_decode_min", 512):
                self._row(T, "КБД decoding", None, "", "", "INFO",
                          text=f"skipped: {rate} kbit/s < {self.cfg.get('shr_decode_min', 512)} kbit/s "
                               f"(КБД = {17912 * sf / 1e6:.1f} M chips)")
            return
        # 2) full packet from the RF onset: arm the FSW (IF power trigger), then start the session
        self.cur_mode = "rf_off"
        if self.ctl.get("method") == "protocol":
            yield from self._session()
        n = int(((n_pre + 1) * 32 * sfp + 2 * bos_shr.CBD_SR_BITS * sf) * spc * 1.03) + 20000
        lvl = self.cfg["power_nom"] - self.cfg.get("loss_db", 0)
        if self._is_demo():
            sim = self.sim

            def fetch():
                x = sim.capture_shr(fs, n, rate, "kbd", pre=pre, n_pre=n_pre)
                return analyze(x, fs)
            self.cur_mode = "shr_kbd"
            yield from self._session()
            st, r = yield ("fsw", fetch)
        else:
            def arm():
                return fsw_arm(self.host.worker.dev, c["carrier"], fs, n, lvl + 5, lvl - 20)
            st, a = yield ("fsw", arm)
            if st == "err":
                raise _TestError(f"FSW trigger: {a}")
            for e in a[1]:
                self.log(f"BOS station: FSW: {e}", "warn")
            self.cur_mode = "shr_kbd"
            if self.ctl.get("method") == "protocol":
                yield from self._session()
            else:
                ok = yield ("prompt", "The FSW waits for the RF. Start a ШР session with <b>КБД</b> now.")
                if not ok:
                    return

            def fetch():
                x = fsw_fetch(self.host.worker.dev)
                return analyze(x, a[0])
            self.step_lbl.setText(f"{T}: waiting for the RF onset (FSW IF power trigger) …")
            st, r = yield ("fsw", fetch)
        if st == "err":
            raise _TestError(f"ШР capture: {r}")
        self.res["shr"] = r["shr"]
        self._shr_rows(T + " · КБД", r["shr"])

    # ---- offline: I/Q file
    def analyze_file(self):
        if self.running:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "I/Q recording", "", bos_iqfile.FILE_FILTER)
        if not path:
            return
        dlg = FileDialog(self, path, self.cfg)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        try:
            self._file = dlg.result_data()
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "I/Q file", f"{type(e).__name__}: {e}")
            return
        seg = SegmentDialog(self, self._file[0])
        if seg.exec_() != QtWidgets.QDialog.Accepted:
            return
        a, b = seg.selection()
        iq = self._file[0]
        self._file_seg = (a, b, len(iq.x))
        if (a, b) != (0, len(iq.x)):
            iq = bos_iqfile.IQData(iq.x[a:b], iq.fs, iq.center, iq.name, dict(iq.meta, segment=(a, b)))
            self._file = (iq,) + tuple(self._file[1:])
        self.start_plan(f"I/Q file: {os.path.basename(path)}", tests=["file"])

    def _t_file(self):
        iq, mode, ur, shr = self._file
        self._carrier_saved = self.cfg["carrier"]
        T = f"File {iq.name}"
        cfg = dict(self.cfg)
        msgs = []
        self.log(f"BOS station: analysing {iq.name}: {len(iq.x):,} samples, {iq.fs / 1e6:g} MS/s"
                 + (f", centre {iq.center / 1e9:.6f} GHz" if iq.center else ""))

        prog = {"f": None, "t0": time.time()}

        def job():
            return ba.analyze_file(iq.x, iq.fs, iq.center, cfg, mode, ur, shr, msgs.append,
                                   progress=lambda f: prog.__setitem__("f", f))
        self.step_lbl.setText(f"Analysing {iq.name} …")
        tick = QtCore.QTimer(self)

        def show():
            el = time.time() - prog["t0"]
            last = msgs[-1] if msgs else ""
            pc = f" {prog['f'] * 100:.0f} %" if prog["f"] is not None else ""
            self.step_lbl.setText(f"Analysing {iq.name} …{pc}  {el:.0f} s  {last}")
        tick.timeout.connect(show)
        tick.start(300)
        try:
            st, res = yield ("fsw", job)
        finally:
            tick.stop()
            tick.deleteLater()
        for m in msgs:
            self.log(f"BOS station: {m}")
        if st == "err":
            raise _TestError(str(res))
        idt = res.pop("_ident", {})
        self._row(T, "Signal type", None, "", "", "INFO",
                  text={"cw": "carrier (CW)", "narrow": "narrowband (УР, BPSK)", "wide": "wideband (ШР, OQPSK)",
                        "off": "no signal"}.get(idt.get("type"), "?") + (f", OBW {idt['obw'] / 1e6:.3f} MHz" if
                                                                          _fin(idt.get("obw")) else ""))
        if iq.center:
            self._row(T, "File centre frequency", iq.center / 1e9, "GHz", "", "INFO", spec="{:.9f}")
        seg = iq.meta.get("segment")
        if seg:
            self._row(T, "Analysed part", (seg[1] - seg[0]) / iq.fs, "s", "", "INFO", spec="{:.6f}",
                      note=f"{seg[0] / iq.fs:.6f} … {seg[1] / iq.fs:.6f} s of the recording")
        nom = res.pop("_nominal", None)
        if nom and abs(nom - self.cfg["carrier"]) > 1:
            self.cfg["carrier"] = nom                      # diagnosis against the channel actually used
        for r in res.values():
            if isinstance(r, dict) and r.get("carrier_measured"):
                self._row(T, "Carrier measured", r["carrier_measured"] / 1e9, "GHz", "", "INFO", spec="{:.9f}",
                          note=f"nearest channel n = {r.get('freq_code_n')} = {nom / 1e9:.6f} GHz" if nom else "")
                break
        self.res.update(res)
        if "cw" in res:
            self._add_capture("file: carrier", res["cw"])
            self._cw_rows("Carrier (CW)", res["cw"])
        for mod in ("BPSK", "OQPSK"):
            r = res.get(f"mod_{mod}")
            if r:
                self._add_capture(f"file: {mod}", r)
                if r.get("ur_rate_kbps"):
                    self._row(TEST_NAME[mod.lower()], "УР data rate (detected)", r["ur_rate_kbps"], "kbit/s", "",
                              "INFO", spec="{:.0f}")
                self._mod_rows(TEST_NAME[mod.lower()], mod, r)
                if r.get("decode"):
                    self._decode_rows(TEST_NAME[mod.lower()], r["decode"])
        if "shr" in res:
            self._shr_rows(TEST_NAME["shr"], res["shr"])

    def _t_ber(self):
        T = TEST_NAME["ber"]
        if not self.cfg.get("decode_ur", True):
            raise _Skip("УР decoding is switched off (Unit / signal page)")
        yield from self._set_mode("bpsk")
        tot = {"n_frames": 0, "rs_fail": 0, "rs_corrected": 0, "crc_fail": 0, "idle_bad": 0, "vcfc_gaps": 0,
               "raw_bits": 0, "err": 0, "frames": [], "manchester_violations": 0.0, "scid": None, "scid_ok": True}
        k = 0
        target = int(self.cfg.get("ber_bits", 1e6))
        while tot["raw_bits"] < target and k < 2000 and not self.abort_flag:
            k += 1
            r = yield from self._mod_capture("BPSK", f"BER {tot['raw_bits']:,}/{target:,} bits")
            d = r.get("decode") or {}
            if "frames" not in d:
                continue
            for key in ("n_frames", "rs_fail", "rs_corrected", "crc_fail", "idle_bad", "vcfc_gaps", "raw_bits"):
                tot[key] += d[key]
            tot["err"] += sum(f.get("raw_bit_err", 0) for f in d["frames"])
            tot["frames"] += d["frames"]
            tot["scid"] = d["scid"] if d["scid"] is not None else tot["scid"]
            tot["scid_ok"] &= d["scid_ok"]
            if k >= 3 and tot["n_frames"] == 0:
                break
        tot["ber"] = tot["err"] / tot["raw_bits"] if tot["raw_bits"] else float("nan")
        tot["ber_upper"] = 3.0 / tot["raw_bits"] if tot["raw_bits"] and not tot["err"] else float("nan")
        self.res["ber"] = tot
        self._decode_rows(T, tot)

    def _t_oqpsk(self):
        yield from self._mod_test("OQPSK")

    def _t_att(self):
        T = TEST_NAME["att"]
        c = self.cfg
        yield from self._set_mode("cw")
        codes = ba.ATT_CODES_DEFAULT
        powers = []
        for k, code in enumerate(codes):
            yield from self._set_att(code)
            r = yield from self._capture("tone", c["carrier"], c["cw_fs"], 1 << 13,
                                         lambda x, f: {"tone": ba.tone_power(x, f, c["loss_db"])},
                                         f"code {code} ({k + 1}/{len(codes)})")
            powers.append(r["tone"][1])
        yield from self._set_att(c["att_code"])
        fit = ba.fit_attenuator(codes, powers)
        self.res["att"] = fit
        if "error" in fit:
            raise _TestError(fit["error"])
        for b in fit["bits"]:
            self._lim_row(T, f"{b['nominal']:g} dB step", b["measured"], "dB",
                          b["nominal"] - max(self.lim["att_step_max_db"], 0.05 * b["nominal"]),
                          b["nominal"] + max(self.lim["att_step_max_db"], 0.05 * b["nominal"]), "{:.3f}")
        self._row(T, "Fit residual RMS", fit["residual_rms"], "dB", "", "INFO", spec="{:.3f}")
        self._row(T, "Max deviation from nominal", fit["max_err"], "dB", "", "INFO", spec="{:.2f}")
        self._draw_att(fit)

    def _oqpsk_band_limited(self):
        return self.cfg.get("fsw_bw", 0) < self.cfg["sr_oqpsk"] * (1 + self.cfg["alpha"]) * 1.2

    def _t_lin(self):
        T = TEST_NAME["lin"]
        c = self.cfg
        mod = "BPSK" if self._oqpsk_band_limited() else "OQPSK"     # same PA; BPSK fits any FSW bandwidth
        yield from self._set_mode(mod.lower())
        a = yield from self._mod_capture(mod, "nominal attenuation")
        yield from self._set_att(c["att_code"] + c["lin_step"])
        b = yield from self._mod_capture(mod, f"+{c['lin_step']:g} dB attenuation")
        yield from self._set_att(c["att_code"])
        self.res["lin"] = {"a": a, "b": b}
        if _fin(a.get("power")) and _fin(b.get("power")):
            gc = c["lin_step"] - (a["power"] - b["power"])
            self._lim_row(T, f"Gain compression (expected −{c['lin_step']:g} dB step)", gc, "dB", None,
                          self.lim["compression_max_db"], "{:.2f}")
            if gc > self.lim["compression_max_db"]:
                self.res.setdefault("extra", []).append(ba.Finding(
                    "fail", f"PA gain compression {gc:.2f} dB",
                    f"Atten_EIRP +{c['lin_step']:g} dB lowered the output by only {a['power'] - b['power']:.2f} dB: "
                    f"at the nominal setting the PA (CHA3397) is compressed. This works for constant-envelope BPSK too.",
                    {"PA": 0.8, "ATT": 0.2}, ["Check the PA bias / supply", "Reduce the drive (Atten_EIRP)"], "linearity"))
        for lbl, r in (("nominal", a), (f"+{c['lin_step']:g} dB", b)):
            self._row(T, f"PAPR loss at {lbl}", r.get("compression_db"), "dB", "", "INFO")
            self._row(T, f"ACLR at {lbl}", r.get("spectrum", {}).get("aclr"), "dB", "", "INFO", spec="{:.1f}")
        da = a.get("compression_db", float("nan")) - b.get("compression_db", float("nan"))
        if _fin(da):
            st = "FAIL" if da > self.lim["compression_max_db"] else "PASS"
            self._row(T, "Compression recovered with more attenuation", da, "dB",
                      f"≤ {self.lim['compression_max_db']:g}", st, spec="{:.2f}")
            if st == "FAIL":
                self.res.setdefault("extra", []).append(ba.Finding(
                    "fail", "PA compression confirmed by the linearity test",
                    f"With {c['lin_step']:g} dB more attenuation the PAPR loss drops by {da:.2f} dB: the PA "
                    f"(CHA3397) is driven into compression at the nominal setting.",
                    {"PA": 0.8, "ATT": 0.2}, ["Check the PA bias / supply", "Reduce the drive (attenuator code)"], "linearity"))
            else:
                if _fin(a.get("compression_db")) and a["compression_db"] > self.lim["compression_max_db"]:
                    self.res.setdefault("extra", []).append(ba.Finding(
                        "warn", "PAPR loss does not change with attenuation",
                        "The peak reduction is not from the PA: clipping / scaling in the FPGA or the DAC full scale.",
                        {"FPGA": 0.5, "DAC": 0.4, "MIX": 0.1}, ["Check digital scaling / back-off in the FPGA"], "linearity"))

    def _t_harm(self):
        T = TEST_NAME["harm"]
        c = self.cfg
        yield from self._set_mode("cw")
        main = yield from self._capture("tone", c["carrier"], c["cw_fs"], 1 << 13,
                                        lambda x, f: {"tone": ba.tone_power(x, f, c["loss_db"])}, "carrier")
        p0 = main["tone"][1]
        m = c["lo_mult"]
        lines = []
        for k in range(1, 2 * m):
            if k == m:
                continue
            f = c["carrier"] * k / m
            if f > 50e9 or f < 1e6:
                continue
            if self._is_demo():
                lvl = self.sim.tone(f) + c["loss_db"]
                yield ("sleep", 0.02)
            else:
                r = yield from self._capture("tone", f, 2e6, 1 << 13,
                                             lambda x, fs: {"tone": ba.tone_power(x, fs, c["loss_db"])},
                                             f"{f / 1e9:.3f} GHz")
                lvl = r["tone"][1]
            name = f"{k}/{m} × carrier" + (" (LO)" if k == 1 else "")
            lines.append({"f": f, "dbc": lvl - p0, "name": name})
            self._lim_row(T, f"{name}  {f / 1e9:.4f} GHz", lvl - p0, "dBc", None, self.lim["lo_harm_max_dbc"], "{:.1f}")
        self.res["harm"] = {"carrier_dbm": p0, "lines": lines}

    def _t_drift(self):
        T = TEST_NAME["drift"]
        c = self.cfg
        yield from self._set_mode("oqpsk")
        d = {"t": [], "power": [], "cfe": [], "mer": [], "temp": []}
        self.res["drift"] = d
        for k in range(int(c["drift_n"])):
            r = yield from self._mod_capture("OQPSK", f"stability {k + 1}/{int(c['drift_n'])}")
            if "mer" in r:
                d["t"].append(r["t"])
                d["power"].append(r["power"])
                d["cfe"].append(r["cfe"])
                d["mer"].append(r["mer"])
                tm = self.tlm.get("Temp_DT1")
                if tm is not None:
                    d["temp"].append(float(tm))
            self._add_capture(f"Stability {k + 1}", r)
            if k < int(c["drift_n"]) - 1:
                yield ("sleep", c["drift_s"] if not self._is_demo() else min(1.0, c["drift_s"]))
        if len(d["t"]) >= 2:
            self._lim_row(T, "Power drift (peak-peak)", float(np.ptp(d["power"])), "dB", None, self.lim["drift_power_db"])
            self._lim_row(T, "Frequency drift (peak-peak)", float(np.ptp(d["cfe"])), "Hz", None, self.lim["drift_cfe_hz"], "{:.0f}")
            self._lim_row(T, "Worst MER", float(np.min(d["mer"])), "dB", self.lim["mer_min"], None)
            if d["temp"]:
                self._row(T, "Temperature change", float(np.ptp(d["temp"])), "°C", "", "INFO", spec="{:.1f}")

    def _t_live(self):
        mod = self.live_mod.currentText()
        n = 0
        while not self.abort_flag:
            n += 1
            self.step_lbl.setText(f"Live diagnose ({mod}), capture {n}")
            if mod == "CW":
                c = self.cfg
                r = yield from self._capture("cw", c["carrier"], c["cw_fs"], 1 << 15,
                                             lambda x, f: ba.analyze_cw(x, f, c, (c["pn_lo"], c["pn_hi"])), "carrier")
                self.res = {"cw": r}
            else:
                r = yield from self._mod_capture(mod)
                self.res = {f"mod_{mod}": r}
            self.sig_src.blockSignals(True)
            self.sig_src.clear()
            self.sig_src.blockSignals(False)
            self._add_capture(f"live {mod}", r)
            self._update_diagnosis()
            yield ("sleep", 0.2)

    # ================================================================ results
    def _row(self, test, name, value, unit, limit, status, spec="{:.2f}", text=None, note=""):
        vtxt = text if text is not None else fmt(value, spec)
        self.rows.append({"test": test, "name": name, "value": value, "text": vtxt, "unit": unit, "limit": limit,
                          "status": status, "note": note})
        r = self.table.rowCount()
        self.table.insertRow(r)
        color = {"PASS": C_OK, "FAIL": C_FAIL, "ERROR": C_FAIL, "SKIP": C_DIM, "INFO": C_INFO,
                 "WARN": C_WARN}.get(status, C_DIM)
        for c, t in enumerate((test, name, vtxt, unit, limit, status + (f" - {note}" if note else ""))):
            it = QtWidgets.QTableWidgetItem(str(t))
            if c == 5:
                it.setForeground(QtGui.QColor(color))
                f = it.font()
                f.setBold(True)
                it.setFont(f)
            self.table.setItem(r, c, it)
        self.table.scrollToBottom()

    def _lim_row(self, test, name, value, unit, lo, hi, spec="{:.2f}", none_ok=False):
        if lo is not None and hi is not None:
            lt = f"{spec.format(lo)} … {spec.format(hi)}"
        elif lo is not None:
            lt = f"≥ {spec.format(lo)}"
        else:
            lt = f"≤ {spec.format(hi)}"
        if not _fin(value) and none_ok:
            self._row(test, name, value, unit, lt, "PASS" if none_ok is True else "INFO", spec,
                      text="none found" if none_ok is True else none_ok)
            return
        if not _fin(value):
            st = "FAIL"
        else:
            st = "FAIL" if (lo is not None and value < lo) or (hi is not None and value > hi) else "PASS"
        self._row(test, name, value, unit, lt, st, spec)

    def _add_capture(self, label, r):
        self.res.setdefault("_caps", []).append((label, r))
        self.sig_src.addItem(label)
        self.sig_src.setCurrentIndex(self.sig_src.count() - 1)

    def _draw_signal(self, *_):
        caps = self.res.get("_caps", [])
        k = self.sig_src.currentIndex()
        if not 0 <= k < len(caps):
            return
        label, r = caps[k]
        if r.get("i") is not None:
            i, q = np.asarray(r["i"]), np.asarray(r["q"])
            step = max(1, len(i) // 6000)
            self.const_pts.setData(x=i[::step], y=q[::step])
        else:
            self.const_pts.setData([], [])
        sp = r.get("spectrum", r)
        if sp.get("spec_f") is not None:
            self.spec_curve.setData(np.asarray(sp["spec_f"]), np.asarray(sp["spec_p"]))
            self.spec.setLabel("left", "dBc/Hz" if r.get("kind") == "cw" else "dBm/Hz")
        pe = r.get("phase_err_series")
        self.phase_curve.setData(np.asarray(pe) if pe is not None else [])
        lines = [f"{label}"]
        for key, name, spec_ in (("power", "Power, dBm", "{:.2f}"), ("mer", "MER, dB", "{:.2f}"),
                                 ("evm", "EVM, %", "{:.2f}"), ("cfe", "Carrier freq error, Hz", "{:+.1f}"),
                                 ("cfe_ppm", "  … ppm", "{:+.4f}"), ("srerr", "Symbol rate error, ppm", "{:+.2f}"),
                                 ("offset_dbc", "Carrier leakage, dBc", "{:.1f}"), ("gain_imb", "Gain imbalance, dB", "{:+.3f}"),
                                 ("quad_err", "Quadrature error, °", "{:+.2f}"), ("irr", "Image rejection, dB", "{:.1f}"),
                                 ("phase_err_res_deg", "Phase error (res.), °", "{:.2f}"),
                                 ("mag_err_pct", "Magnitude error, %", "{:.2f}"), ("mer_corr", "MER after I/Q corr., dB", "{:.2f}"),
                                 ("compression_db", "PAPR loss, dB", "{:.2f}"), ("pn_deg", "Integrated PN, ° RMS", "{:.3f}"),
                                 ("snr_floor", "SNR over FSW floor, dB", "{:.1f}"), ("detected", "Detected modulation", "{}")):
            v = r.get(key)
            if v is not None:
                lines.append(f"{name:<26} {v if isinstance(v, str) else fmt(v, spec_)}")
        s = r.get("spectrum")
        if s:
            lines += [f"{'OBW 99 %, MHz':<26} {fmt(s['obw'] / 1e6, '{:.3f}')}",
                      f"{'ACLR low / up, dB':<26} {fmt(s['aclr_l'], '{:.1f}')} / {fmt(s['aclr_u'], '{:.1f}')}",
                      f"{'Asymmetry, dB':<26} {fmt(s['asym_db'], '{:+.2f}')}"]
            for f0, lv in s.get("spurs", [])[:4]:
                lines.append(f"{'Spur':<26} {f0 / 1e6:+.3f} MHz  {lv:.1f} dBc")
        d = r.get("decode")
        if d and "frames" in d:
            lines += ["", f"УР data: {d['n_frames']} КБД, RS corrected {d['rs_corrected']} bytes, "
                          f"uncorrectable {d['rs_fail']}, CRC errors {d['crc_fail']}, BER {fmt(d['ber'], '{:.2e}')}"
                          + (" (polarity inverted)" if d.get("inverted") else "")]
            for f in d["frames"][:6]:
                lines.append(f"  SCID {f['scid']} VC {f['vcid']} cnt {f['vcfc']:3d} {'idle' if f['idle'] else 'data'}"
                             f"  RS {f['rs']}  CRC {'ok' if f['crc_ok'] else 'BAD'}"
                             + ("" if f.get("idle_content_ok", True) else "  content BAD"))
        self.sig_info.setPlainText("\n".join(lines))

    def _draw_att(self, fit):
        codes, pw, nom = np.array(fit["codes"]), np.array(fit["powers"]), np.array(fit["nominal"])
        self.att_meas.setData(x=codes, y=pw)
        o = np.argsort(codes)
        self.att_nom.setData(codes[o], nom[o])
        for b in fit["bits"]:
            for c, t in enumerate((f"{b['nominal']:g}", f"{b['measured']:.3f}", f"{b['error']:+.3f}", b["state"])):
                it = QtWidgets.QTableWidgetItem(t)
                if c == 3:
                    it.setForeground(QtGui.QColor(C_OK if b["state"] == "ok" else C_FAIL))
                self.att_table.setItem(b["bit"], c, it)

    def _update_diagnosis(self):
        res = {k: v for k, v in self.res.items() if not k.startswith("_") and k != "extra"}
        offline = self.run_info.get("tests") == ["file"]
        self.findings = ba.diagnose(res, {} if offline else self.tlm_snapshot(), self.cfg, self.lim) + \
            list(self.res.get("extra", []))
        if offline:
            self.findings = [f for f in self.findings if f.source != "telemetry"]
        if any(f.title.startswith("PA gain compression") for f in self.findings):
            for f in self.findings:                          # small steps at low attenuation = PA compression
                if f.source == "attenuator" and f.sev == "warn":
                    f.sev, f.suspects = "info", {"PA": 0.3}
                    f.detail += " Explained by the PA gain compression (steps shrink where the PA compresses)."
        if any(f.sev in ("fail", "warn") for f in self.findings):
            self.findings = [f for f in self.findings if f.sev != "ok"]
        order = {"fail": 0, "warn": 1, "info": 2, "ok": 3}
        self.findings.sort(key=lambda f: order.get(f.sev, 9))
        self.diagram.set_scores(ba.suspicion(self.findings))
        self._filter_findings(self.diagram.selected)
        self._show_corrections()
        if hasattr(self, "fr_table"):
            self._fill_frames()

    def _filter_findings(self, block):
        self.find_list.clear()
        shown = [f for f in self.findings if not block or f.suspects.get(block, 0) > 0]
        self._shown = shown
        for f in shown:
            it = QtWidgets.QListWidgetItem(f"{SEV_ICON[f.sev]}  {_ru(f.title) if _ru_on() else f.title}")
            it.setForeground(QtGui.QColor(SEV_COLOR[f.sev]))
            self.find_list.addItem(it)
        nf = sum(f.sev == "fail" for f in self.findings)
        nw = sum(f.sev == "warn" for f in self.findings)
        self.find_hdr.setText(f"<b>Findings</b> — {nf} fault(s), {nw} warning(s)"
                              + (f" — filtered: {ba.BLOCK_NAME[block]}" if block else ""))
        if shown:
            self.find_list.setCurrentRow(0)
        else:
            self.find_detail.clear()

    def _show_finding(self, row):
        shown = getattr(self, "_shown", [])
        if not 0 <= row < len(shown):
            return
        self.find_detail.setHtml(self._finding_html(shown[row]))

    @staticmethod
    def _finding_html(f, L=None, R=None):
        if L is None:                                   # on screen: follow the interface language (Russian)
            ruo = _ru_on()
            L = (lambda x: _ru(x)) if ruo else (lambda x: x)
            R = (lambda x: __import__("bos_ru").REPORT.get(x, x)) if ruo else (lambda x: x)
        sus = sorted(f.suspects.items(), key=lambda kv: -kv[1])
        h = [f'<h3 style="color:{SEV_COLOR[f.sev]}">{SEV_ICON[f.sev]} {html.escape(L(f.title))}</h3>',
             f"<p>{html.escape(L(f.detail))}</p>"]
        if sus:
            h.append(f"<p><b>{R('Most likely location')}</b></p><table cellspacing=4>")
            for b, w in sus:
                bar = "█" * max(1, int(w * 20))
                h.append(f"<tr><td>{html.escape(L(ba.BLOCK_NAME.get(b, b)))}</td>"
                         f'<td style="color:{C_WARN}">{bar}</td><td>{w * 100:.0f} %</td></tr>')
            h.append("</table>")
        if f.actions:
            h.append(f"<p><b>{R('What to do')}</b></p><ol>" + "".join(f"<li>{html.escape(L(a))}</li>" for a in f.actions)
                     + "</ol>")
        if f.source:
            h.append(f'<p style="color:{C_DIM}">{R("source")}: {html.escape(R(f.source) if _ru_on() else f.source)}</p>')
        return "".join(h)

    def _show_corrections(self):
        res = {k: v for k, v in self.res.items() if not k.startswith("_")}
        cfg = dict(self.cfg)
        self.corr_rows = ba.corrections(res, cfg)
        self.corr.setRowCount(len(self.corr_rows))
        for r, row in enumerate(self.corr_rows):
            for c, t in enumerate(row):
                self.corr.setItem(r, c, QtWidgets.QTableWidgetItem(_ru(t) if _ru_on() else t))

    def _copy_corr(self):
        QtWidgets.QApplication.clipboard().setText(
            "\n".join("\t".join(r) for r in getattr(self, "corr_rows", [])))

    # ================================================================ finish
    def _conclude(self):
        if self.live_mode:
            self._finish("ABORTED", "live diagnose ended")
            return
        statuses = [r["status"] for r in self.rows]
        if "FAIL" in statuses or "ERROR" in statuses or any(f.sev == "fail" for f in self.findings):
            v = "FAIL"
        elif any(f.sev == "warn" for f in self.findings):
            v = "PASS*"
        else:
            v = "PASS"
        self._finish(v, self._summary())

    def _summary(self):
        nf = sum(f.sev == "fail" for f in self.findings)
        nw = sum(f.sev == "warn" for f in self.findings)
        s = ba.suspicion(self.findings)
        top = [b for b, v in sorted(s.items(), key=lambda kv: -kv[1]) if v >= 0.3][:2]
        txt = f"{nf} fault(s), {nw} warning(s)"
        if top:
            txt += "; look at " + ", ".join(ba.BLOCK_NAME[b] for b in top)
        return txt

    def _finish(self, verdict, summary):
        if not self.running or getattr(self, "_finishing", False):
            return
        self._finishing = True
        try:
            self._finish2(verdict, summary)
        finally:
            self._finishing = False
            self.running = False

    def _finish2(self, verdict, summary):
        self.pending = None
        if self.ctl.get("method") == "protocol" and self.cur_mode not in (None, "rf_off"):
            self._send_now(0x01)                            # leave the unit in standby, RF off
        self.cur_mode = None
        self.wait_timer.stop()
        self.run_btn.setText("▶ Run test plan")
        if self.live_btn.isChecked():
            self.live_btn.blockSignals(True)
            self.live_btn.setChecked(False)
            self.live_btn.blockSignals(False)
        live = self.live_mode
        self.live_mode = False
        self.cur_test = ""
        if not live:
            self.verdict = verdict
            self._show_verdict(verdict)
            self.step_lbl.setText(f"Finished: {verdict} — {summary}")
            self.log(f"BOS station: plan '{self.run_info.get('plan')}' {verdict}: {summary}",
                     "ok" if verdict == "PASS" else "warn" if verdict == "PASS*" else "err")
            if verdict != "ABORTED" or self.rows:
                try:
                    self.last_report = self._write_report(verdict, summary)
                    self.report_btn.setEnabled(True)
                    self.log(f"BOS station: report {self.last_report}", "info")
                except Exception as e:
                    self.log(f"BOS station: report not written: {type(e).__name__}: {e}", "err")
            if hasattr(self, "sum_verdict"):
                self.sum_verdict.setText(f"{self.run_info.get('serial') or 'unit'}: {verdict}")
                self.sum_verdict.setStyleSheet(f"font-weight:700;color:{self._vcolor(verdict)};")
                self.sum_text.setText(summary)
        else:
            self._show_verdict(self.verdict)
            self.step_lbl.setText(summary)
        if getattr(self, "_carrier_saved", None):          # file analysis used the measured channel
            self.cfg["carrier"] = self._carrier_saved
            self._carrier_saved = None
        self.running = False
        done, self.on_done = self.on_done, None
        if done:
            done(verdict, summary)

    @staticmethod
    def _vcolor(v):
        return {"PASS": C_OK, "PASS*": C_WARN, "FAIL": C_FAIL, "RUNNING": C_INFO}.get(v, C_DIM)

    def _show_verdict(self, v):
        text = {"": "NOT TESTED", "PASS*": "PASS (warnings)"}.get(v, v)
        c = self._vcolor(v)
        self.verdict_lbl.setText(text)
        self.verdict_lbl.setStyleSheet(f"font-size:18px;font-weight:800;color:{c};border:2px solid {c};"
                                       f"border-radius:6px;padding:4px 12px;")

    # ---- report
    def _img(self, w):
        buf = QtCore.QBuffer()
        buf.open(QtCore.QIODevice.WriteOnly)
        w.grab().save(buf, "PNG")
        return base64.b64encode(bytes(buf.data())).decode()

    def _write_report(self, verdict, summary):
        os.makedirs(REPORT_DIR, exist_ok=True)
        ri = self.run_info
        stamp = _dt.datetime.fromtimestamp(ri["start"]).strftime("%Y-%m-%d_%H%M%S")
        sn = "".join(ch for ch in (ri["serial"] or "unit") if ch.isalnum() or ch in "-_") or "unit"
        path = os.path.join(REPORT_DIR, f"BOS_{sn}_{stamp}.html")
        e = html.escape
        ruo = _ru_on()
        L = (lambda x: _ru(x)) if ruo else (lambda x: x)          # report in Russian when the UI is Russian
        R = (lambda x: __import__("bos_ru").REPORT.get(x, x)) if ruo else (lambda x: x)
        U = (lambda x: __import__("bos_ru").UNITS.get(x, x)) if ruo else (lambda x: x)
        vc = self._vcolor(verdict)
        h = ["<!doctype html><html><head><meta charset='utf-8'><title>" + R('BOS test report') + "</title><style>"
             "body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#1f2328}"
             "table{border-collapse:collapse;margin:8px 0}td,th{border:1px solid #d0d7de;padding:3px 8px;font-size:13px}"
             "th{background:#f6f8fa;text-align:left}.PASS{color:#1a7f37;font-weight:600}.FAIL,.ERROR{color:#cf222e;font-weight:600}"
             ".INFO{color:#0969da}.SKIP{color:#6e7781}h2{border-bottom:1px solid #d0d7de;padding-bottom:4px}"
             "img{max-width:100%;border:1px solid #d0d7de;margin:4px 0}</style></head><body>",
             f"<h1>{R('BOS modulator test report')}</h1>",
             f"<p style='font-size:22px;font-weight:800;color:{vc}'>{e(R('Verdict'))}: {e(L(verdict))}</p><p>{e(L(summary))}</p>",
             "<table>"]
        info = [("Unit serial", ri["serial"] or "—"), ("Operator", ri["operator"] or "—"),
                ("Date", _dt.datetime.fromtimestamp(ri["start"]).strftime("%Y-%m-%d %H:%M:%S")),
                ("Duration", f"{time.time() - ri['start']:.0f} {U('s')}"), ("Test plan", ri["plan"]),
                ("Tests", ", ".join(L(TEST_NAME.get(t, t)) for t in ri["tests"])),
                ("Carrier / symbol rates", f"{self.cfg['carrier'] / 1e9:.6f} {U('GHz')} / BPSK {self.cfg['sr_bpsk'] / 1e3:g} {U('ksym/s')}, "
                                           f"OQPSK {self.cfg['sr_oqpsk'] / 1e6:g} {U('Msym/s')}, "
                                          f"RRC α {self.cfg['alpha']:g}"),
                ("Nominal power / att. code / cable loss", f"{self.cfg['power_nom']:g} {U('dBm')} / {self.cfg['att_code']} / "
                                                           f"{self.cfg['loss_db']:g} {U('dB')}"),
                ("FSW", getattr(self.host, "idn_label", None).text().replace("\n", " · ")
                 if hasattr(self.host, "idn_label") else "—")]
        if ri.get("demo"):
            info.append(("Mode", "DEMO (simulated BOS) — injected faults: " +
                         (", ".join(ri["faults"]) if ri["faults"] else "none")))
        h += [f"<tr><th>{e(R(k))}</th><td>{e(str(v))}</td></tr>" for k, v in info] + ["</table>"]
        h.append(f"<h2>{R('Diagnosis')}</h2>")
        h.append(f"<img src='data:image/png;base64,{self._img(self.diagram)}'>")
        for f in self.findings:
            h.append(self._finding_html(f, L, R).replace(C_FAIL, "#cf222e").replace(C_WARN, "#9a6700")
                     .replace(C_OK, "#1a7f37").replace(C_DIM, "#6e7781"))
        h.append(f"<h2>{R('Measurements')}</h2><table><tr><th>{R('Test')}</th><th>{R('Parameter')}</th>"
                 f"<th>{R('Value')}</th><th>{R('Unit')}</th>"
                 f"<th>{R('Limit')}</th><th>{R('Result')}</th></tr>")
        for r in self.rows:
            h.append(f"<tr><td>{e(L(r['test']))}</td><td>{e(L(r['name']))}</td><td>{e(L(r['text']))}</td>"
                     f"<td>{e(U(r['unit']))}</td><td>{e(str(r['limit']))}</td><td class='{r['status']}'>{e(R(r['status']))}"
                     f"{(' - ' + e(L(r['note']))) if r['note'] else ''}</td></tr>")
        h.append("</table>")
        if getattr(self, "corr_rows", None):
            h.append(f"<h2>{R('Suggested corrections')}</h2><table><tr><th>{R('Adjustment')}</th><th>{R('Value')}</th>"
                     f"<th>{R('How')}</th></tr>")
            h += [f"<tr><td>{e(L(a))}</td><td>{e(L(b))}</td><td>{e(L(c))}</td></tr>" for a, b, c in self.corr_rows]
            h.append("</table>")
        tl = self.tlm_snapshot()
        if tl.get("age_dop") is not None:
            h.append(f"<h2>{R('BOS telemetry at the end of the test')}</h2><table>")
            for k in ("pll_lmk", "pll_lmx (DAC)", "pll_lmx (mod)", "pll_jesd", "pll_all", "jesd_err", "vdd_1v",
                      "vdd_1_8v", "vdd_2_5v", "Temp_DT1", "Temp_DT2", "Temp_DT3", "Stat_10MHz", "Sour_10MHz",
                      "Err_Ref_LO", "Soft_MORE_Ver"):
                if k in tl:
                    v = tl[k]
                    h.append(f"<tr><th>{e(k)}</th><td>{e(f'{v:.3f}' if isinstance(v, float) else str(v))}</td></tr>")
            h.append("</table>")
        h.append(f"<h2>{R('Signal')}</h2>")
        caps = self.res.get("_caps", [])
        keep = self.sig_src.currentIndex()
        for k, (label, r) in enumerate(caps[:8]):
            self.sig_src.setCurrentIndex(k)
            QtWidgets.QApplication.processEvents()
            h.append(f"<h3>{e(label)}</h3><table><tr><td><img src='data:image/png;base64,{self._img(self.const)}' "
                     f"width=420></td><td><img src='data:image/png;base64,{self._img(self.spec)}' width=520></td></tr></table>")
        if keep >= 0:
            self.sig_src.setCurrentIndex(keep)
        if "att" in self.res and "bits" in self.res["att"]:
            h.append(f"<h2>{R('Attenuator')}</h2><img src='data:image/png;base64,{self._img(self.att_plot)}' width=700>")
        h.append(f"<p style='color:#6e7781'>KTR TEST · BOS Test Station · {e(stamp)}</p></body></html>")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(h))
        self._append_history(verdict, summary, path)
        return path

    def _append_history(self, verdict, summary, path):
        ri = self.run_info
        new = not os.path.isfile(HISTORY_FILE)
        g = self.res

        def val(key, sub):
            r = g.get(key)
            return f"{r[sub]:.3f}" if isinstance(r, dict) and _fin(r.get(sub)) else ""
        s = ba.suspicion(self.findings)
        top = max(s, key=s.get) if s and max(s.values()) >= 0.3 else ""
        row = [_dt.datetime.fromtimestamp(ri["start"]).strftime("%Y-%m-%d %H:%M:%S"), ri["serial"], ri["operator"],
               ri["plan"], verdict, sum(f.sev == "fail" for f in self.findings),
               sum(f.sev == "warn" for f in self.findings), val("mod_BPSK", "mer"), val("mod_OQPSK", "mer"),
               val("cw", "power") or val("mod_OQPSK", "power"), val("cw", "cfe") or val("mod_OQPSK", "cfe"),
               top, summary, os.path.basename(path)]
        with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["Time", "Serial", "Operator", "Plan", "Verdict", "Faults", "Warnings", "MER BPSK dB",
                            "MER OQPSK dB", "Power dBm", "CFE Hz", "Top suspect", "Summary", "Report"])
            w.writerow(row)
        self._load_history()

    def _load_history(self, *_):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
        except FileNotFoundError:
            rows = []
        if not rows:
            self.hist.setRowCount(0)
            return
        hdr, data = rows[0], rows[1:]
        flt = self.hist_filter.text().strip().lower()
        if flt:
            data = [r for r in data if flt in " ".join(r).lower()]
        data = data[::-1]
        self.hist.setColumnCount(len(hdr))
        self.hist.setHorizontalHeaderLabels(hdr)
        self.hist.setRowCount(len(data))
        for r, row in enumerate(data):
            for c, t in enumerate(row):
                it = QtWidgets.QTableWidgetItem(t)
                if hdr[c] == "Verdict":
                    it.setForeground(QtGui.QColor(self._vcolor(t)))
                self.hist.setItem(r, c, it)
        self.hist.resizeColumnsToContents()
        # which blocks fail most often (helps spot assembly / supplier problems)
        if "Top suspect" in hdr:
            k = hdr.index("Top suspect")
            cnt = {}
            for row in data:
                if len(row) > k and row[k]:
                    cnt[row[k]] = cnt.get(row[k], 0) + 1
            n = len(data)
            fails = sum(1 for row in data if len(row) > 4 and row[4] == "FAIL")
            self.fail_stats.setText(f"{n} run(s), {fails} FAIL" + (
                "; most frequent suspects: " + ", ".join(f"{ba.BLOCK_NAME.get(b, b)} ×{c}" for b, c in
                                                        sorted(cnt.items(), key=lambda kv: -kv[1])[:4]) if cnt else ""))

    def _open_report(self):
        if self.last_report:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self.last_report))

    def _open_hist_report(self):
        r = self.hist.currentRow()
        if r < 0:
            return
        hdr = [self.hist.horizontalHeaderItem(c).text() for c in range(self.hist.columnCount())]
        if "Report" in hdr:
            it = self.hist.item(r, hdr.index("Report"))
            if it and it.text():
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(os.path.join(REPORT_DIR, it.text())))

    def shutdown(self):
        if self.running:
            self.abort_flag = True
            self.running = False
        self._collect()


class _Skip(Exception):
    pass


class _TestError(Exception):
    pass
