#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fsw_vsa_monitor.py
==================
Real-time GUI for an R&S FSW (e.g. FSW50) running the VSA application (FSW-K70),
connected over Ethernet (LAN).

Features
--------
* Connect via HiSLIP, VXI-11 or raw socket (port 5025) - or a built-in DEMO mode.
* Read / write signal & analyzer settings:
    centre frequency, reference level, attenuation (auto/manual), preamp,
    modulation, symbol rate, capture oversampling (-> sample rate),
    capture length (auto/manual), result length, Tx filter (type, alpha/BT),
    measurement filter auto, equalizer, burst search, pattern search,
    trigger source, statistic count, instrument display update.
* Live constellation (measured symbols) with persistence.
* Live results: MER RMS, Power, Carrier Frequency Error
  (+ EVM RMS, Symbol Rate Error, I/Q Offset), with mean/min/max and a trend plot.
* Optional CSV logging of every sweep.
* Spectrum tab: live spectrum from an FSW Spectrum channel (created on first use,
  INST:CRE SANALYZER) with center/span, ref level/range, attenuation, RBW/VBW,
  sweep time, sweep points, detector, trace mode and count; peak marker and
  channel power. Switching tabs switches the FSW channel (only the selected one
  measures).

* Tabs: Main (dashboard.py: device status, test sequences driving HMP / SMW / CAN /
  FSW / scan, constellation + MER/Power/CFE, voltages, telemetry watch list),
  Spectrum Analyzer (VSA + Spectrum), SMW200A (smw_generator.py), Bench, CAN, Telemetry.
* Bench tab (bench_monitor.py, keep it in the same folder): 176-channel voltage
  scanner (Arduino + CD74HC4067 multiplexers + Agilent 34405A) with plot, table and
  daily CSV log, plus HMP4040 control (USB / LAN) with timed power cycling.

Install
-------
    pip install numpy pyqtgraph PyQt5 pyvisa pyvisa-py pyserial

    (pyvisa-py is a pure-Python VISA backend. If NI-VISA or R&S VISA is installed,
     it is used automatically and pyvisa-py is just the fallback.)

Run
---
    python fsw_vsa_monitor.py                      # GUI, enter IP and press Connect
    python fsw_vsa_monitor.py --ip 192.168.1.50 --connect
    python fsw_vsa_monitor.py --demo               # no instrument needed

SCPI notes (FSW-K70)
--------------------
* The script selects (or creates) a VSA channel (type DDEM) and applies the
  standard 4-window layout with  [SENS:]DDEM:PRES:CALC :
      window 1 = magnitude (capture buffer), window 2 = spectrum,
      window 3 = Result Summary,             window 4 = Constellation I/Q.
  The constellation trace is read from window 4, results from window 3.
* Results:  CALC3:MARK:FUNC:DDEM:STAT:<SNR|MPOW|CFER|EVM|SRER|OOFF>?
  (SNR is the K70 name for MER; no suffix = value of the current sweep.)
* All SCPI headers and enumerated parameters were checked against R&S's official
  FSW Python driver (RsFsw). Modulation: DDEM:FORM only knows the groups
  APSK|ASK|FSK|MSK|PSK|QAM|QPSK|UQAM; OQPSK = DDEM:FORM QPSK + DDEM:QPSK:FORM OFFS.
* Burst search (DDEM:SEAR:BURS:STAT) only exists for Signal type = Bursted
  (DDEM:SIGN:VAL BURS) and pattern search (DDEM:SEAR:SYNC:STAT) only when the
  signal description contains a pattern (DDEM:SIGN:PATT ON); otherwise the FSW
  answers -200 "Function not available". Apply only sends changed settings.

Live update speed
-----------------
* Two run modes (toolbar):
    Continuous (default) - INIT:CONT ON, the FSW sweeps back-to-back by itself and
        the script reads the latest trace/results every poll interval. A trace that
        is identical to the previous one means "no new sweep yet" and is skipped.
    Single sweeps (synced) - the script triggers every sweep ('INIT:IMM;*OPC'),
        waits for it (*ESR? bit 0) and reads it: trace and numbers always come from
        the same complete sweep. Slower; best for CSV logging / statistics.
* Settings jobs (Apply, Read, Auto level) briefly pause the continuous sweep,
  because *OPC? is not reliable while the FSW sweeps continuously.
* The status bar shows where the time goes (FSW sweep / trace / results / draw).
* Biggest FSW-side factors: Statistic count (set 1 for live view), capture length
  much longer than result length, and FSW screen update during remote.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import math
import os
import queue
import re
import sys
import threading
import time
from collections import deque

import numpy as np

try:
    from PyQt5 import QtCore, QtGui, QtWidgets
    import pyqtgraph as pg
except ImportError as _e:  # pragma: no cover
    sys.exit(f"Missing GUI dependency ({_e}). Run:  pip install PyQt5 pyqtgraph numpy")

try:
    import pyvisa
except ImportError:  # pragma: no cover - demo mode still works
    pyvisa = None

try:  # Bench tab (176-channel voltage monitor + HMP4040), file next to this script
    import bench_monitor
    _BENCH_ERR = None
except Exception as _e:  # pragma: no cover
    bench_monitor = None
    _BENCH_ERR = f"{type(_e).__name__}: {_e}"

try:  # SMW200A generator tab
    import smw_generator
except Exception:  # pragma: no cover
    smw_generator = None

try:  # Main (dashboard) tab
    import dashboard
except Exception:  # pragma: no cover
    dashboard = None

try:  # 1-Wire temperature tab (sensors on the bench Arduino, pin A3)
    import onewire_temp
except Exception:  # pragma: no cover
    onewire_temp = None

try:  # switch outputs on the slave Arduino + BOS watchdog (panel on the Main tab)
    import slave_switch
except Exception:  # pragma: no cover
    slave_switch = None

try:  # Logs tab (all CSV logs on one plot)
    import log_viewer
except Exception:  # pragma: no cover
    log_viewer = None

try:  # Telemetry tab (decodes unit arrays received on the CAN tab)
    import telemetry
except Exception:  # pragma: no cover
    telemetry = None

try:  # CAN tab (CANable sniffer), canable_analyzer.py + can_commands.py next to this script
    import canable_analyzer
    _CAN_ERR = None
except Exception as _e:  # pragma: no cover
    canable_analyzer = None
    _CAN_ERR = f"{type(_e).__name__}: {_e}"


# --------------------------------------------------------------------------- #
#  Constants                                                                   #
# --------------------------------------------------------------------------- #
SUMMARY_WIN = 3          # Result Summary window after DDEM:PRES:CALC
CONST_WIN = 4            # Constellation I/Q window after DDEM:PRES:CALC
SPEC_WIN = 1             # spectrum trace window of the Spectrum channel
MAX_PLOT_POINTS = 30000  # decimate constellation above this (keeps the GUI responsive)
INVALID = 9.0e37         # FSW returns 9.91E37 for "no result"
_NUM_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")

# key, SCPI statistic suffix, label, unit
RESULTS = [
    ("mer",   "SNR",  "MER RMS",            "dB"),
    ("power", "MPOW", "Power",              "dBm"),
    ("cfe",   "CFER", "Carrier Freq Error", "Hz"),
    ("evm",   "EVM",  "EVM RMS",            "%"),
    ("srerr", "SRER", "Symbol Rate Error",  "ppm"),
    ("iqoff", "OOFF", "I/Q Offset",         "dB"),
]
RESULT_LABEL = {k: (lbl, unit) for k, _, lbl, unit in RESULTS}
PRIMARY = ("mer", "power", "cfe")

# label -> (DDEM:FORM group, sub-format command or None, NSTate or None).
# Values checked against the enums of R&S's official FSW driver (RsFsw):
#   DDEM:FORM      APSK|ASK|FSK|MSK|PSK|QAM|QPSK|UQAM   (no separate OQPSK / BPSK group)
#   DDEM:QPSK:FORM NORMal|DIFFerential|NPI4|DPI4|OFFSet|SOFFset|N3Pi4
#   DDEM:PSK:NST   2|8,   DDEM:APSK:NST 16|32,   DDEM:QAM:FORM NORMal|...
MOD_TABLE = {
    "BPSK":        ("PSK",  "DDEM:PSK:FORM NORM",   2),
    "QPSK":        ("QPSK", "DDEM:QPSK:FORM NORM",  None),
    "OQPSK":       ("QPSK", "DDEM:QPSK:FORM OFFS",  None),
    "SOQPSK":      ("QPSK", "DDEM:QPSK:FORM SOFF",  None),
    "DQPSK":       ("QPSK", "DDEM:QPSK:FORM DIFF",  None),
    "pi/4-QPSK":   ("QPSK", "DDEM:QPSK:FORM NPI4",  None),
    "pi/4-DQPSK":  ("QPSK", "DDEM:QPSK:FORM DPI4",  None),
    "3pi/4-QPSK":  ("QPSK", "DDEM:QPSK:FORM N3PI4", None),
    "8PSK":        ("PSK",  "DDEM:PSK:FORM NORM",   8),
    **{f"{n}QAM":  ("QAM",  "DDEM:QAM:FORM NORM",   n) for n in (16, 32, 64, 128, 256, 512, 1024)},
    "16APSK":      ("APSK", None,                   16),
    "32APSK":      ("APSK", None,                   32),
    "MSK":         ("MSK",  None,                   None),
}
MODULATIONS = list(MOD_TABLE)
TX_FILTERS = ["RRC", "RC", "GAUSS"]          # combo is editable: type any K70 filter name
OVERSAMPLING = ["2", "4", "8", "16", "32", "64", "128"]
TRIGGERS = ["IMM", "EXT", "EXT2", "EXT3", "IFP", "RFP"]
SIGNAL_TYPES = [("Continuous", "CONT"), ("Bursted", "BURS")]
DETECTORS = [("Auto", "AUTO"), ("Positive peak", "POS"), ("RMS", "RMS"), ("Average", "AVER"),
             ("Negative peak", "NEG"), ("Sample", "SAMP"), ("Auto peak", "APE")]
TRACE_MODES = [("Clear/Write", "WRIT"), ("Max hold", "MAXH"), ("Min hold", "MINH"),
               ("Average", "AVER"), ("View", "VIEW")]
PROTOCOLS = ["HiSLIP (hislip0)", "VXI-11 (INSTR)", "Raw socket (5025)", "Demo (no instrument)"]


# --------------------------------------------------------------------------- #
#  Helpers                                                                     #
# --------------------------------------------------------------------------- #
_SI_PREFIX = {"T": 1e12, "G": 1e9, "M": 1e6, "k": 1e3, "K": 1e3,
              "m": 1e-3, "u": 1e-6, "µ": 1e-6, "n": 1e-9}
_UNITS = ("sym/s", "sps", "baud", "hz", "dbm", "db", "sym", "s", "%")


def parse_si(text: str) -> float:
    """'1.5 GHz' -> 1.5e9, '10M' -> 1e7, '2e6' -> 2e6.  (M = mega, m = milli)"""
    t = text.strip().replace(" ", "").replace(",", ".")
    low = t.lower()
    for u in _UNITS:
        if low.endswith(u) and len(t) > len(u):
            t = t[: -len(u)]
            break
    if t and t[-1] in _SI_PREFIX:
        return float(t[:-1]) * _SI_PREFIX[t[-1]]
    return float(t)


def fmt_si(v, unit: str = "", digits: int = 9) -> str:
    if v is None or not math.isfinite(v):
        return "—"
    if v == 0:
        return f"0 {unit}".strip()
    exp = int(math.floor(math.log10(abs(v)) / 3) * 3)
    exp = max(-9, min(12, exp))
    prefix = {12: "T", 9: "G", 6: "M", 3: "k", 0: "", -3: "m", -6: "u", -9: "n"}[exp]
    return f"{v / 10 ** exp:.{digits}g} {prefix}{unit}".strip()


def fmt_result(key: str, v) -> str:
    if v is None or not math.isfinite(v):
        return "—"
    unit = RESULT_LABEL[key][1]
    if key == "cfe":
        return f"{v:+.2f} Hz" if abs(v) < 1e3 else ("+" if v > 0 else "") + fmt_si(v, "Hz", 6)
    if key == "evm":
        return f"{v:.3f} %"
    if key == "srerr":
        return f"{v:+.3f} ppm"
    return f"{v:.2f} {unit}"


def mod_commands(name: str) -> list[str]:
    """SCPI commands that select a modulation in K70."""
    entry = MOD_TABLE.get(name.strip())
    if entry is None:
        return []  # unknown label read back from the instrument -> leave as is
    group, sub, nst = entry
    cmds = [f"DDEM:FORM {group}"]
    if nst is not None:
        cmds.append(f"DDEM:{group}:NST {nst}")
    if sub:
        cmds.append(sub)
    return cmds


def ideal_points(mod: str) -> np.ndarray:
    """Ideal constellation (unit average power) - used by the demo simulator."""
    if "pi/4" in mod:
        p = np.exp(1j * np.pi / 4 * np.arange(8))
    elif "QPSK" in mod or mod == "MSK":
        p = np.exp(1j * (np.pi / 4 + np.pi / 2 * np.arange(4)))
    elif mod == "BPSK":
        p = np.array([1, -1], dtype=complex)
    elif mod.endswith("APSK"):
        rings = {"16": [(4, 1.0), (12, 2.6)], "32": [(4, 1.0), (12, 2.6), (16, 4.2)]}
        p = np.concatenate([r * np.exp(1j * (2 * np.pi * np.arange(k) / k + np.pi / k))
                            for k, r in rings.get(mod[:-4], rings["16"])])
    elif mod.endswith("PSK"):
        m = int(mod[:-3] or 8)
        p = np.exp(1j * 2 * np.pi * np.arange(m) / m)
    elif mod.endswith("QAM"):
        m = int(mod[:-3])
        side = int(math.ceil(math.sqrt(m)))
        ax = np.arange(side) - (side - 1) / 2
        grid = (ax[None, :] + 1j * ax[:, None]).ravel()
        p = grid[np.argsort(np.abs(grid), kind="stable")][:m]
    else:
        p = np.exp(1j * (np.pi / 4 + np.pi / 2 * np.arange(4)))
    return p / np.sqrt(np.mean(np.abs(p) ** 2))


class _CmdDiff:
    """Builds the SCPI commands for the settings that differ from the last read."""

    def __init__(self, s: dict, prev: dict):
        self.s, self.prev, self.cmds, self.notes = s, prev or {}, [], []

    def changed(self, key):
        v, p = self.s.get(key), self.prev.get(key)
        if v is None:
            return False
        if p is None:
            return True
        if isinstance(v, float) or isinstance(p, float):
            return not math.isclose(float(v), float(p), rel_tol=1e-9, abs_tol=1e-9)
        return v != p

    def eff(self, key):          # value the instrument will have after this apply
        return self.s[key] if self.s.get(key) is not None else self.prev.get(key)

    def num(self, key, cmd):
        if self.changed(key):
            self.cmds.append(f"{cmd} {self.s[key]:.10g}")

    def onoff(self, key, cmd):
        if self.changed(key):
            self.cmds.append(f"{cmd} {'ON' if self.s[key] else 'OFF'}")

    def word(self, key, cmd):
        if self.changed(key) and self.s[key]:
            self.cmds.append(f"{cmd} {self.s[key]}")


# --------------------------------------------------------------------------- #
#  Instrument driver (FSW-K70 over VISA)                                       #
# --------------------------------------------------------------------------- #
class FswVsa:
    """Thin SCPI wrapper. Used only from the worker thread."""

    def __init__(self, resource: str, timeout_ms: int = 30000):
        if pyvisa is None:
            raise RuntimeError("pyvisa is not installed:  pip install pyvisa pyvisa-py")
        try:
            self.rm = pyvisa.ResourceManager()
        except Exception:
            self.rm = pyvisa.ResourceManager("@py")
        self.inst = self.rm.open_resource(resource)
        self.inst.timeout = timeout_ms
        self.timeout_ms = timeout_ms
        self._unsupported: set[str] = set()   # queries the firmware does not know (-113)
        self._batch_results = True            # fetch all results in one round trip
        self.continuous = False               # FSW sweeping in INIT:CONT ON mode
        self.app = "vsa"                      # active FSW channel: "vsa" (K70) or "spec"
        self.channels: dict[str, str] = {}    # app -> FSW channel name
        self._last_raw = None                 # last trace, to detect "no new sweep yet"
        if resource.upper().endswith("::SOCKET"):
            self.inst.read_termination = "\n"
            self.inst.write_termination = "\n"
        else:
            try:
                self.inst.clear()
            except Exception:
                pass
        self.idn = self.query("*IDN?").strip()
        self.channel = self._setup()

    # -- low level ---------------------------------------------------------
    def write(self, cmd: str):
        self.inst.write(cmd)

    def query(self, cmd: str) -> str:
        return self.inst.query(cmd).strip()

    def errors(self, maxn: int = 30) -> list[str]:
        out = []
        for _ in range(maxn):
            e = self.query("SYST:ERR?")
            if e.startswith("0") or e.startswith("+0"):
                break
            out.append(e)
        return out

    def _clear_io(self):
        """Drop any stale response (e.g. a late answer to a timed-out query) so that
        the next query does not read an old, unrelated value."""
        try:
            self.inst.clear()          # VISA device clear (HiSLIP / VXI-11)
        except Exception:
            pass
        try:
            self.write("*CLS")
        except Exception:
            pass

    def _probe(self, cmd: str):
        """Query a setting that may be unknown or not available in the current state.

        '<cmd>;*STB?' always produces an answer: '<value>;<stb>' if the query worked
        and just '<stb>' if it failed. So an unavailable setting costs one round trip
        instead of a full VISA timeout (the original script waited 5 s for each).
        *STB? (unlike *OPC?) answers at once, even while a sweep is running."""
        if cmd in self._unsupported:
            return None
        try:
            resp = self.query(f"{cmd};*STB?")
        except Exception:
            self._unsupported.add(cmd)
            self._clear_io()
            return None
        head, sep, tail = resp.rpartition(";")
        if sep and tail.strip().lstrip("+").isdigit():
            return head.strip()
        try:   # remember only "undefined header", not "not available right now"
            if any(e.lstrip("+").startswith("-113") for e in self.errors()):
                self._unsupported.add(cmd)
        except Exception:
            pass
        return None

    @staticmethod
    def _num(text):
        m = _NUM_RE.search(text or "")
        if not m:
            return None
        v = float(m.group(0))
        return None if abs(v) >= INVALID else v

    def _qf(self, cmd):
        return self._num(self._probe(cmd))

    def _qi(self, cmd):
        v = self._qf(cmd)
        return None if v is None else int(round(v))

    def _qb(self, cmd):
        v = self._probe(cmd)
        if v is None:
            return None
        v = v.upper()
        if v in ("ON", "OFF"):
            return v == "ON"
        try:
            return float(v) != 0
        except ValueError:
            return None

    def _qs(self, cmd):
        v = self._probe(cmd)
        return None if v is None else v.strip("'\"")

    # -- setup -------------------------------------------------------------
    def _find_channel(self, types: tuple[str, ...]):
        try:
            items = [x.strip().strip("'\"") for x in self.query("INST:LIST?").split(",")]
        except Exception:
            return None
        for typ, nm in zip(items[0::2], items[1::2]):
            if typ.upper() in types:
                return nm
        return None

    def _open_channel(self, types, create_type, default_name) -> str:
        """Select an existing channel of that type or create one."""
        name = self._find_channel(types)
        if name:
            self.write(f"INST:SEL '{name}'")
        else:
            name = default_name
            self.write(f"INST:CRE {create_type}, '{name}'")
        self.query("*OPC?")
        self.write("INIT:CONT OFF")                # we control the sweep (Run / Single)
        self.write("FORM REAL,32")                 # binary trace transfer
        self.write("FORM:BORD SWAP")               # little endian
        return name

    def _setup(self) -> str:
        self.write("*CLS")
        name = self._open_channel(("DDEM",), "DDEM", "VSA")
        self.channels["vsa"] = name
        self.app = "vsa"
        self.preset_layout()
        self.errors()
        return name

    def select_app(self, app: str):
        """Switch the FSW to the K70 (vsa) or the Spectrum (spec) channel. Only the
        selected channel measures, so the GUI tab decides what the FSW does."""
        if app == self.app:
            return
        if self.continuous:
            self.stop_continuous()
        if app in self.channels:
            self.write(f"INST:SEL '{self.channels[app]}'")
            self.query("*OPC?")
            self.write("INIT:CONT OFF")
        elif app == "spec":
            self.channels["spec"] = self._open_channel(("SANALYZER", "SAN"), "SANALYZER", "Spectrum")
        self.app = app
        self._last_raw = None
        self.errors()

    def preset_layout(self):
        self.select_app("vsa")
        self.write("DDEM:PRES:CALC")               # W1 mag, W2 spectrum, W3 summary, W4 constellation
        self.query("*OPC?")

    def set_display_update(self, on: bool):
        self.write(f"SYST:DISP:UPD {'ON' if on else 'OFF'}")

    def auto_level(self, app: str = "vsa"):
        self.select_app(app)
        self.write("ADJ:LEV")
        self.query("*OPC?")

    # -- settings ----------------------------------------------------------
    def _read_modulation(self):
        form = self._qs("DDEM:FORM?")
        if not form:
            return None
        form = form.upper()
        if form.startswith("QPSK"):
            sub = (self._qs("DDEM:QPSK:FORM?") or "").upper()     # e.g. NORM, OFFS, DPI4
            for label, (g, cmd, _) in MOD_TABLE.items():
                if g == "QPSK" and sub and cmd.split()[-1].upper().startswith(sub[:4]):
                    return label
            return f"QPSK ({sub})"
        if form in ("PSK", "QAM", "APSK", "ASK"):
            n = self._qi(f"DDEM:{form}:NST?")
            if n is None:
                return None
            if form == "PSK" and n == 2:
                return "BPSK"
            return f"{n}{form}"
        return form

    def _read_capture_len(self, symbol_rate):
        """Capture length in symbols. DDEM:RLEN? may answer in symbols or in seconds
        (depends on firmware / last unit used), so decide from the answer itself."""
        raw = self._probe("DDEM:RLEN?")
        v = self._num(raw)
        if v is None:
            return None
        unit = raw.upper()
        if "SYM" in unit:
            return int(round(v))
        in_symbols = v >= 10 and abs(v - round(v)) < 1e-6
        if in_symbols or not symbol_rate:
            return int(round(v)) if in_symbols else None
        return int(round(v * symbol_rate))

    def read_settings(self) -> dict:
        self.select_app("vsa")
        old = self.inst.timeout
        self.inst.timeout = min(old, 3000)
        try:
            s = {
                "center":      self._qf("FREQ:CENT?"),
                "ref_level":   self._qf("DISP:WIND:TRAC:Y:SCAL:RLEV?"),
                "att_auto":    self._qb("INP:ATT:AUTO?"),
                "att":         self._qf("INP:ATT?"),
                "preamp":      self._qb("INP:GAIN:STAT?"),
                "modulation":  self._read_modulation(),
                "symbol_rate": self._qf("DDEM:SRAT?"),
                "prat":        self._qi("DDEM:PRAT?"),
                "tfil_state":  self._qb("DDEM:TFIL:STAT?"),
                "tfil_name":   self._qs("DDEM:TFIL:NAME?"),
                "tfil_alpha":  self._qf("DDEM:TFIL:ALPH?"),
                "mfil_auto":   self._qb("DDEM:MFIL:AUTO?"),
                "rlen_auto":   self._qb("DDEM:RLEN:AUTO?"),
                "result_len":  self._qi("DDEM:TIME?"),
                "sweep_count": self._qi("SWE:COUN?"),
                "equalizer":   self._qb("DDEM:EQU:STAT?"),
                "signal_type": None,
                "pattern_sig": self._qb("DDEM:SIGN:PATT?"),
                "trig_source": self._qs("TRIG:SOUR?"),
            }
            st = self._qs("DDEM:SIGN:VAL?")
            if st:
                s["signal_type"] = "BURS" if st.upper().startswith("BURS") else "CONT"
            s["capture_len"] = self._read_capture_len(s["symbol_rate"])
            # Burst search only exists for a bursted signal, pattern search only when
            # the signal description contains a pattern. Otherwise the FSW answers
            # -200 "Function not available" -> report them as simply "off".
            s["burst"] = (self._qb("DDEM:SEAR:BURS:STAT?")
                          if s["signal_type"] != "CONT" else False)
            s["pattern"] = (self._qb("DDEM:SEAR:SYNC:STAT?")
                            if s["pattern_sig"] is not False else False)
            return s
        finally:
            self.inst.timeout = old
            try:
                self.errors()
            except Exception:
                pass

    def apply_settings(self, s: dict, prev: dict | None = None):
        """Send only the settings that differ from `prev` (the last values read from
        the FSW). Every K70 setting change makes the instrument re-evaluate, so
        re-sending 20 unchanged settings is slow and can provoke needless errors."""
        self.select_app("vsa")
        d = _CmdDiff(s, prev)
        if d.changed("modulation"):
            d.cmds += mod_commands(s["modulation"])
        d.num("center", "FREQ:CENT")
        d.num("ref_level", "DISP:WIND:TRAC:Y:SCAL:RLEV")
        d.onoff("att_auto", "INP:ATT:AUTO")
        if not d.eff("att_auto"):
            d.num("att", "INP:ATT")
        d.onoff("preamp", "INP:GAIN:STAT")
        d.num("symbol_rate", "DDEM:SRAT")
        if d.changed("prat"):
            d.cmds.append(f"DDEM:PRAT {int(s['prat'])}")
        d.onoff("tfil_state", "DDEM:TFIL:STAT")
        if d.eff("tfil_state"):
            if d.changed("tfil_name") and s["tfil_name"]:
                d.cmds.append(f"DDEM:TFIL:NAME '{s['tfil_name']}'")
            d.num("tfil_alpha", "DDEM:TFIL:ALPH")
        d.onoff("mfil_auto", "DDEM:MFIL:AUTO")
        d.onoff("rlen_auto", "DDEM:RLEN:AUTO")
        if not d.eff("rlen_auto") and s.get("capture_len") is not None and (
                d.changed("capture_len") or d.changed("rlen_auto")):
            d.cmds.append(f"DDEM:RLEN {int(s['capture_len'])} SYM")
        if d.changed("result_len"):
            d.cmds.append(f"DDEM:TIME {int(s['result_len'])}")
        if d.changed("sweep_count"):
            d.cmds.append(f"SWE:COUN {int(s['sweep_count'])}")
        d.onoff("equalizer", "DDEM:EQU:STAT")
        d.word("signal_type", "DDEM:SIGN:VAL")
        d.onoff("pattern_sig", "DDEM:SIGN:PATT")
        if d.changed("burst"):
            if d.eff("signal_type") == "CONT":
                d.notes.append("Burst search skipped: only available for Signal type = Bursted")
            else:
                d.onoff("burst", "DDEM:SEAR:BURS:STAT")
        if d.changed("pattern"):
            if d.eff("pattern_sig") is False:
                d.notes.append("Pattern search skipped: signal description has no pattern")
            else:
                d.onoff("pattern", "DDEM:SEAR:SYNC:STAT")
        d.word("trig_source", "TRIG:SOUR")
        return self._send(d)

    def _send(self, d: _CmdDiff):
        self.write("*CLS")
        errors = []
        for c in d.cmds:
            self.write(c)
            errors += [f"{c}  →  {e}" for e in self.errors()]
        self.query("*OPC?")
        return d.cmds, errors, d.notes

    # -- spectrum channel ----------------------------------------------------
    def read_spec_settings(self) -> dict:
        self.select_app("spec")
        old = self.inst.timeout
        self.inst.timeout = min(old, 3000)
        try:
            s = {
                "center":     self._qf("FREQ:CENT?"),
                "span":       self._qf("FREQ:SPAN?"),
                "ref_level":  self._qf("DISP:WIND:TRAC:Y:SCAL:RLEV?"),
                "range":      self._qf("DISP:WIND:TRAC:Y:SCAL?"),
                "att_auto":   self._qb("INP:ATT:AUTO?"),
                "att":        self._qf("INP:ATT?"),
                "preamp":     self._qb("INP:GAIN:STAT?"),
                "rbw_auto":   self._qb("BWID:RES:AUTO?"),
                "rbw":        self._qf("BWID:RES?"),
                "vbw_auto":   self._qb("BWID:VID:AUTO?"),
                "vbw":        self._qf("BWID:VID?"),
                "swt_auto":   self._qb("SWE:TIME:AUTO?"),
                "swt":        self._qf("SWE:TIME?"),
                "points":     self._qi("SWE:POIN?"),
                "detector":   None,
                "trace_mode": None,
                "count":      self._qi("SWE:COUN?"),
            }
            det_auto = self._qb("DET:FUNC:AUTO?")
            det = self._qs("DET:FUNC?")
            if det_auto:
                s["detector"] = "AUTO"
            elif det:
                code = det.upper()
                s["detector"] = next((c for _, c in DETECTORS if c[:3] == code[:3]), code)
            mode = self._qs("DISP:WIND:TRAC:MODE?")
            s["trace_mode"] = mode.upper()[:4] if mode else None
            return s
        finally:
            self.inst.timeout = old
            try:
                self.errors()
            except Exception:
                pass

    def apply_spec_settings(self, s: dict, prev: dict | None = None):
        self.select_app("spec")
        d = _CmdDiff(s, prev)
        d.num("center", "FREQ:CENT")
        d.num("span", "FREQ:SPAN")
        d.num("ref_level", "DISP:WIND:TRAC:Y:SCAL:RLEV")
        d.num("range", "DISP:WIND:TRAC:Y:SCAL")
        d.onoff("att_auto", "INP:ATT:AUTO")
        if not d.eff("att_auto"):
            d.num("att", "INP:ATT")
        d.onoff("preamp", "INP:GAIN:STAT")
        for key, cmd in (("rbw", "BWID:RES"), ("vbw", "BWID:VID"), ("swt", "SWE:TIME")):
            d.onoff(f"{key}_auto", f"{cmd}:AUTO")
            if not d.eff(f"{key}_auto") and (d.changed(key) or d.changed(f"{key}_auto")):
                d.cmds.append(f"{cmd} {s[key]:.10g}")
        if d.changed("points"):
            d.cmds.append(f"SWE:POIN {int(s['points'])}")
        if d.changed("detector"):
            d.cmds.append("DET:FUNC:AUTO ON" if s["detector"] == "AUTO" else f"DET:FUNC {s['detector']}")
        d.word("trace_mode", "DISP:WIND:TRAC:MODE")
        if d.changed("count"):
            d.cmds.append(f"SWE:COUN {int(s['count'])}")
        return self._send(d)

    # -- measurement -------------------------------------------------------
    def abort(self):
        try:
            self.write("ABOR")
            self.query("*OPC?")
        except Exception:
            self._clear_io()

    def _wait_sweep(self, should_abort) -> bool:
        """Wait for the sweep started with '...;*OPC' by polling the ESR.

        Unlike a blocking 'INIT;*OPC?' (which locked the I/O for the whole sweep and
        left a late '1' in the output buffer after a timeout), this can be stopped at
        any moment (Stop / Apply / Disconnect) and never desynchronises the I/O."""
        t0 = time.perf_counter()
        deadline = t0 + self.timeout_ms / 1000
        delay = 0.002
        while True:
            if int(float(self.query("*ESR?"))) & 1:      # bit 0 = operation complete
                return True
            if should_abort():
                self.abort()
                return False
            if time.perf_counter() > deadline:
                self.abort()
                raise TimeoutError(
                    f"sweep did not finish within {self.timeout_ms / 1000:.0f} s - check "
                    f"trigger source, burst/pattern search and statistic count")
            time.sleep(delay)
            delay = min(delay * 1.5, 0.03)

    def _read_results(self) -> dict:
        qs = [f"CALC{SUMMARY_WIN}:MARK:FUNC:DDEM:STAT:{scpi}?" for _, scpi, _, _ in RESULTS]
        if self._batch_results:
            old = self.inst.timeout
            self.inst.timeout = min(old, 5000)
            try:
                parts = self.query(";:".join(qs)).split(";")
                if len(parts) == len(RESULTS):
                    return {k: self._num(p) for (k, *_), p in zip(RESULTS, parts)}
            except Exception:
                self._clear_io()
            finally:
                self.inst.timeout = old
            self._batch_results = False                   # fall back to one query each
        return {k: self._qf(q) for (k, *_), q in zip(RESULTS, qs)}

    def _read_trace(self):
        win = CONST_WIN if self.app == "vsa" else SPEC_WIN
        raw = self.inst.query_binary_values(f"TRAC{win}:DATA? TRACE1", datatype="f",
                                            is_big_endian=False, container=np.array)
        return np.asarray(raw)

    def _result(self, raw, t0, t1, t2, mode) -> dict:
        if self.app == "spec":
            return self._spec_result(raw, t0, t1, t2, mode)
        raw = raw.astype(np.float64)
        n = raw.size // 2 * 2
        i, q = raw[0:n:2], raw[1:n:2]
        ok = np.isfinite(i) & np.isfinite(q) & (np.abs(i) < 1e30) & (np.abs(q) < 1e30)
        res = {"i": i[ok], "q": q[ok], "t": time.time(), "mode": mode, "app": "vsa"}
        res.update({k: (float("nan") if v is None else v) for k, v in self._read_results().items()})
        t3 = time.perf_counter()
        res.update(sweep_time=t3 - t0, t_acq=t1 - t0, t_trace=t2 - t1, t_res=t3 - t2)
        return res

    def _spec_result(self, raw, t0, t1, t2, mode) -> dict:
        # x axis and RBW in one round trip (they may have been changed on the FSW itself)
        parts = [self._num(x) for x in self.query("FREQ:STAR?;:FREQ:STOP?;:BWID:RES?").split(";")]
        start, stop, rbw = (parts + [None, None, None])[:3]
        lvl = raw.astype(np.float64)
        lvl[np.abs(lvl) >= 1e30] = np.nan
        f = np.linspace(start or 0.0, stop or float(lvl.size), lvl.size)
        t3 = time.perf_counter()
        return {"app": "spec", "f": f, "p": lvl, "rbw": rbw, "t": time.time(), "mode": mode,
                "sweep_time": t3 - t0, "t_acq": t1 - t0, "t_trace": t2 - t1, "t_res": t3 - t2}

    # Continuous mode: the FSW sweeps back-to-back on its own (INIT:CONT ON) and we
    # just pick up the latest completed result. No per-sweep handshake.
    def start_continuous(self):
        self.write("INIT:CONT ON")
        self.write("INIT:IMM")                 # (re)start the continuous sweep
        self.continuous = True
        self._last_raw = None

    def stop_continuous(self):
        self.continuous = False
        try:
            self.write("INIT:CONT OFF")
        finally:
            self.abort()

    def fetch(self):
        """Latest result of the continuous sweep, or None if no new sweep finished
        since the last call (identical trace data)."""
        t0 = time.perf_counter()
        raw = self._read_trace()
        key = raw.tobytes()
        if key == self._last_raw:
            return None
        self._last_raw = key
        t2 = time.perf_counter()
        return self._result(raw, t0, t0, t2, "cont")

    # Synchronized single sweep: INIT:IMM, wait for completion, read. Every result
    # is guaranteed to come from one fresh sweep (trace and numbers consistent).
    def measure(self, should_abort=lambda: False):
        if self.continuous:
            self.stop_continuous()
        t0 = time.perf_counter()
        self.write("*CLS;:INIT:IMM;*OPC")                  # one single sweep, non-blocking
        if not self._wait_sweep(should_abort):
            return None                                    # aborted by the GUI
        t1 = time.perf_counter()
        raw = self._read_trace()
        t2 = time.perf_counter()
        return self._result(raw, t0, t1, t2, "sync")

    def close(self):
        try:
            self.abort()
            self.write("INIT:CONT ON")
            self.write("SYST:DISP:UPD ON")
        except Exception:
            pass
        try:
            self.inst.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  Simulated instrument (DEMO mode)                                            #
# --------------------------------------------------------------------------- #
class DemoFsw:
    def __init__(self):
        self.idn = "Rohde&Schwarz,FSW-50,DEMO,simulated"
        self.channel = "VSA (demo)"
        self.rng = np.random.default_rng()
        self.snr = 30.0
        self.cfe = 150.0
        self.s = {"center": 2.4e9, "ref_level": -10.0, "att_auto": True, "att": 10.0,
                  "preamp": False, "modulation": "16QAM", "symbol_rate": 10e6, "prat": 4,
                  "tfil_state": True, "tfil_name": "RRC", "tfil_alpha": 0.35,
                  "mfil_auto": True, "rlen_auto": False, "capture_len": 4000,
                  "result_len": 1000, "sweep_count": 1, "equalizer": False,
                  "signal_type": "CONT", "pattern_sig": False,
                  "burst": False, "pattern": False, "trig_source": "IMM"}
        self.spec = {"center": 2.4e9, "span": 40e6, "ref_level": 0.0, "range": 100.0,
                     "att_auto": True, "att": 10.0, "preamp": False,
                     "rbw_auto": True, "rbw": 100e3, "vbw_auto": True, "vbw": 100e3,
                     "swt_auto": True, "swt": 0.01, "points": 1001, "detector": "AUTO",
                     "trace_mode": "WRIT", "count": 0}
        self.app = "vsa"

    def select_app(self, app):
        self.app = app

    @staticmethod
    def _apply(target, s):
        cmds = []
        for k, v in s.items():
            if v is not None and v != target.get(k):
                target[k] = v
                cmds.append(f"{k} = {v}")
        return cmds, [], []

    def read_settings(self):
        self.app = "vsa"
        return dict(self.s)

    def apply_settings(self, s, prev=None):
        self.app = "vsa"
        return self._apply(self.s, s)

    def read_spec_settings(self):
        self.app = "spec"
        if self.spec["rbw_auto"]:
            self.spec["rbw"] = self.spec["span"] / 400
        return dict(self.spec)

    def apply_spec_settings(self, s, prev=None):
        self.app = "spec"
        return self._apply(self.spec, s)

    def preset_layout(self):
        pass

    def set_display_update(self, on):
        pass

    def auto_level(self, app="vsa"):
        time.sleep(0.3)
        (self.s if app == "vsa" else self.spec)["ref_level"] = -12.0

    continuous = False
    _next = 0.0

    def start_continuous(self):
        self.continuous = True
        self._next = time.perf_counter() + self._sweep_s()

    def stop_continuous(self):
        self.continuous = False

    def _sweep_s(self):
        n = int(self.s["result_len"] or 1000)
        return 0.04 + min(0.3, (self.s["capture_len"] or n) / (self.s["symbol_rate"] or 1e6))

    def fetch(self):
        now = time.perf_counter()
        if now < self._next:
            return None
        self._next = now + self._sweep_s()
        r = self._gen()
        r.update(mode="cont", sweep_time=0.0, t_acq=0.0)
        return r

    def measure(self, should_abort=lambda: False):
        self.continuous = False
        t0 = time.perf_counter()
        time.sleep(self._sweep_s())
        if should_abort():
            return None
        r = self._gen()
        r.update(mode="sync", sweep_time=time.perf_counter() - t0, t_acq=time.perf_counter() - t0)
        return r

    def _gen(self):
        if self.app == "spec":
            return self._gen_spec()
        n = int(self.s["result_len"] or 1000)
        pts = ideal_points(self.s["modulation"] or "QPSK")
        sym = pts[self.rng.integers(len(pts), size=n)]
        self.snr = float(np.clip(self.snr + self.rng.normal(0, 0.4), 18, 36))
        self.cfe = float(np.clip(self.cfe + self.rng.normal(0, 8), -800, 800))
        sigma = math.sqrt(10 ** (-self.snr / 10) / 2)
        rx = sym + sigma * (self.rng.normal(size=n) + 1j * self.rng.normal(size=n))
        err = np.mean(np.abs(rx - sym) ** 2)
        sig = np.mean(np.abs(sym) ** 2)
        return {"i": rx.real, "q": rx.imag, "t": time.time(),
                "mer": 10 * math.log10(sig / err), "evm": 100 * math.sqrt(err / sig),
                "power": self.s["ref_level"] - 8 + self.rng.normal(0, 0.03),
                "cfe": self.cfe, "srerr": self.rng.normal(0, 0.5),
                "iqoff": -45 + self.rng.normal(0, 0.5), "t_trace": 0.0, "t_res": 0.0,
                "app": "vsa"}

    def _gen_spec(self):
        sp = self.spec
        n = int(sp["points"] or 1001)
        f = np.linspace(sp["center"] - sp["span"] / 2, sp["center"] + sp["span"] / 2, n)
        sr, a = self.s["symbol_rate"], self.s["tfil_alpha"] or 0.35
        x = np.abs(f - self.s["center"] - self.cfe) / (sr / 2)        # RRC-like signal shape
        shape = np.where(x <= 1 - a, 1.0,
                         np.where(x <= 1 + a, 0.5 * (1 + np.cos(np.pi * (x - 1 + a) / (2 * a))), 0))
        rbw = sp["rbw"] or 100e3
        sig_dbm_hz = self.s["ref_level"] - 8 - 10 * math.log10(sr)
        lin = 10 ** ((sig_dbm_hz + 10 * math.log10(rbw)) / 10) * shape + 10 ** ((-150 + 10 * math.log10(rbw)) / 10)
        p = 10 * np.log10(lin) + self.rng.normal(0, 0.6, n)
        return {"app": "spec", "f": f, "p": p, "rbw": rbw, "t": time.time(),
                "t_trace": 0.0, "t_res": 0.0}

    def close(self):
        pass


# --------------------------------------------------------------------------- #
#  Worker thread: owns the instrument, runs jobs and the sweep loop            #
# --------------------------------------------------------------------------- #
class Worker(QtCore.QObject):
    measured = QtCore.pyqtSignal(object)
    job_done = QtCore.pyqtSignal(str, object)
    failed = QtCore.pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.dev = None
        self.jobs: "queue.Queue[tuple[str, callable]]" = queue.Queue()
        self.running = False
        self.single = False
        self.quit = False
        self.mode = "cont"                # "cont" = FSW runs continuous, "sync" = single sweeps
        self.interval = 0.1               # s between fetches in continuous mode
        self._next_fetch = 0.0
        self._gui_ready = threading.Event()
        self._gui_ready.set()

    def submit(self, tag: str, fn):
        self.jobs.put((tag, fn))

    def ack(self):
        self._gui_ready.set()

    def _pause_continuous(self):
        if self.dev is not None and getattr(self.dev, "continuous", False):
            self.dev.stop_continuous()

    @QtCore.pyqtSlot()
    def loop(self):
        while not self.quit:
            try:
                tag, fn = self.jobs.get_nowait()
            except queue.Empty:
                tag = None
            if tag is not None:
                try:
                    # Settings changes / reads use *OPC?, which is only reliable while the
                    # FSW is not sweeping continuously -> pause; the loop resumes it.
                    self._pause_continuous()
                    self.job_done.emit(tag, fn())
                except Exception as e:
                    self.failed.emit(tag, f"{type(e).__name__}: {e}")
                continue
            dev = self.dev
            if dev is None:
                time.sleep(0.01)
                continue
            try:
                if self.running and self.mode == "cont" and not self.single:
                    self._step_continuous(dev)
                elif self.running or self.single:
                    self._step_single(dev)
                else:
                    self._pause_continuous()      # Stop pressed
                    time.sleep(0.01)
            except Exception as e:
                self.running = self.single = False
                try:
                    dev.continuous = False
                    clear = getattr(dev, "_clear_io", None)
                    if clear:
                        clear()                   # never leave a stale answer in the buffer
                except Exception:
                    pass
                self.failed.emit("measure", f"{type(e).__name__}: {e}")

    def _step_continuous(self, dev):
        now = time.perf_counter()
        if not dev.continuous:
            dev.start_continuous()
            self._next_fetch = now + self.interval
            return
        if not self._gui_ready.is_set() or now < self._next_fetch:
            time.sleep(0.005)
            return
        self._next_fetch = now + self.interval
        r = dev.fetch()
        if r is not None:                         # None = FSW has no new sweep yet
            self._gui_ready.clear()               # back-pressure: wait until GUI drew it
            self.measured.emit(r)

    def _step_single(self, dev):
        if not self._gui_ready.is_set():
            time.sleep(0.005)
            return
        run_mode, was_single = self.running, self.single
        self.single = False

        def should_abort():
            # Stop pressed, mode switched, window closing, or a job (Apply, Read...)
            # waiting: abort the sweep instead of making the user wait for it.
            return (self.quit or not self.jobs.empty()
                    or (run_mode and not was_single and (not self.running or self.mode != "sync")))
        r = dev.measure(should_abort)
        if r is None:                             # aborted
            if was_single and not self.quit:
                self.single = True                # redo the single sweep after the job
            return
        self._gui_ready.clear()
        self.measured.emit(r)


# --------------------------------------------------------------------------- #
#  GUI widgets                                                                 #
# --------------------------------------------------------------------------- #
class SIEdit(QtWidgets.QLineEdit):
    def __init__(self, unit: str, example: str):
        super().__init__()
        self.unit = unit
        self.setPlaceholderText(example)
        self.setToolTip("Accepts SI prefixes: k, M, G (e.g. 2.4G, 10 MHz, 3.84e6)")

    def value(self) -> float:
        return parse_si(self.text())

    def setValue(self, v):
        self.setText(fmt_si(v, self.unit, 10))


def wget(wdg):
    """Value of a settings widget (used by the Spectrum settings)."""
    if isinstance(wdg, SIEdit):
        return wdg.value()
    if isinstance(wdg, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
        return wdg.value()
    if isinstance(wdg, QtWidgets.QCheckBox):
        return wdg.isChecked()
    if isinstance(wdg, QtWidgets.QComboBox):
        d = wdg.currentData()
        return d if d is not None else wdg.currentText().strip()
    raise TypeError(type(wdg))


def wset(wdg, v):
    if isinstance(wdg, SIEdit):
        wdg.setValue(v)
    elif isinstance(wdg, QtWidgets.QSpinBox):
        wdg.setValue(int(v))
    elif isinstance(wdg, QtWidgets.QDoubleSpinBox):
        wdg.setValue(float(v))
    elif isinstance(wdg, QtWidgets.QCheckBox):
        wdg.setChecked(bool(v))
    elif isinstance(wdg, QtWidgets.QComboBox):
        idx = wdg.findData(v)
        if idx < 0:
            idx = wdg.findText(str(v), QtCore.Qt.MatchFixedString)
        if idx < 0:
            wdg.addItem(str(v), v)
            idx = wdg.count() - 1
        wdg.setCurrentIndex(idx)


def combo(items) -> QtWidgets.QComboBox:
    c = QtWidgets.QComboBox()
    for label, data in items:
        c.addItem(label, data)
    return c


def with_auto(wdg, auto: QtWidgets.QCheckBox) -> QtWidgets.QHBoxLayout:
    row = QtWidgets.QHBoxLayout()
    row.addWidget(wdg, 1)
    row.addWidget(auto)
    return row


class Tile(QtWidgets.QFrame):
    def __init__(self, title: str, color: str):
        super().__init__()
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setStyleSheet("Tile{background:#181c22;border:1px solid #2a3038;border-radius:8px;}")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(12, 5, 12, 5)
        lay.setSpacing(2)
        t = QtWidgets.QLabel(title)
        t.setStyleSheet("color:#8a96a3;font-size:12px;font-weight:600;")
        self.value = QtWidgets.QLabel("—")
        self.value.setStyleSheet(f"color:{color};font-size:26px;font-weight:700;")
        self.sub = QtWidgets.QLabel("mean —   min —   max —")
        self.sub.setStyleSheet("color:#7c8793;font-size:11px;")
        for w in (t, self.value, self.sub):
            lay.addWidget(w)


class RunningStat:
    def __init__(self):
        self.reset()

    def reset(self):
        self.n, self.sum, self.min, self.max = 0, 0.0, math.inf, -math.inf

    def add(self, v):
        if v is None or not math.isfinite(v):
            return
        self.n += 1
        self.sum += v
        self.min = min(self.min, v)
        self.max = max(self.max, v)

    @property
    def mean(self):
        return self.sum / self.n if self.n else float("nan")


# --------------------------------------------------------------------------- #
#  Main window                                                                 #
# --------------------------------------------------------------------------- #
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, args):
        super().__init__()
        self.setWindowTitle("FSW VSA Monitor (K70) + Bench")
        self.resize(1560, 940)
        self.connected = False
        self.supported: set[str] = set()
        self.last_read: dict = {}          # last settings read from the FSW (Apply sends the diff)
        self.spec_supported: set[str] = set()
        self.last_read_spec: dict = {}
        self._spec_xrange = None
        self._warned_missing: set[str] = set()
        self.persist: deque = deque(maxlen=1)
        self.trend = {k: deque(maxlen=600) for k, *_ in RESULTS}
        self.stats = {k: RunningStat() for k, *_ in RESULTS}
        self.csv_file = None
        self.csv_writer = None
        self.t_start = None
        self.last_rx = None
        self.rate = 0.0
        self.sweeps = 0
        self._range = None

        self._build_ui()
        self.on_tab(self.tabs.currentIndex())     # Main tab: no left column
        self._set_connected(False)

        self.worker_thread = QtCore.QThread(self)
        self.worker = Worker()
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.loop)
        self.worker.measured.connect(self.on_measured)
        self.worker.job_done.connect(self.on_job_done)
        self.worker.failed.connect(self.on_failed)
        self.worker_thread.start()

        if args.ip:
            self.ip_edit.setText(args.ip)
        if args.protocol:
            self.proto.setCurrentIndex({"hislip": 0, "vxi11": 1, "socket": 2}[args.protocol])
        if self.bench:
            self.bench.log.connect(self.logmsg)
        else:
            self.logmsg(f"Bench tab disabled - bench_monitor.py could not be loaded ({_BENCH_ERR}). "
                        f"Put it in the same folder as this script.", "warn")
        if not self.can_win:
            self.logmsg(f"CAN tab disabled - canable_analyzer.py / can_commands.py could not be "
                        f"loaded ({_CAN_ERR}). Put them in the same folder as this script.", "warn")
        if args.demo:
            self.proto.setCurrentIndex(3)
            if self.can_win:
                self.can_win.demo_setup()           # BOS / AFAR / UPNG each on its own simulated line
                QtCore.QTimer.singleShot(300, self.can_win.toggle_connect)
            if self.bench:
                self.bench.demo_chk.setChecked(True)
            if self.smw:
                self.smw.proto.setCurrentIndex(3)
                QtCore.QTimer.singleShot(400, self.smw.on_connect)
        if args.connect or args.demo:
            QtCore.QTimer.singleShot(200, self.on_connect)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        pg.setConfigOptions(antialias=False, background="#0f1216", foreground="#c9d1d9")
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        # Top-level tabs span the whole window, so the tab bar never moves; the settings
        # columns live inside the Spectrum Analyzer and Bench pages.
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        root.addWidget(self.tabs, 1)
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)      # Spectrum Analyzer page

        # ---------------- left: settings (scrollable)
        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(4, 4, 4, 4)
        self.w: dict[str, QtWidgets.QWidget] = {}

        # connection
        g = QtWidgets.QGroupBox("FSW connection (LAN)")
        f = QtWidgets.QFormLayout(g)
        self.ip_edit = QtWidgets.QLineEdit("192.168.0.10")
        self.ip_edit.setToolTip("IP / hostname, or a full VISA resource string "
                                "(e.g. TCPIP0::192.168.0.10::hislip0::INSTR)")
        self.proto = QtWidgets.QComboBox()
        self.proto.addItems(PROTOCOLS)
        self.timeout_spin = QtWidgets.QSpinBox()
        self.timeout_spin.setRange(2, 600)
        self.timeout_spin.setValue(30)
        self.timeout_spin.setSuffix(" s")
        self.connect_btn = QtWidgets.QPushButton("Connect")
        self.connect_btn.clicked.connect(self.on_connect_toggle)
        self.idn_label = QtWidgets.QLabel("not connected")
        self.idn_label.setWordWrap(True)
        self.idn_label.setStyleSheet("color:#8a96a3;font-size:11px;")
        self.disp_upd = QtWidgets.QCheckBox("Update FSW screen during remote")
        self.disp_upd.setToolTip("Off = much faster sweeps: the FSW does not have to redraw\n"
                                 "its 4 result windows for every single sweep.")
        self.disp_upd.setChecked(False)
        self.disp_upd.toggled.connect(self.on_display_update)
        f.addRow("Address", self.ip_edit)
        f.addRow("Protocol", self.proto)
        f.addRow("Timeout", self.timeout_spin)
        f.addRow(self.connect_btn)
        f.addRow(self.idn_label)
        f.addRow(self.disp_upd)
        conn_group = g

        # frequency & level
        self.g_level = g = QtWidgets.QGroupBox("Frequency && Level")
        f = QtWidgets.QFormLayout(g)
        self.w["center"] = SIEdit("Hz", "e.g. 2.4 GHz")
        self.w["ref_level"] = QtWidgets.QDoubleSpinBox()
        self.w["ref_level"].setRange(-130, 30)
        self.w["ref_level"].setDecimals(2)
        self.w["ref_level"].setSuffix(" dBm")
        self.w["att_auto"] = QtWidgets.QCheckBox("Auto")
        self.w["att"] = QtWidgets.QDoubleSpinBox()
        self.w["att"].setRange(0, 79)
        self.w["att"].setDecimals(0)
        self.w["att"].setSuffix(" dB")
        self.w["att_auto"].toggled.connect(lambda on: self.w["att"].setEnabled(not on and "att" in self.supported))
        att_row = QtWidgets.QHBoxLayout()
        att_row.addWidget(self.w["att"], 1)
        att_row.addWidget(self.w["att_auto"])
        self.w["preamp"] = QtWidgets.QCheckBox("Preamplifier (B24)")
        self.autolevel_btn = QtWidgets.QPushButton("Auto Level")
        self.autolevel_btn.clicked.connect(self.on_autolevel)
        f.addRow("Center freq", self.w["center"])
        f.addRow("Ref level", self.w["ref_level"])
        f.addRow("Attenuation", att_row)
        f.addRow(self.w["preamp"])
        f.addRow(self.autolevel_btn)
        lv.addWidget(g)

        # signal description
        self.g_sig = g = QtWidgets.QGroupBox("Signal Description")
        f = QtWidgets.QFormLayout(g)
        self.w["modulation"] = QtWidgets.QComboBox()
        self.w["modulation"].addItems(MODULATIONS)
        self.w["symbol_rate"] = SIEdit("Hz", "e.g. 10M")
        self.w["prat"] = QtWidgets.QComboBox()
        self.w["prat"].addItems(OVERSAMPLING)
        self.sr_label = QtWidgets.QLabel("—")
        self.w["tfil_state"] = QtWidgets.QCheckBox("Enabled")
        self.w["tfil_name"] = QtWidgets.QComboBox()
        self.w["tfil_name"].setEditable(True)
        self.w["tfil_name"].addItems(TX_FILTERS)
        self.w["tfil_name"].setToolTip("Any K70 Tx filter name can be typed here")
        self.w["tfil_alpha"] = QtWidgets.QDoubleSpinBox()
        self.w["tfil_alpha"].setRange(0.01, 1.0)
        self.w["tfil_alpha"].setSingleStep(0.01)
        self.w["tfil_alpha"].setDecimals(3)
        self.w["mfil_auto"] = QtWidgets.QCheckBox("Meas filter = Auto (matched)")
        self.w["symbol_rate"].textChanged.connect(self._update_derived)
        self.w["prat"].currentIndexChanged.connect(self._update_derived)
        f.addRow("Modulation", self.w["modulation"])
        f.addRow("Symbol rate", self.w["symbol_rate"])
        f.addRow("Oversampling", self.w["prat"])
        f.addRow("Sample rate", self.sr_label)
        f.addRow("Tx filter", self.w["tfil_state"])
        f.addRow("Filter type", self.w["tfil_name"])
        f.addRow("Alpha / BT", self.w["tfil_alpha"])
        f.addRow(self.w["mfil_auto"])
        lv.addWidget(g)

        # capture
        self.g_cap = g = QtWidgets.QGroupBox("Capture / Result")
        f = QtWidgets.QFormLayout(g)
        self.w["rlen_auto"] = QtWidgets.QCheckBox("Auto")
        self.w["capture_len"] = QtWidgets.QSpinBox()
        self.w["capture_len"].setRange(16, 100_000_000)
        self.w["capture_len"].setSingleStep(1000)
        self.w["capture_len"].setSuffix(" sym")
        self.w["capture_len"].valueChanged.connect(self._update_derived)
        self.w["rlen_auto"].toggled.connect(
            lambda on: self.w["capture_len"].setEnabled(not on and "capture_len" in self.supported))
        cap_row = QtWidgets.QHBoxLayout()
        cap_row.addWidget(self.w["capture_len"], 1)
        cap_row.addWidget(self.w["rlen_auto"])
        self.cap_time = QtWidgets.QLabel("—")
        self.w["result_len"] = QtWidgets.QSpinBox()
        self.w["result_len"].setRange(16, 1_000_000)
        self.w["result_len"].setSingleStep(100)
        self.w["result_len"].setSuffix(" sym")
        self.w["sweep_count"] = QtWidgets.QSpinBox()
        self.w["sweep_count"].setRange(0, 32767)
        self.w["sweep_count"].setToolTip("Statistic count (sweeps per single measurement)")
        f.addRow("Capture length", cap_row)
        f.addRow("Capture time", self.cap_time)
        f.addRow("Result length", self.w["result_len"])
        f.addRow("Statistic count", self.w["sweep_count"])
        lv.addWidget(g)

        # demod / sync / trigger
        self.g_dem = g = QtWidgets.QGroupBox("Demodulation / Sync / Trigger")
        f = QtWidgets.QFormLayout(g)
        self.w["equalizer"] = QtWidgets.QCheckBox("Equalizer")
        self.w["signal_type"] = QtWidgets.QComboBox()
        for label, code in SIGNAL_TYPES:
            self.w["signal_type"].addItem(label, code)
        self.w["pattern_sig"] = QtWidgets.QCheckBox("Signal contains a sync pattern")
        self.w["burst"] = QtWidgets.QCheckBox("Burst search")
        self.w["burst"].setToolTip("Only available when Signal type = Bursted")
        self.w["pattern"] = QtWidgets.QCheckBox("Pattern search")
        self.w["pattern"].setToolTip("Only available when the signal contains a sync pattern\n"
                                     "(pattern selected in the K70 signal description)")
        self.w["signal_type"].currentIndexChanged.connect(self._update_dependencies)
        self.w["pattern_sig"].toggled.connect(self._update_dependencies)
        self.w["trig_source"] = QtWidgets.QComboBox()
        self.w["trig_source"].addItems(TRIGGERS)
        f.addRow(self.w["equalizer"])
        f.addRow("Signal type", self.w["signal_type"])
        f.addRow(self.w["pattern_sig"])
        f.addRow(self.w["burst"])
        f.addRow(self.w["pattern"])
        f.addRow("Trigger", self.w["trig_source"])
        lv.addWidget(g)

        # apply / read
        btns = QtWidgets.QGridLayout()
        self.apply_btn = QtWidgets.QPushButton("Apply ▶ FSW")
        self.apply_btn.setStyleSheet("font-weight:700;")
        self.read_btn = QtWidgets.QPushButton("Read ◀ FSW")
        self.layout_btn = QtWidgets.QPushButton("Restore VSA layout")
        self.apply_btn.clicked.connect(self.on_apply)
        self.read_btn.clicked.connect(self.on_read)
        self.layout_btn.clicked.connect(self.on_layout)
        btns.addWidget(self.apply_btn, 0, 0)
        btns.addWidget(self.read_btn, 0, 1)
        btns.addWidget(self.layout_btn, 1, 0, 1, 2)
        lv.addLayout(btns)
        lv.addStretch(1)

        # left column: connection (shared) + settings of the active tab
        self.settings_stack = QtWidgets.QStackedWidget()
        self.bench = bench_monitor.BenchTab(self) if bench_monitor else None
        pages = [left, self._build_spec_settings()]
        self.can_win = None
        if canable_analyzer:
            # The sniffer is a complete QMainWindow; embedded as a plain widget it keeps
            # its own adapter bar, command library and log. No left settings column.
            self.can_win = canable_analyzer.SnifferWindow(argparse.Namespace(
                demo=False, port=None, interface=None, bitrate=None, connect=False))
            self.can_win.setWindowFlags(QtCore.Qt.Widget)
        self.telem = None
        if telemetry and self.can_win:
            self.telem = telemetry.TelemetryTab()
            self.can_win.frame_listeners.append(self.telem.on_frames)
        for page in pages:
            scroll = QtWidgets.QScrollArea()
            scroll.setWidget(page)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            self.settings_stack.addWidget(scroll)
        left_col = QtWidgets.QWidget()
        lc = QtWidgets.QVBoxLayout(left_col)
        lc.setContentsMargins(0, 0, 0, 0)
        self.conn_group = conn_group
        lc.addWidget(conn_group)
        lc.addWidget(self.settings_stack, 1)
        left_col.setMinimumWidth(410)
        split.addWidget(left_col)
        self.left_col = left_col

        # right column: run controls (shared) + tabs
        right_col = QtWidgets.QWidget()
        rc = QtWidgets.QVBoxLayout(right_col)
        rc.setContentsMargins(0, 0, 0, 0)
        self.run_bar_w = QtWidgets.QWidget()
        self.run_bar = QtWidgets.QHBoxLayout(self.run_bar_w)
        self.run_bar.setContentsMargins(0, 0, 0, 0)
        rc.addWidget(self.run_bar_w)
        split.addWidget(right_col)
        vsa_page = QtWidgets.QSplitter(QtCore.Qt.Horizontal)

        # ---------------- centre: constellation
        mid = QtWidgets.QWidget()
        mv = QtWidgets.QVBoxLayout(mid)
        mv.setContentsMargins(4, 4, 4, 4)
        bar = QtWidgets.QHBoxLayout()
        self.run_btn = QtWidgets.QPushButton("▶ Run")
        self.run_btn.setCheckable(True)
        self.run_btn.toggled.connect(self.on_run)
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItem("Continuous", "cont")
        self.mode_combo.addItem("Single sweeps (synced)", "sync")
        self.mode_combo.setToolTip(
            "Continuous: FSW runs INIT:CONT ON and sweeps back-to-back on its own;\n"
            "the script picks up the latest result every poll interval (fastest, live).\n\n"
            "Single sweeps: the script triggers each sweep (INIT:IMM) and waits for it.\n"
            "Slower, but every value/trace is from one complete fresh sweep (best for CSV logging).")
        self.mode_combo.currentIndexChanged.connect(self.on_mode)
        self.interval_spin = QtWidgets.QSpinBox()
        self.interval_spin.setRange(20, 5000)
        self.interval_spin.setSingleStep(50)
        self.interval_spin.setValue(100)
        self.interval_spin.setPrefix("Poll: ")
        self.interval_spin.setSuffix(" ms")
        self.interval_spin.setToolTip("Continuous mode: how often the latest result is read from the FSW")
        self.interval_spin.valueChanged.connect(lambda v: setattr(self.worker, "interval", v / 1000))
        self.single_btn = QtWidgets.QPushButton("Single")
        self.single_btn.setToolTip("One synchronized sweep (stops continuous mode)")
        self.single_btn.clicked.connect(self.on_single)
        self.persist_spin = QtWidgets.QSpinBox()
        self.persist_spin.setRange(1, 50)
        self.persist_spin.setPrefix("Persistence: ")
        self.persist_spin.setSuffix(" sweeps")
        self.persist_spin.valueChanged.connect(lambda n: setattr(self, "persist", deque(self.persist, maxlen=n)))
        self.autoscale = QtWidgets.QCheckBox("Auto scale")
        self.autoscale.setChecked(True)
        self.reset_btn = QtWidgets.QPushButton("Reset stats")
        self.reset_btn.clicked.connect(self.on_reset)
        self.csv_chk = QtWidgets.QCheckBox("Log CSV")
        self.csv_chk.toggled.connect(self.on_csv)
        for wdg in (self.run_btn, self.mode_combo, self.interval_spin, self.single_btn):
            self.run_bar.addWidget(wdg)
        self.run_bar.addStretch(1)
        for wdg in (self.persist_spin, self.autoscale, self.reset_btn, self.csv_chk):
            bar.addWidget(wdg)
        bar.addStretch(1)
        mv.addLayout(bar)

        self.const_plot = pg.PlotWidget(title="Constellation I/Q (measured)")
        self.const_plot.setAspectLocked(True)
        self.const_plot.showGrid(x=True, y=True, alpha=0.25)
        self.const_plot.setLabel("bottom", "I")
        self.const_plot.setLabel("left", "Q")
        for ax in ("left", "bottom"):
            self.const_plot.getAxis(ax).enableAutoSIPrefix(False)   # no "(x0.001)" scaling
        self.const_plot.addLine(x=0, pen=pg.mkPen("#3a424c"))
        self.const_plot.addLine(y=0, pen=pg.mkPen("#3a424c"))
        # A bare ScatterPlotItem (pixmap fragments) is several times faster than
        # PlotDataItem(symbol=...) for tens of thousands of points.
        self.const_curve = pg.ScatterPlotItem(size=3, pen=None, symbol="s", pxMode=True,
                                              brush=pg.mkBrush(255, 196, 0, 150))
        self.const_plot.addItem(self.const_curve)
        self.const_plot.getPlotItem().setMenuEnabled(False)
        self.const_plot.setRange(xRange=(-1.5, 1.5), yRange=(-1.5, 1.5))
        mv.addWidget(self.const_plot, 1)
        vsa_page.addWidget(mid)

        # ---------------- right: results
        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(4, 4, 4, 4)
        colors = {"mer": "#4fd18b", "power": "#58a6ff", "cfe": "#f0883e"}
        self.tiles = {}
        for k in PRIMARY:
            self.tiles[k] = Tile(RESULT_LABEL[k][0], colors[k])
            rv.addWidget(self.tiles[k])
        extra = QtWidgets.QGroupBox("Additional results")
        ef = QtWidgets.QFormLayout(extra)
        self.extra_labels = {}
        for k, _, lbl, _ in RESULTS:
            if k not in PRIMARY:
                self.extra_labels[k] = QtWidgets.QLabel("—")
                self.extra_labels[k].setStyleSheet("font-size:14px;font-weight:600;")
                ef.addRow(lbl, self.extra_labels[k])
        rv.addWidget(extra)

        trow = QtWidgets.QHBoxLayout()
        trow.addWidget(QtWidgets.QLabel("Trend:"))
        self.trend_sel = QtWidgets.QComboBox()
        for k, _, lbl, unit in RESULTS:
            self.trend_sel.addItem(f"{lbl} [{unit}]", k)
        self.trend_sel.currentIndexChanged.connect(self._redraw_trend)
        trow.addWidget(self.trend_sel, 1)
        rv.addLayout(trow)
        self.trend_plot = pg.PlotWidget()
        self.trend_plot.showGrid(x=True, y=True, alpha=0.25)
        self.trend_plot.setLabel("bottom", "time", units="s")
        self.trend_curve = self.trend_plot.plot([], [], pen=pg.mkPen("#4fd18b", width=2))
        rv.addWidget(self.trend_plot, 1)
        right.setMinimumWidth(270)
        vsa_page.addWidget(right)
        vsa_page.setSizes([820, 380])
        # Every page sits in its own scroll area: a page that needs more room than the
        # window has gets scrollbars instead of forcing the whole window (and the other
        # tabs) wider than the screen.
        def page(w):
            sa = QtWidgets.QScrollArea()
            sa.setWidget(w)
            sa.setWidgetResizable(True)
            sa.setFrameShape(QtWidgets.QFrame.NoFrame)
            return sa

        # Spectrum Analyzer = VSA + Spectrum as sub-tabs (they drive the same FSW)
        self.fsw_tabs = QtWidgets.QTabWidget()
        self.fsw_tabs.addTab(page(vsa_page), "VSA (K70)")
        self.fsw_tabs.addTab(page(self._build_spec_page()), "Spectrum")
        self.fsw_tabs.currentChanged.connect(self.on_fsw_tab)
        self.smw = smw_generator.SmwTab(vsa_settings=lambda: self.last_read) if smw_generator else None
        if self.smw:
            self.smw.log.connect(self.logmsg)
        self.switch = None
        if slave_switch and self.bench:
            self.switch = slave_switch.SwitchController(self.bench, self.logmsg, self)
            if self.can_win:
                self.can_win.frame_listeners.append(self.switch.on_frames)
        self.dash = dashboard.Dashboard(self) if dashboard else None
        if self.dash:
            self.tabs.addTab(page(self.dash), "Main")
        rc.addWidget(self.fsw_tabs, 1)
        self.tabs.addTab(split, "Spectrum Analyzer")
        if self.smw:
            self.tabs.addTab(page(self.smw), "SMW200A")
        if self.bench:
            bench_split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
            bscroll = QtWidgets.QScrollArea()
            bscroll.setWidget(self.bench.settings_panel)
            bscroll.setWidgetResizable(True)
            bscroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            bscroll.setMinimumWidth(380)
            bench_split.addWidget(bscroll)
            bench_split.addWidget(page(self.bench.page))
            bench_split.setSizes([390, 1170])
            self.tabs.addTab(bench_split, "Bench: 176 ch + HMP4040")
        self.temps = None
        if onewire_temp and self.bench:
            self.temps = onewire_temp.TempTab(self.bench)
            self.bench.folder_listeners.append(self.temps.folder_changed)
            self.tabs.addTab(page(self.temps), "1-Wire Temp")
        if self.can_win:
            self.tabs.addTab(page(self.can_win), "CAN")
        if self.telem:
            self.tabs.addTab(page(self.telem), "Telemetry")
        if self.telem and self.dash:
            self.telem.array_listeners.append(self.dash.on_array)
        self.logs = None
        if log_viewer:
            self.logs = log_viewer.LogsTab(
                folder_fn=(lambda: self.bench.log_folder) if self.bench else None)
            self.tabs.addTab(self.logs, "Logs")
        self.tabs.currentChanged.connect(self.on_tab)
        split.setSizes([420, 1140])

        # ---------------- bottom: log
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(100)
        self.log.setStyleSheet("font-family:Consolas,monospace;font-size:11px;")
        root.addWidget(self.log)
        self.status = self.statusBar()

        self.setStyleSheet("""
            QWidget{background:#0f1216;color:#c9d1d9;}
            QGroupBox{border:1px solid #2a3038;border-radius:6px;margin-top:14px;padding-top:6px;font-weight:600;}
            QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 4px;color:#8a96a3;}
            QLineEdit,QSpinBox,QDoubleSpinBox,QComboBox,QPlainTextEdit{background:#161b22;border:1px solid #30363d;
                border-radius:4px;padding:3px;}
            QPushButton{background:#21262d;border:1px solid #30363d;border-radius:5px;padding:5px 10px;}
            QPushButton:hover{background:#2d333b;}
            QPushButton:checked{background:#1f6f43;border-color:#2ea043;}
            QPushButton:disabled,QLineEdit:disabled,QSpinBox:disabled,QDoubleSpinBox:disabled,
            QComboBox:disabled,QCheckBox:disabled{color:#4d5560;}
            QTabWidget::tab-bar{alignment:left;left:0px;}
            QTabBar{qproperty-expanding:0;qproperty-usesScrollButtons:1;}
            QTabBar::tab{background:#161b22;border:1px solid #2a3038;padding:5px 14px;margin-right:2px;}
            QTabBar::tab:selected{background:#21262d;color:#ffffff;border-bottom:2px solid #58a6ff;}
        """)

    # ------------------------------------------------------ spectrum tab
    def _build_spec_settings(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(page)
        lv.setContentsMargins(4, 4, 4, 4)
        ws = self.ws = {}

        self.g_spec_freq = g = QtWidgets.QGroupBox("Frequency")
        f = QtWidgets.QFormLayout(g)
        ws["center"] = SIEdit("Hz", "e.g. 2.4 GHz")
        ws["span"] = SIEdit("Hz", "e.g. 40 MHz")
        self.spec_from_vsa = QtWidgets.QPushButton("Center / span from VSA signal")
        self.spec_from_vsa.setToolTip("Center = VSA center frequency, span = 2 × occupied bandwidth\n"
                                      "(symbol rate × (1 + alpha)); also sets the channel bandwidth")
        self.spec_from_vsa.clicked.connect(self.on_spec_from_vsa)
        f.addRow("Center", ws["center"])
        f.addRow("Span", ws["span"])
        f.addRow(self.spec_from_vsa)
        lv.addWidget(g)

        self.g_spec_level = g = QtWidgets.QGroupBox("Level")
        f = QtWidgets.QFormLayout(g)
        ws["ref_level"] = QtWidgets.QDoubleSpinBox()
        ws["ref_level"].setRange(-130, 30)
        ws["ref_level"].setDecimals(2)
        ws["ref_level"].setSuffix(" dBm")
        ws["range"] = QtWidgets.QDoubleSpinBox()
        ws["range"].setRange(1, 200)
        ws["range"].setDecimals(0)
        ws["range"].setSuffix(" dB")
        ws["att_auto"] = QtWidgets.QCheckBox("Auto")
        ws["att"] = QtWidgets.QDoubleSpinBox()
        ws["att"].setRange(0, 79)
        ws["att"].setDecimals(0)
        ws["att"].setSuffix(" dB")
        ws["preamp"] = QtWidgets.QCheckBox("Preamplifier (B24)")
        self.spec_autolevel_btn = QtWidgets.QPushButton("Auto Level")
        self.spec_autolevel_btn.clicked.connect(self.on_autolevel)
        f.addRow("Ref level", ws["ref_level"])
        f.addRow("Display range", ws["range"])
        f.addRow("Attenuation", with_auto(ws["att"], ws["att_auto"]))
        f.addRow(ws["preamp"])
        f.addRow(self.spec_autolevel_btn)
        lv.addWidget(g)

        self.g_spec_bw = g = QtWidgets.QGroupBox("Bandwidth / Sweep / Trace")
        f = QtWidgets.QFormLayout(g)
        for key, unit, ex in (("rbw", "Hz", "e.g. 100k"), ("vbw", "Hz", "e.g. 300k"), ("swt", "s", "e.g. 10m")):
            ws[key] = SIEdit(unit, ex)
            ws[f"{key}_auto"] = QtWidgets.QCheckBox("Auto")
        ws["points"] = QtWidgets.QSpinBox()
        ws["points"].setRange(101, 100001)
        ws["points"].setSingleStep(500)
        ws["detector"] = combo(DETECTORS)
        ws["trace_mode"] = combo(TRACE_MODES)
        ws["count"] = QtWidgets.QSpinBox()
        ws["count"].setRange(0, 32767)
        ws["count"].setToolTip("Sweep / average count (used by Average, Max hold in single sweep)")
        f.addRow("RBW", with_auto(ws["rbw"], ws["rbw_auto"]))
        f.addRow("VBW", with_auto(ws["vbw"], ws["vbw_auto"]))
        f.addRow("Sweep time", with_auto(ws["swt"], ws["swt_auto"]))
        f.addRow("Sweep points", ws["points"])
        f.addRow("Detector", ws["detector"])
        f.addRow("Trace mode", ws["trace_mode"])
        f.addRow("Sweep/avg count", ws["count"])
        lv.addWidget(g)
        for key in ("att", "rbw", "vbw", "swt"):
            ws[f"{key}_auto"].toggled.connect(self._update_spec_dependencies)

        g = QtWidgets.QGroupBox("Channel power (computed here)")
        f = QtWidgets.QFormLayout(g)
        self.chan_bw = SIEdit("Hz", "e.g. 13.5M")
        self.chan_bw.setToolTip("Integration bandwidth around the center frequency.\n"
                                "Computed from the trace; use the RMS detector for correct results.")
        f.addRow("Channel BW", self.chan_bw)
        lv.addWidget(g)

        btns = QtWidgets.QGridLayout()
        self.spec_apply_btn = QtWidgets.QPushButton("Apply ▶ FSW")
        self.spec_apply_btn.setStyleSheet("font-weight:700;")
        self.spec_read_btn = QtWidgets.QPushButton("Read ◀ FSW")
        self.spec_apply_btn.clicked.connect(self.on_apply)
        self.spec_read_btn.clicked.connect(self.on_read)
        btns.addWidget(self.spec_apply_btn, 0, 0)
        btns.addWidget(self.spec_read_btn, 0, 1)
        lv.addLayout(btns)
        lv.addStretch(1)
        return page

    def _build_spec_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.spec_plot = pg.PlotWidget(title="Spectrum (FSW Spectrum channel, trace 1)")
        self.spec_plot.showGrid(x=True, y=True, alpha=0.25)
        self.spec_plot.setLabel("bottom", "Frequency", units="Hz")
        self.spec_plot.setLabel("left", "Level", units="dBm")
        self.spec_plot.getPlotItem().setMenuEnabled(False)
        self.spec_curve = self.spec_plot.plot([], [], pen=pg.mkPen("#ffc400", width=1))
        self.spec_peak = pg.ScatterPlotItem(size=9, symbol="t", pen=None, brush=pg.mkBrush("#ff6b6b"))
        self.spec_plot.addItem(self.spec_peak)
        self.spec_chan = pg.LinearRegionItem(movable=False, brush=pg.mkBrush(88, 166, 255, 30),
                                             pen=pg.mkPen(88, 166, 255, 120))
        self.spec_plot.addItem(self.spec_chan)
        self.spec_chan.hide()
        page.addWidget(self.spec_plot)

        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(4, 4, 4, 4)
        self.spec_tiles = {
            "peak_f": Tile("Peak frequency", "#f0883e"),
            "peak_p": Tile("Peak level", "#58a6ff"),
            "chp":    Tile("Channel power", "#4fd18b"),
        }
        for t in self.spec_tiles.values():
            t.sub.setText("")
            rv.addWidget(t)
        self.spec_rbw_label = QtWidgets.QLabel("RBW —")
        self.spec_rbw_label.setStyleSheet("color:#8a96a3;")
        rv.addWidget(self.spec_rbw_label)
        rv.addStretch(1)
        right.setMinimumWidth(300)
        page.addWidget(right)
        page.setSizes([880, 320])
        return page

    def _update_spec_dependencies(self, *_):
        for key in ("att", "rbw", "vbw", "swt"):
            self.ws[key].setEnabled(key in self.spec_supported and not self.ws[f"{key}_auto"].isChecked())

    def populate_spec(self, s: dict):
        self.spec_supported = {k for k, v in s.items() if v is not None}
        for key, wdg in self.ws.items():
            wdg.setEnabled(key in self.spec_supported)
            if s.get(key) is not None:
                wset(wdg, s[key])
        self._update_spec_dependencies()
        self.last_read_spec = self.collect_spec()
        if s.get("ref_level") is not None and s.get("range"):
            self.spec_plot.setYRange(s["ref_level"] - s["range"], s["ref_level"], padding=0)
        missing = sorted(set(s) - self.spec_supported)
        if missing:
            self.logmsg(f"Spectrum: not available (disabled): {', '.join(missing)}", "warn")

    def collect_spec(self) -> dict:
        return {k: wget(w) for k, w in self.ws.items() if k in self.spec_supported}

    def on_spec_from_vsa(self):
        v = self.last_read
        sr = v.get("symbol_rate")
        if not sr or v.get("center") is None:
            self.logmsg("Read the VSA settings first (VSA tab)", "warn")
            return
        obw = sr * (1 + (v.get("tfil_alpha") or 0.35))
        self.ws["center"].setValue(v["center"])
        self.ws["span"].setValue(2 * obw)
        self.chan_bw.setValue(obw)
        self.logmsg("Center / span taken from the VSA settings - press Apply to send them")

    def _draw_spec(self, r: dict):
        f, p = r["f"], r["p"]
        self.spec_curve.setData(f, p, connect="finite")
        if f.size and (self._spec_xrange is None or self._spec_xrange != (f[0], f[-1])):
            self._spec_xrange = (f[0], f[-1])
            self.spec_plot.setXRange(f[0], f[-1], padding=0)
        if not np.isfinite(p).any():
            return
        k = int(np.nanargmax(p))
        self.spec_peak.setData([f[k]], [p[k]])
        self.spec_tiles["peak_f"].value.setText(fmt_si(f[k], "Hz", 9))
        self.spec_tiles["peak_p"].value.setText(f"{p[k]:.2f} dBm")
        rbw = r.get("rbw")
        self.spec_rbw_label.setText(f"RBW {fmt_si(rbw, 'Hz', 4)}    {f.size} points" if rbw else "RBW —")
        # Channel power: integrate the trace over the channel, scaled from the RBW
        # (Gaussian filter, noise bandwidth ≈ 1.065 × RBW) to the bin spacing.
        try:
            cbw = self.chan_bw.value()
        except ValueError:
            cbw = None
        if cbw and rbw and f.size > 1:
            fc = 0.5 * (f[0] + f[-1])
            sel = (np.abs(f - fc) <= cbw / 2) & np.isfinite(p)
            self.spec_chan.setRegion((fc - cbw / 2, fc + cbw / 2))
            self.spec_chan.show()
            if sel.any():
                df = (f[-1] - f[0]) / (f.size - 1)
                mw = np.sum(10 ** (p[sel] / 10)) * df / (1.065 * rbw)
                self.spec_tiles["chp"].value.setText(f"{10 * math.log10(mw):.2f} dBm")
                self.spec_tiles["chp"].sub.setText(f"in {fmt_si(cbw, 'Hz', 6)}  (RMS detector recommended)")
        else:
            self.spec_chan.hide()
            self.spec_tiles["chp"].value.setText("—")
            self.spec_tiles["chp"].sub.setText("set a channel BW")

    def app(self) -> str:
        """FSW application of the Spectrum Analyzer sub-tab: 'vsa' or 'spec'."""
        return "spec" if self.fsw_tabs.currentIndex() == 1 else "vsa"

    def on_tab(self, idx):
        w = self.tabs.widget(idx)
        shown = w.widget() if isinstance(w, QtWidgets.QScrollArea) else w
        self.log.setVisible(not (self.can_win is not None and shown is self.can_win))   # CAN has its own

    def on_fsw_tab(self, idx):
        self.settings_stack.setCurrentIndex(idx)
        self.last_rx = None
        if self.connected:
            self.logmsg("FSW channel: " + ("Spectrum" if idx == 1 else "VSA (K70)"))
            self.on_read()              # also switches the FSW to that channel

    # ------------------------------------------------------------ helpers
    def logmsg(self, text: str, level: str = "info"):
        color = {"info": "#c9d1d9", "ok": "#4fd18b", "warn": "#f0b03e", "err": "#ff6b6b"}[level]
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        self.log.appendHtml(f'<span style="color:#6e7681">{ts}</span> '
                            f'<span style="color:{color}">{text}</span>')

    def _set_connected(self, on: bool):
        self.connected = on
        for wdg in (self.g_level, self.g_sig, self.g_cap, self.g_dem, self.apply_btn, self.read_btn,
                    self.layout_btn, self.run_btn, self.single_btn, self.g_spec_freq,
                    self.g_spec_level, self.g_spec_bw, self.spec_apply_btn, self.spec_read_btn):
            wdg.setEnabled(on)
        self.connect_btn.setText("Disconnect" if on else "Connect")
        self.ip_edit.setEnabled(not on)
        self.proto.setEnabled(not on)
        if not on:
            self.run_btn.setChecked(False)
            self.idn_label.setText("not connected")

    def _resource(self) -> str:
        addr = self.ip_edit.text().strip()
        if "::" in addr:
            return addr
        return [f"TCPIP0::{addr}::hislip0::INSTR",
                f"TCPIP0::{addr}::inst0::INSTR",
                f"TCPIP0::{addr}::5025::SOCKET"][self.proto.currentIndex()]

    def _update_derived(self, *_):
        try:
            sr = parse_si(self.w["symbol_rate"].text())
            ovs = int(self.w["prat"].currentText())
            self.sr_label.setText(fmt_si(sr * ovs, "Hz", 8) + f"  ({ovs} × SR)")
            self.cap_time.setText(fmt_si(self.w["capture_len"].value() / sr, "s", 6))
        except Exception:
            self.sr_label.setText("—")
            self.cap_time.setText("—")

    def _update_dependencies(self, *_):
        w, sup = self.w, self.supported
        bursted = "signal_type" not in sup or w["signal_type"].currentData() != "CONT"
        has_pattern = "pattern_sig" not in sup or w["pattern_sig"].isChecked()
        w["burst"].setEnabled("burst" in sup and bursted)
        w["pattern"].setEnabled("pattern" in sup and has_pattern)

    @staticmethod
    def _set_combo_data(combo: QtWidgets.QComboBox, data):
        idx = combo.findData(data)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    @staticmethod
    def _set_combo(combo: QtWidgets.QComboBox, text: str):
        idx = combo.findText(text, QtCore.Qt.MatchFixedString)
        if idx < 0:
            combo.addItem(text)
            idx = combo.count() - 1
        combo.setCurrentIndex(idx)

    # ---------------------------------------------------------- settings
    def populate(self, s: dict):
        self.supported = {k for k, v in s.items() if v is not None}
        w = self.w
        for key, wdg in w.items():
            wdg.setEnabled(key in self.supported)
        setters = {
            "center": lambda v: w["center"].setValue(v),
            "ref_level": lambda v: w["ref_level"].setValue(v),
            "att_auto": lambda v: w["att_auto"].setChecked(v),
            "att": lambda v: w["att"].setValue(v),
            "preamp": lambda v: w["preamp"].setChecked(v),
            "modulation": lambda v: self._set_combo(w["modulation"], v),
            "symbol_rate": lambda v: w["symbol_rate"].setValue(v),
            "prat": lambda v: self._set_combo(w["prat"], str(int(v))),
            "tfil_state": lambda v: w["tfil_state"].setChecked(v),
            "tfil_name": lambda v: self._set_combo(w["tfil_name"], v),
            "tfil_alpha": lambda v: w["tfil_alpha"].setValue(v),
            "mfil_auto": lambda v: w["mfil_auto"].setChecked(v),
            "rlen_auto": lambda v: w["rlen_auto"].setChecked(v),
            "capture_len": lambda v: w["capture_len"].setValue(int(v)),
            "result_len": lambda v: w["result_len"].setValue(int(v)),
            "sweep_count": lambda v: w["sweep_count"].setValue(int(v)),
            "equalizer": lambda v: w["equalizer"].setChecked(v),
            "signal_type": lambda v: self._set_combo_data(w["signal_type"], v),
            "pattern_sig": lambda v: w["pattern_sig"].setChecked(v),
            "burst": lambda v: w["burst"].setChecked(v),
            "pattern": lambda v: w["pattern"].setChecked(v),
            "trig_source": lambda v: self._set_combo(w["trig_source"], v),
        }
        for k, fn in setters.items():
            if s.get(k) is not None:
                fn(s[k])
        w["att"].setEnabled("att" in self.supported and not w["att_auto"].isChecked())
        w["capture_len"].setEnabled("capture_len" in self.supported and not w["rlen_auto"].isChecked())
        self._update_dependencies()
        self._update_derived()
        # remember exactly what the widgets now show -> Apply sends only what the user changed
        self.last_read = self.collect()
        missing = sorted(set(s) - self.supported - self._warned_missing)
        if missing:
            self._warned_missing |= set(missing)
            self.logmsg(f"Not available on this instrument/firmware (disabled): {', '.join(missing)}", "warn")

    def collect(self) -> dict:
        w = self.w
        getters = {
            "center": lambda: w["center"].value(),
            "ref_level": lambda: w["ref_level"].value(),
            "att_auto": lambda: w["att_auto"].isChecked(),
            "att": lambda: w["att"].value(),
            "preamp": lambda: w["preamp"].isChecked(),
            "modulation": lambda: w["modulation"].currentText(),
            "symbol_rate": lambda: w["symbol_rate"].value(),
            "prat": lambda: int(w["prat"].currentText()),
            "tfil_state": lambda: w["tfil_state"].isChecked(),
            "tfil_name": lambda: w["tfil_name"].currentText().strip(),
            "tfil_alpha": lambda: w["tfil_alpha"].value(),
            "mfil_auto": lambda: w["mfil_auto"].isChecked(),
            "rlen_auto": lambda: w["rlen_auto"].isChecked(),
            "capture_len": lambda: w["capture_len"].value(),
            "result_len": lambda: w["result_len"].value(),
            "sweep_count": lambda: w["sweep_count"].value(),
            "equalizer": lambda: w["equalizer"].isChecked(),
            "signal_type": lambda: w["signal_type"].currentData(),
            "pattern_sig": lambda: w["pattern_sig"].isChecked(),
            "burst": lambda: w["burst"].isChecked(),
            "pattern": lambda: w["pattern"].isChecked(),
            "trig_source": lambda: w["trig_source"].currentText().strip(),
        }
        return {k: g() for k, g in getters.items() if k in self.supported}

    # ----------------------------------------------------------- actions
    def on_connect_toggle(self):
        if self.connected:
            self.on_disconnect()
        else:
            self.on_connect()

    def on_connect(self):
        demo = self.proto.currentIndex() == 3
        res = None if demo else self._resource()
        tmo = self.timeout_spin.value() * 1000
        self.connect_btn.setEnabled(False)
        self.logmsg("Starting DEMO (simulated FSW)…" if demo else f"Connecting to {res} …")

        def job():
            dev = DemoFsw() if demo else FswVsa(res, tmo)
            self.worker.dev = dev
            return dev
        self.worker.submit("connect", job)

    def on_disconnect(self):
        self.worker.running = False

        def job():
            dev, self.worker.dev = self.worker.dev, None
            if dev:
                dev.close()
        self.worker.submit("disconnect", job)

    def on_read(self):
        if self.app() == "spec":
            self.worker.submit("read_spec", lambda: self.worker.dev.read_spec_settings())
        else:
            self.worker.submit("read", lambda: self.worker.dev.read_settings())

    def on_apply(self):
        if self.app() == "spec":
            try:
                s = self.collect_spec()
            except ValueError as e:
                QtWidgets.QMessageBox.warning(self, "Invalid value", f"Could not parse a field: {e}")
                return
            prev = dict(self.last_read_spec)
            self.worker.submit("apply_spec", lambda: self.worker.dev.apply_spec_settings(s, prev))
            return
        try:
            s = self.collect()
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "Invalid value", f"Could not parse a field: {e}")
            return
        prev = dict(self.last_read)
        self.worker.submit("apply", lambda: self.worker.dev.apply_settings(s, prev))

    def on_autolevel(self):
        self.logmsg("Auto level running…")
        app = self.app()
        self.worker.submit("autolevel", lambda: self.worker.dev.auto_level(app))

    def on_layout(self):
        self.worker.submit("layout", lambda: self.worker.dev.preset_layout())

    def on_display_update(self, on):
        if self.connected:
            self.worker.submit("display", lambda: self.worker.dev.set_display_update(on))

    def on_mode(self, *_):
        mode = self.mode_combo.currentData()
        self.worker.mode = mode
        self.interval_spin.setEnabled(mode == "cont")
        if self.worker.running:
            self.logmsg("Mode: " + self.mode_combo.currentText())
            self._speed_hints()

    def on_run(self, on):
        self.run_btn.setText("■ Stop" if on else "▶ Run")
        self.single_btn.setEnabled(self.connected and not on)
        self.worker.running = on
        if on:
            self.last_rx = None
            self._speed_hints()

    def _speed_hints(self):
        """Warn about settings that make every live update slow."""
        if self.app() != "vsa":
            return
        s = self.last_read
        if self.worker.mode == "sync" and (s.get("sweep_count") or 0) > 1:
            self.logmsg(f"Statistic count = {s['sweep_count']}: every update waits for "
                        f"{s['sweep_count']} result ranges. Set it to 1 for a fast live view.", "warn")
        cap, res = s.get("capture_len"), s.get("result_len")
        if not s.get("rlen_auto") and cap and res and cap > 10 * res:
            self.logmsg(f"Capture length ({cap} sym) is {cap // res}× the result length - the FSW "
                        f"demodulates the whole capture every sweep. Reduce it or set Auto.", "warn")
        if self.disp_upd.isChecked():
            self.logmsg("FSW screen update is ON - sweeps are slower. Uncheck it for max speed.")

    def on_single(self):
        self.worker.single = True

    def on_reset(self):
        for st in self.stats.values():
            st.reset()
        for d in self.trend.values():
            d.clear()
        self.t_start = None
        self.sweeps = 0
        self.persist.clear()
        self._redraw_trend()
        for k in PRIMARY:
            self.tiles[k].sub.setText("mean —   min —   max —")

    def on_csv(self, on):
        if on:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Log results to CSV",
                f"fsw_vsa_{_dt.datetime.now():%Y%m%d_%H%M%S}.csv", "CSV (*.csv)")
            if not path:
                self.csv_chk.blockSignals(True)
                self.csv_chk.setChecked(False)
                self.csv_chk.blockSignals(False)
                return
            self.csv_file = open(path, "w", newline="")
            self.csv_writer = csv.writer(self.csv_file)
            self.csv_writer.writerow(["timestamp"] + [f"{lbl} [{u}]" for _, _, lbl, u in RESULTS])
            self.logmsg(f"Logging to {path}", "ok")
        elif self.csv_file:
            self.csv_file.close()
            self.csv_file = self.csv_writer = None
            self.logmsg("CSV logging stopped")

    # ---------------------------------------------------------- callbacks
    def on_job_done(self, tag: str, result):
        if tag == "connect":
            self._set_connected(True)
            self.connect_btn.setEnabled(True)
            self.idn_label.setText(f"{result.idn}\nChannel: {result.channel}")
            self.logmsg(f"Connected: {result.idn}", "ok")
            self.on_display_update(self.disp_upd.isChecked())
            self.on_read()
        elif tag == "disconnect":
            self._set_connected(False)
            self.logmsg("Disconnected")
        elif tag == "read":
            self.populate(result)
            self.logmsg("Settings read from FSW", "ok")
        elif tag == "read_spec":
            self.populate_spec(result)
            self.logmsg("Spectrum settings read from FSW", "ok")
        elif tag in ("apply", "apply_spec"):
            cmds, errors, notes = result
            for n in notes:
                self.logmsg(n, "warn")
            for e in errors:
                self.logmsg(e, "err")
            if not cmds:
                self.logmsg("Nothing changed - no settings sent")
            else:
                self.logmsg(f"Applied {len(cmds)} setting(s): " + "; ".join(cmds)
                            + (f"  —  {len(errors)} error(s)" if errors else ""),
                            "warn" if errors else "ok")
            self.on_read()   # read back what the instrument actually accepted
        elif tag == "autolevel":
            self.logmsg("Auto level done", "ok")
            self.on_read()
        elif tag == "layout":
            self.logmsg("VSA layout restored (W3 = Result Summary, W4 = Constellation)", "ok")

    def on_failed(self, tag: str, msg: str):
        self.logmsg(f"[{tag}] {msg}", "err")
        if tag == "connect":
            self.connect_btn.setEnabled(True)
            self.worker.dev = None
        if tag == "measure":
            self.run_btn.setChecked(False)

    def on_measured(self, r: dict):
        try:
            self._draw(r)
        finally:
            self.worker.ack()

    def _draw(self, r: dict):
        if self.dash:
            self.dash.on_measurement(r)
        t_draw = time.perf_counter()
        now = r["t"]
        self.sweeps += 1
        if r.get("app") == "spec":
            self._draw_spec(r)
            npts = r["f"].size
        else:
            self._draw_vsa(r)
            npts = r["i"].size
        if self.last_rx is not None:
            dt = now - self.last_rx
            if dt > 0:
                self.rate = 0.8 * self.rate + 0.2 / dt if self.rate else 1 / dt
        self.last_rx = now
        ms = lambda k: r.get(k, 0) * 1e3
        draw = (time.perf_counter() - t_draw) * 1e3
        what = "results" if r.get("app") != "spec" else "axis"
        if r.get("mode") == "cont":
            detail = (f"Continuous   read: trace {ms('t_trace'):.0f} · {what} {ms('t_res'):.0f}"
                      f" · draw {draw:.0f} ms")
        else:
            detail = (f"Single   last: {ms('sweep_time'):.0f} ms  (FSW sweep {ms('t_acq'):.0f}"
                      f" · trace {ms('t_trace'):.0f} · {what} {ms('t_res'):.0f} · draw {draw:.0f} ms)")
        self.status.showMessage(f"Updates: {self.sweeps}    Rate: {self.rate:.1f} /s    "
                                f"Points: {npts}    {detail}")

    def _draw_vsa(self, r: dict):
        if self.dash:
            self.dash.update_vsa(r)
        if self.switch:
            self.switch.on_vsa(r)
        now = r["t"]
        if self.t_start is None:
            self.t_start = now

        # constellation
        i, q = r["i"], r["q"]
        ok = np.isfinite(i) & np.isfinite(q) & (np.abs(i) < 1e30) & (np.abs(q) < 1e30)
        self.persist.append((i[ok], q[ok]))
        I = np.concatenate([p[0] for p in self.persist])
        Q = np.concatenate([p[1] for p in self.persist])
        if I.size > MAX_PLOT_POINTS:
            step = int(math.ceil(I.size / MAX_PLOT_POINTS))
            I, Q = I[::step], Q[::step]
        self.const_curve.setData(x=I, y=Q)
        if self.autoscale.isChecked() and I.size:
            m = float(np.percentile(np.maximum(np.abs(I), np.abs(Q)), 99.9)) * 1.15 or 1.0
            if self._range is None or not (0.85 < m / self._range < 1.15):
                self._range = m
                self.const_plot.setRange(xRange=(-m, m), yRange=(-m, m), padding=0)

        # numbers
        for k, *_ in RESULTS:
            v = r.get(k, float("nan"))
            self.stats[k].add(v)
            if math.isfinite(v):
                self.trend[k].append((now - self.t_start, v))
        for k in PRIMARY:
            st = self.stats[k]
            self.tiles[k].value.setText(fmt_result(k, r.get(k)))
            if st.n:
                self.tiles[k].sub.setText(f"mean {fmt_result(k, st.mean)}   "
                                          f"min {fmt_result(k, st.min)}   max {fmt_result(k, st.max)}")
        for k, lbl in self.extra_labels.items():
            lbl.setText(fmt_result(k, r.get(k)))
        self._redraw_trend()

        if self.csv_writer:
            self.csv_writer.writerow([_dt.datetime.fromtimestamp(now).isoformat(timespec="milliseconds")]
                                     + [f"{r.get(k, float('nan')):.6g}" for k, *_ in RESULTS])
            self.csv_file.flush()

    def _redraw_trend(self, *_):
        k = self.trend_sel.currentData()
        data = self.trend.get(k)
        if data:
            t, v = zip(*data)
            self.trend_curve.setData(np.array(t), np.array(v))
        else:
            self.trend_curve.setData([], [])

    def closeEvent(self, ev):
        if self.dash:
            self.dash.shutdown()
        if self.temps:
            self.temps.shutdown()
        if self.switch:
            self.switch.shutdown()
        if self.bench:
            if not self.bench.confirm_close(self):
                ev.ignore()
                return
            self.bench.shutdown()
        if self.can_win:
            self.can_win.close()            # stops worker, script, recording
        if self.smw:
            self.smw.shutdown()
        self.worker.running = False
        self.worker.quit = True
        self.worker_thread.quit()
        self.worker_thread.wait(5000)
        if self.worker.dev:
            try:
                self.worker.dev.close()
            except Exception:
                pass
        if self.csv_file:
            self.csv_file.close()
        ev.accept()


# --------------------------------------------------------------------------- #
APP_ID = "FSW.VSAMonitor.Bench.1"      # any unique string: Company.Product.SubProduct.Version
ICON_FILE = "app.ico"


def resource_path(name: str) -> str:
    """Path of a bundled file: works for 'python script.py' and for a PyInstaller
    EXE (one-file builds unpack into sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def set_windows_app_id():
    """Without this, Windows groups the process under python.exe / pythonw.exe and
    the taskbar shows the Python icon instead of ours. Call before any window exists."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="R&S FSW VSA (K70) live monitor")
    ap.add_argument("--ip", help="instrument IP / hostname or full VISA resource string")
    ap.add_argument("--protocol", choices=["hislip", "vxi11", "socket"], help="LAN protocol")
    ap.add_argument("--connect", action="store_true", help="connect on start-up")
    ap.add_argument("--demo", action="store_true", help="simulated instrument, no hardware needed")
    args = ap.parse_args()

    set_windows_app_id()
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    icon = QtGui.QIcon(resource_path(ICON_FILE))
    if not icon.isNull():
        app.setWindowIcon(icon)           # title bar, taskbar, Alt+Tab, all dialogs
    win = MainWindow(args)
    if not icon.isNull():
        # System tray (notification area) icon - Windows shows nothing there by default.
        win.tray = QtWidgets.QSystemTrayIcon(icon, win)
        win.tray.setToolTip("FSW VSA Monitor + Bench")
        win.tray.activated.connect(lambda *_: (win.showNormal(), win.raise_(), win.activateWindow()))
        win.tray.show()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
