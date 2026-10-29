# -*- coding: utf-8 -*-
"""
tfg_generator.py
================
Suin TFG6806 (TFG6800 series, 2 channels, 65 MHz) function / arbitrary waveform generator tab.

Connection (as in the maker's PC software), commands end with LF:
  LAN  raw SCPI socket, port 5025 ("TCPIP0::<ip>::5025::SOCKET")
  USB  USB-TMC through VISA, e.g. "USB0::1267::260::CH372USB::0::INSTR" (rear USB Device port;
       needs a VISA library: Keysight IO Libraries / NI-VISA, or pyvisa-py + pyusb + libusb)

SCPI taken from the TFG6800 Programmer's Guide (12/2024), sent in the long form exactly as written
there, with the channel number: SOURce1: / SOURce2:, OUTPut1 / OUTPut2.
    Basic     FUNCtion, FUNCtion:MODE, FREQuency, VOLTage (Vpp), VOLTage:OFFSet, PHASe,
              FUNCtion:SQUare:DCYCle, FUNCtion:RAMP:SYMMetry, FUNCtion:PULSe:WIDTh/RISing/FALLing,
              FUNCtion:PRBS, FUNCtion:PRBS:BITRate
    Output    OUTPut, OUTPut:LOAD:STYLe {50|RES|HRES}, OUTPut:LOAD, OUTPut:POLarity, OUTPut:SYNC,
              VOLTage:LIMit:HIGH / LOW
    Modulate  MODulate:MODE/SOURce/FUNCtion, AM:INTernal:FREQuency, AM:DEPTh, AM:DSSC, FM:..., PM:...,
              PWM:..., SUM:..., FSKey:..., NFSKey/4FSKey:FREQuency1-3, PSK:..., NPSK/4PSK:PHASe1-3,
              ASK:..., OSK:...
    Sweep     SWEep:SPACing, FREQuency:STARt/STOP, MARKer:FREQuency, SWEep:TIME/HTIMe/RTIMe/INTerval,
              SWEep:TRIGger:SOURce/OUTPut
    Burst     BURSt:MODE, BURSt:NCYCles, BURSt:INTernal:PERiod, BURSt:PHASe, BURSt:DELay,
              BURSt:TRIGger:SOURce/SLOPe/OUTPut
    Double    DOUBle:FIRSt:WIDTh, DOUBle:SECond:WIDTh, DOUBle:INTernal:TIMe, DOUBle:PULSe:STATe
    System    *IDN?, *RST, *CLS, *TRG, *SAV n, *RCL n, SYSTem:ERRor?, SYSTem:LOCAl, PHASe:ALIgn
Everything else (coupling, harmonics, waveform sequence, counter, ...) through the console.

TFG6806 limits (Users' Guide, chapter 5): sine/square 1 uHz-65 MHz, pulse 25 MHz, ramp 3 MHz,
other waveforms 20 MHz; amplitude 2 mVpp-20 Vpp (high-Z, f <= 10 MHz), 10 Vpp above 10 MHz;
offset +/-10 Vpk (ac+dc); modulating frequency 1 mHz-100 kHz; burst count 1-100000000.
"""

from __future__ import annotations

import math
import queue
import re
import socket
import time

from PyQt5 import QtCore, QtWidgets

try:
    import pyvisa
except ImportError:  # pragma: no cover - LAN works without it
    pyvisa = None

DEFAULT_PORT = 5025
USB_IDS = ("1267::260", "0X04F3::0X0104")     # vendor::product of the TFG6800 (decimal / hex form)
CHANNELS = (1, 2)
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")

WAVES = [("Sine", "SINusoid"), ("Square", "SQUare"), ("Ramp", "RAMP"), ("Pulse", "PULSe"),
         ("Noise", "NOISe"), ("Built-in / arb #", "ARB")]
MODES = [("Continuous", "CONTinuous"), ("Modulation", "MODulate"), ("Sweep", "SWEep"),
         ("Burst", "BURSt"), ("Double pulse", "DOUBle")]
MOD_TYPES = [(m, m) for m in ("AM", "FM", "PM", "PWM", "SUM", "FSK", "NFSK", "4FSK", "PSK", "NPSK",
                               "4PSK", "ASK", "OSK", "QAM")]
MOD_WAVES = WAVES[:5]
SRC_IE = [("Internal", "INTernal"), ("External", "EXTernal")]
SRC_IEM = SRC_IE + [("Manual", "MANual")]
TRIG_OUT = [("Off", "OFF"), ("Rising", "RAIse"), ("Falling", "FALL")]

# key, label, kind, SCPI (long form, {s} = "SOURce<ch>:", {o} = "OUTPut<ch>"), group, shown-when, choices
#   kind: hz s vpp v ohm bps deg pct num int enum bool
#   shown-when: None, ("wave", [...]), ("mode", [...]) or ("mod", [...]) - only a display filter
P = [
    # ----- basic
    ("wave", "Waveform", "enum", "{s}FUNCtion", "Basic", None, WAVES),
    ("arb", "Waveform number", "int", "{s}FUNCtion", "Basic", ("wave", ["ARB"]), (1, 166)),
    ("mode", "Mode", "enum", "{s}FUNCtion:MODE", "Basic", None, MODES),
    ("freq", "Frequency", "hz", "{s}FREQuency", "Basic", None, None),
    ("ampl", "Amplitude", "vpp", "{s}VOLTage", "Basic", None, None),
    ("offs", "Offset", "v", "{s}VOLTage:OFFSet", "Basic", None, None),
    ("phase", "Phase", "deg", "{s}PHASe", "Basic", None, None),
    ("duty", "Duty cycle", "pct", "{s}FUNCtion:SQUare:DCYCle", "Basic", ("wave", ["SQUare"]), None),
    ("symm", "Symmetry", "pct", "{s}FUNCtion:RAMP:SYMMetry", "Basic", ("wave", ["RAMP"]), None),
    ("pwidth", "Pulse width", "s", "{s}FUNCtion:PULSe:WIDTh", "Basic", ("wave", ["PULSe"]), None),
    ("prise", "Rising edge", "s", "{s}FUNCtion:PULSe:RISing", "Basic", ("wave", ["PULSe"]), None),
    ("pfall", "Falling edge", "s", "{s}FUNCtion:PULSe:FALLing", "Basic", ("wave", ["PULSe"]), None),
    # ----- output
    ("load", "Load", "enum", "{o}:LOAD:STYLe", "Output", None,
     [("50 Ω", "50"), ("Custom (value below)", "RES"), ("High-Z", "HRES")]),
    ("loadr", "Load value", "ohm", "{o}:LOAD", "Output", None, None),
    ("pol", "Polarity", "enum", "{o}:POLarity", "Output", None, [("Normal", "NORmal"), ("Inverted", "INVerted")]),
    ("sync", "Sync output", "bool", "{o}:SYNC", "Output", None, None),
    ("limhi", "High level limit", "v", "{s}VOLTage:LIMit:HIGH", "Output", None, None),
    ("limlo", "Low level limit", "v", "{s}VOLTage:LIMit:LOW", "Output", None, None),
    # ----- modulation
    ("modtype", "Modulation", "enum", "{s}MODulate:MODE", "Modulation", None, MOD_TYPES),
    ("modsrc", "Source", "enum", "{s}MODulate:SOURce", "Modulation", None, SRC_IE),
    ("modwave", "Modulating waveform", "enum", "{s}MODulate:FUNCtion", "Modulation",
     ("mod", ["AM", "FM", "PM", "PWM", "SUM"]), MOD_WAVES),
    ("am_f", "AM frequency", "hz", "{s}AM:INTernal:FREQuency", "Modulation", ("mod", ["AM"]), None),
    ("am_d", "AM depth", "pct", "{s}AM:DEPTh", "Modulation", ("mod", ["AM"]), None),
    ("am_dssc", "AM DSSC", "bool", "{s}AM:DSSC", "Modulation", ("mod", ["AM"]), None),
    ("fm_f", "FM frequency", "hz", "{s}FM:INTernal:FREQuency", "Modulation", ("mod", ["FM"]), None),
    ("fm_dev", "FM deviation", "hz", "{s}FM:DEViation", "Modulation", ("mod", ["FM"]), None),
    ("pm_f", "PM frequency", "hz", "{s}PM:INTernal:FREQuency", "Modulation", ("mod", ["PM"]), None),
    ("pm_dev", "PM deviation", "deg", "{s}PM:DEViation", "Modulation", ("mod", ["PM"]), None),
    ("pwm_f", "PWM frequency", "hz", "{s}PWM:INTernal:FREQuency", "Modulation", ("mod", ["PWM"]), None),
    ("pwm_dev", "PWM deviation", "pct", "{s}PWM:DEViation", "Modulation", ("mod", ["PWM"]), None),
    ("sum_f", "SUM frequency", "hz", "{s}SUM:INTernal:FREQuency", "Modulation", ("mod", ["SUM"]), None),
    ("sum_a", "SUM amplitude", "pct", "{s}SUM:AMPLitude", "Modulation", ("mod", ["SUM"]), None),
    ("fsk_f", "FSK hop frequency", "hz", "{s}FSKey:FREQuency", "Modulation", ("mod", ["FSK"]), None),
    ("fsk_r", "FSK rate", "hz", "{s}FSKey:INTernal:RATE", "Modulation", ("mod", ["FSK"]), None),
    ("nfsk_f1", "NFSK frequency 1", "hz", "{s}NFSKey:FREQuency1", "Modulation", ("mod", ["NFSK"]), None),
    ("nfsk_f2", "NFSK frequency 2", "hz", "{s}NFSKey:FREQuency2", "Modulation", ("mod", ["NFSK"]), None),
    ("nfsk_f3", "NFSK frequency 3", "hz", "{s}NFSKey:FREQuency3", "Modulation", ("mod", ["NFSK"]), None),
    ("nfsk_r", "NFSK rate", "hz", "{s}NFSKey:INTernal:RATE", "Modulation", ("mod", ["NFSK"]), None),
    ("fsk4_f1", "4FSK frequency 1", "hz", "{s}4FSKey:FREQuency1", "Modulation", ("mod", ["4FSK"]), None),
    ("fsk4_f2", "4FSK frequency 2", "hz", "{s}4FSKey:FREQuency2", "Modulation", ("mod", ["4FSK"]), None),
    ("fsk4_f3", "4FSK frequency 3", "hz", "{s}4FSKey:FREQuency3", "Modulation", ("mod", ["4FSK"]), None),
    ("fsk4_r", "4FSK rate", "hz", "{s}4FSKey:INTernal:RATE", "Modulation", ("mod", ["4FSK"]), None),
    ("psk_p", "PSK hop phase", "deg", "{s}PSK:PHASe", "Modulation", ("mod", ["PSK"]), None),
    ("psk_r", "PSK rate", "hz", "{s}PSK:INTernal:RATE", "Modulation", ("mod", ["PSK"]), None),
    ("npsk_p1", "NPSK phase 1", "deg", "{s}NPSK:PHASe1", "Modulation", ("mod", ["NPSK"]), None),
    ("npsk_p2", "NPSK phase 2", "deg", "{s}NPSK:PHASe2", "Modulation", ("mod", ["NPSK"]), None),
    ("npsk_p3", "NPSK phase 3", "deg", "{s}NPSK:PHASe3", "Modulation", ("mod", ["NPSK"]), None),
    ("npsk_r", "NPSK rate", "hz", "{s}NPSK:INTernal:RATE", "Modulation", ("mod", ["NPSK"]), None),
    ("psk4_p1", "4PSK phase 1", "deg", "{s}4PSK:PHASe1", "Modulation", ("mod", ["4PSK"]), None),
    ("psk4_p2", "4PSK phase 2", "deg", "{s}4PSK:PHASe2", "Modulation", ("mod", ["4PSK"]), None),
    ("psk4_p3", "4PSK phase 3", "deg", "{s}4PSK:PHASe3", "Modulation", ("mod", ["4PSK"]), None),
    ("psk4_r", "4PSK rate", "hz", "{s}4PSK:INTernal:RATE", "Modulation", ("mod", ["4PSK"]), None),
    ("ask_a", "ASK hop amplitude", "vpp", "{s}ASK:AMPLitude", "Modulation", ("mod", ["ASK"]), None),
    ("ask_r", "ASK rate", "hz", "{s}ASK:INTernal:RATE", "Modulation", ("mod", ["ASK"]), None),
    ("osk_r", "OSK rate", "hz", "{s}OSK:INTernal:RATE", "Modulation", ("mod", ["OSK"]), None),
    ("osk_t", "OSK oscillation time", "s", "{s}OSK:TIME", "Modulation", ("mod", ["OSK"]), None),
    # ----- sweep
    ("sw_type", "Sweep type", "enum", "{s}SWEep:SPACing", "Sweep", None,
     [("Linear", "LINear"), ("Logarithmic", "LOGarithmic"), ("List", "LIST")]),
    ("sw_start", "Start frequency", "hz", "{s}FREQuency:STARt", "Sweep", None, None),
    ("sw_stop", "Stop frequency", "hz", "{s}FREQuency:STOP", "Sweep", None, None),
    ("sw_mark", "Marker frequency", "hz", "{s}MARKer:FREQuency", "Sweep", None, None),
    ("sw_time", "Sweep time", "s", "{s}SWEep:TIME", "Sweep", None, None),
    ("sw_hold", "Hold time", "s", "{s}SWEep:HTIMe", "Sweep", None, None),
    ("sw_ret", "Return time", "s", "{s}SWEep:RTIMe", "Sweep", None, None),
    ("sw_int", "Interval", "s", "{s}SWEep:INTerval", "Sweep", None, None),
    ("sw_trig", "Trigger source", "enum", "{s}SWEep:TRIGger:SOURce", "Sweep", None, SRC_IEM),
    ("sw_tout", "Trigger output", "enum", "{s}SWEep:TRIGger:OUTPut", "Sweep", None, TRIG_OUT),
    # ----- burst
    ("b_mode", "Burst mode", "enum", "{s}BURSt:MODE", "Burst", None, [("Triggered (N cycles)", "TRIGgered"),
                                                                        ("Gated", "GATed")]),
    ("b_n", "Cycles", "int", "{s}BURSt:NCYCles", "Burst", None, (1, 100_000_000)),
    ("b_per", "Burst period", "s", "{s}BURSt:INTernal:PERiod", "Burst", None, None),
    ("b_phase", "Start phase", "deg", "{s}BURSt:PHASe", "Burst", None, None),
    ("b_delay", "Delay", "s", "{s}BURSt:DELay", "Burst", None, None),
    ("b_trig", "Trigger source", "enum", "{s}BURSt:TRIGger:SOURce", "Burst", None, SRC_IEM),
    ("b_slope", "Ext. trigger slope", "enum", "{s}BURSt:TRIGger:SLOPe", "Burst", None,
     [("Positive", "POSitive"), ("Negative", "NEGative")]),
    ("b_tout", "Trigger output", "enum", "{s}BURSt:TRIGger:OUTPut", "Burst", None, TRIG_OUT),
    # ----- double pulse
    ("d_w1", "First pulse width", "s", "{s}DOUBle:FIRSt:WIDTh", "Double pulse", None, None),
    ("d_w2", "Second pulse width", "s", "{s}DOUBle:SECond:WIDTh", "Double pulse", None, None),
    ("d_gap", "Pulse interval", "s", "{s}DOUBle:INTernal:TIMe", "Double pulse", None, None),
    ("d_pol", "Pulses", "enum", "{s}DOUBle:PULSe:STATe", "Double pulse", None,
     [("Positive", "POSitive"), ("Negative", "NEGative"), ("Positive + negative", "BOTH")]),
    # ----- output on/off last, so the signal is set up before it appears
    ("out", "Output", "bool", "{o}", "Output", None, None),
]
PARAMS = {p[0]: p for p in P}
GROUPS = ["Basic", "Modulation", "Sweep", "Burst", "Double pulse", "Output"]
MODE_GROUP = {"MODulate": "Modulation", "SWEep": "Sweep", "BURSt": "Burst", "DOUBle": "Double pulse"}
UNITS = {"hz": "Hz", "s": "s", "vpp": "Vpp", "v": "V", "ohm": "Ω", "bps": "bps", "deg": "°", "pct": "%"}


def short(mnemonic: str) -> str:
    """SCPI short form: upper-case letters and digits ('SINusoid' -> 'SIN', '4FSKey' -> '4FSK')."""
    m = re.match(r"[A-Z0-9]+", mnemonic)
    return m.group(0) if m else mnemonic.upper()


def match_choice(resp: str, choices) -> str | None:
    r = resp.strip().strip("'\"").upper()
    for _, val in choices:
        s, full = short(val), val.upper()
        if r in (s, full) or (len(r) >= min(3, len(s)) and full.startswith(r)) or r.startswith(s):
            return val
    return None


def parse_si(text: str) -> float:
    t = text.strip().replace(" ", "").replace(",", ".")
    for u in ("vpp", "hz", "ohm", "ω", "bps", "deg", "°", "%", "v", "s"):
        if t.lower().endswith(u) and len(t) > len(u):
            t = t[:-len(u)]
            break
    pre = {"G": 1e9, "M": 1e6, "k": 1e3, "K": 1e3, "m": 1e-3, "u": 1e-6, "µ": 1e-6, "n": 1e-9}
    if t and t[-1] in pre:
        return float(t[:-1]) * pre[t[-1]]
    return float(t)


def fmt_si(v, unit="") -> str:
    if v is None or not math.isfinite(v):
        return "—"
    if v == 0:
        return f"0 {unit}".strip()
    e = max(-9, min(9, int(math.floor(math.log10(abs(v)) / 3) * 3)))
    p = {9: "G", 6: "M", 3: "k", 0: "", -3: "m", -6: "u", -9: "n"}[e]
    return f"{v / 10 ** e:.15g} {p}{unit}".strip()


def scpi_value(kind: str, v, key="") -> str:
    """Value as sent: frequencies in Hz, times in s, amplitude in Vpp (units as listed in the guide)."""
    if kind == "bool":
        return "ON" if v else "OFF"
    if kind in ("enum",):
        return str(v)
    if kind == "int":
        return str(int(v))
    suffix = {"hz": "Hz", "s": "s", "vpp": "Vpp", "v": "V", "ohm": "Ohm"}.get(kind, "")
    return f"{float(v):.12g}{suffix}"


def describe(key, v) -> str:
    p = PARAMS[key]
    kind, choices = p[2], p[6]
    if kind == "enum":
        return next((lbl for lbl, val in choices if val == v), str(v))
    if kind == "bool":
        return "ON" if v else "OFF"
    if kind == "int":
        return str(int(v))
    if kind in ("deg", "pct", "num"):
        return f"{v:g}{UNITS.get(kind, '')}"
    return fmt_si(v, UNITS.get(kind, ""))


def same(a, b) -> bool:
    """Unchanged? Numbers equal within 1e-14 relative (text <-> number round trips are not exact)."""
    if isinstance(a, float) or isinstance(b, float):
        try:
            return math.isclose(float(a), float(b), rel_tol=1e-14, abs_tol=1e-15)
        except (TypeError, ValueError):
            return False
    return a == b


# --------------------------------------------------------------------------- #
#  Drivers                                                                     #
# --------------------------------------------------------------------------- #
class SocketLink:
    """LAN: raw SCPI over TCP (port 5025). Replies end with LF."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout=2.0):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.settimeout(timeout)
        self._buf = b""
        self.addr = f"LAN {host}:{port}"

    def write(self, c: str):
        self.sock.sendall(c.encode("ascii") + b"\n")

    def _readline(self) -> str:
        while b"\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("generator closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode("ascii", "replace").strip()

    def query(self, c: str) -> str:
        self._buf = b""
        self.write(c)
        try:
            return self._readline()
        except socket.timeout:
            self._drain()
            raise TimeoutError(f"no reply to {c}")

    def _drain(self):
        """After a timeout, swallow a late reply so the next query is not answered with it."""
        old = self.sock.gettimeout()
        self.sock.settimeout(0.3)
        try:
            while self.sock.recv(4096):
                pass
        except (socket.timeout, OSError):
            pass
        finally:
            self.sock.settimeout(old)
            self._buf = b""

    def close(self):
        self.sock.close()


class VisaLink:
    """USB (USB-TMC) or any other VISA resource string."""

    def __init__(self, resource: str, timeout_ms=2000):
        if pyvisa is None:
            raise RuntimeError("pyvisa is not installed:  pip install pyvisa pyvisa-py pyusb")
        try:
            self.inst = _rm().open_resource(resource)
        except pyvisa.errors.VisaIOError as e:
            if e.error_code != pyvisa.constants.StatusCode.error_resource_not_found:
                raise
            seen = [r for r, _ in find_usb()]
            raise ConnectionError(
                f"VISA has no device '{resource}'. "
                + (f"USB devices VISA sees: {', '.join(seen)} - pick one with ⟳."
                   if seen else "VISA sees no USB instrument at all: check the cable in the rear USB Device "
                                "port, the generator is on, and the USB driver (Keysight IO Libraries / "
                                "NI-VISA) - the generator must appear in Keysight Connection Expert."))
        self.inst.timeout = timeout_ms
        self.inst.read_termination = "\n"
        self.inst.write_termination = "\n"
        self.addr = resource

    def write(self, c: str):
        self.inst.write(c)

    def query(self, c: str) -> str:
        try:
            return self.inst.query(c).strip()
        except pyvisa.errors.VisaIOError as e:
            if e.error_code == pyvisa.constants.StatusCode.error_timeout:
                try:
                    self.inst.clear()            # drop a late reply (USB-TMC device clear)
                except Exception:
                    pass
                raise TimeoutError(f"no reply to {c}")
            raise ConnectionError(f"VISA: {e}")

    def close(self):
        self.inst.close()


def _rm():
    """The bench's VISA loader: installed VISA (Keysight / NI) first, pyvisa-py only as last resort."""
    try:
        from bench_monitor import visa_manager
        return visa_manager()
    except ImportError:
        try:
            return pyvisa.ResourceManager()
        except Exception:
            return pyvisa.ResourceManager("@py")       # pure-Python backend (needs pyusb for USB)


def find_usb() -> list[tuple[str, bool]]:
    """VISA USB resources as (resource, looks_like_TFG); TFGs first."""
    if pyvisa is None:
        return []
    try:
        res = [r for r in _rm().list_resources() if r.upper().startswith("USB")]
    except Exception:
        return []
    out = [(r, any(i in r.upper() for i in USB_IDS)) for r in res]
    return sorted(out, key=lambda x: not x[1])


class TfgDriver:
    """TFG6800 SCPI on top of a link (SocketLink for LAN, VisaLink for USB)."""

    def __init__(self, link):
        self.link = link
        self.addr = link.addr
        try:
            self.idn = self.q("*IDN?")
            self.write("*CLS")
            self.write("SOURce1:VOLTage:UNIT VPP")      # amplitudes are read and set in Vpp
            self.write("SOURce2:VOLTage:UNIT VPP")
            self.errors()
        except Exception:
            link.close()
            raise

    def write(self, c: str):
        self.link.write(c)

    def q(self, c: str) -> str:
        return self.link.query(c)

    def errors(self) -> list[str]:
        out = []
        for _ in range(20):
            e = self.q("SYSTem:ERRor?")
            if e.lstrip("+").startswith("0"):
                break
            out.append(e)
        return out

    def get(self, ch: int, key: str):
        p = PARAMS[key]
        kind, choices = p[2], p[6]
        if key == "arb":
            return None                              # FUNCtion? already tells the waveform
        r = self.q(_cmd(p[3], ch) + "?")
        if kind == "enum":
            if key == "wave":
                v = match_choice(r, WAVES[:5])
                return v or "ARB"
            return match_choice(r, choices)
        if kind == "bool":
            return r.strip().upper() in ("1", "ON")
        m = _NUM.search(r)
        if not m:
            return None
        v = float(m.group(0))
        return int(v) if kind == "int" else v

    def set(self, ch: int, key: str, v):
        p = PARAMS[key]
        if key == "wave" and v == "ARB":
            return                                  # the number is sent with key "arb"
        self.write(f"{_cmd(p[3], ch)} {scpi_value(p[2], v, key)}")

    def raw(self, c: str) -> str:
        if "?" in c:
            return self.q(c)
        self.write(c)
        return ""

    def close(self):
        try:
            self.write("SYSTem:LOCAl")
        except Exception:
            pass
        try:
            self.link.close()
        except Exception:
            pass


def _cmd(template: str, ch: int) -> str:
    return template.format(s=f"SOURce{ch}:", o=f"OUTPut{ch}")


class DemoTfg:
    idn = "Suin,TFG6806,DEMO,simulated"
    addr = "demo"

    def __init__(self):
        base = {"wave": "SINusoid", "arb": 1, "mode": "CONTinuous", "freq": 1e3, "ampl": 1.0, "offs": 0.0,
                "phase": 0.0, "duty": 50.0, "symm": 50.0, "pwidth": 500e-6, "prise": 20e-9, "pfall": 20e-9,
                "load": "HRES", "loadr": 50.0, "pol": "NORmal", "sync": False, "limhi": 10.0, "limlo": -10.0,
                "modtype": "AM", "modsrc": "INTernal", "modwave": "SINusoid", "am_f": 100.0, "am_d": 100.0,
                "am_dssc": False, "sw_type": "LINear", "sw_start": 100.0, "sw_stop": 1e3, "sw_mark": 550.0,
                "sw_time": 1.0, "sw_hold": 0.0, "sw_ret": 0.0, "sw_int": 0.0, "sw_trig": "INTernal",
                "sw_tout": "OFF", "b_mode": "TRIGgered", "b_n": 1, "b_per": 10e-3, "b_phase": 0.0,
                "b_delay": 0.0, "b_trig": "INTernal", "b_slope": "POSitive", "b_tout": "OFF",
                "d_w1": 1e-6, "d_w2": 1e-6, "d_gap": 1e-6, "d_pol": "POSitive", "out": False}
        self.s = {ch: dict(base) for ch in CHANNELS}
        self.log = []

    def get(self, ch, key):
        time.sleep(0.002)
        return self.s[ch].get(key, 0.0 if PARAMS[key][2] not in ("enum", "bool") else None)

    def set(self, ch, key, v):
        self.s[ch][key] = v
        self.log.append(f"{_cmd(PARAMS[key][3], ch)} {scpi_value(PARAMS[key][2], v)}")

    def errors(self):
        return []

    def raw(self, c):
        self.log.append(c)
        return "Suin,TFG6806,DEMO,simulated" if c.strip().upper() == "*IDN?" else ("0,No error" if "?" in c else "")

    def write(self, c):
        self.log.append(c)

    def close(self):
        pass


# --------------------------------------------------------------------------- #
#  Worker                                                                      #
# --------------------------------------------------------------------------- #
def keys_to_read(state: dict | None) -> list[str]:
    """Basic + output, plus the group of the channel's current mode (keeps a refresh short)."""
    keys = [k for k, p in PARAMS.items() if p[4] in ("Basic", "Output") and k != "arb"]
    mode = (state or {}).get("mode")
    grp = MODE_GROUP.get(mode)
    if grp:
        keys += [k for k, p in PARAMS.items() if p[4] == grp]
    return keys


class TfgWorker(QtCore.QThread):
    log = QtCore.pyqtSignal(str, str)
    connected = QtCore.pyqtSignal(bool, str)
    state = QtCore.pyqtSignal(int, object, bool)    # channel, {key: value}, partial
    console = QtCore.pyqtSignal(str, str)           # command, reply

    def __init__(self):
        super().__init__()
        self.jobs: queue.Queue = queue.Queue()
        self.dev = None
        self._quit = False
        self.last = {ch: {} for ch in CHANNELS}

    def submit(self, fn):
        self.jobs.put(fn)

    def run(self):
        while not self._quit:
            try:
                fn = self.jobs.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                fn()
            except Exception as e:
                self.log.emit(f"TFG6806: {type(e).__name__}: {e}", "err")
                if isinstance(e, (ConnectionError, OSError)) and not isinstance(e, TimeoutError):
                    self._lost(e)

    def _lost(self, e):
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
        self.dev = None
        self.connected.emit(False, f"connection lost: {e}")

    def shutdown(self):
        self._quit = True
        self.wait(3000)
        if self.dev:
            self.dev.close()

    # ---- jobs
    def connect_dev(self, demo, iface, addr, port=DEFAULT_PORT):
        """iface 'lan' (addr = IP) or 'usb' (addr = VISA resource)."""
        if self.dev:
            self.dev.close()
        try:
            if demo:
                self.dev = DemoTfg()
            else:
                link = SocketLink(addr, port) if iface == "lan" else VisaLink(addr)
                self.dev = TfgDriver(link)
        except Exception as e:
            self.dev = None
            self.connected.emit(False, f"{type(e).__name__}: {e}")
            return
        self.connected.emit(True, f"{self.dev.idn}  ({self.dev.addr})")
        for ch in CHANNELS:
            self.read(ch)

    def disconnect_dev(self):
        if self.dev:
            self.dev.close()
        self.dev = None
        self.connected.emit(False, "")

    def read(self, ch, keys=None, keep_mode=False):
        if self.dev is None:
            return
        st = dict(self.last[ch])
        st["mode"] = self.dev.get(ch, "mode")
        failed = []
        for k in keys or keys_to_read(st):
            try:
                v = self.dev.get(ch, k)
            except TimeoutError:
                failed.append(k)
                continue
            if v is not None:
                st[k] = v
        self.dev.errors()                            # clear "not applicable" errors of the queries
        self.last[ch] = st
        shown = {k: st[k] for k in (keys or []) if k in st} if keep_mode else dict(st)
        self.state.emit(ch, shown, keep_mode)
        if failed:
            self.log.emit(f"TFG6806 CH{ch}: no reply for {', '.join(PARAMS[k][1] for k in failed)}", "warn")

    def apply(self, ch: int, s: dict, prev=None, label="Applied"):
        """Send the keys of `s` that differ from `prev` (default: last read state), in table order.
        prev={} sends everything in `s`."""
        if self.dev is None:
            raise RuntimeError("TFG6806 not connected")
        ref = self.last[ch] if prev is None else prev
        sent, errors = [], []
        for key in PARAMS:                         # table order: waveform, mode ... output last
            if key not in s or s[key] is None:
                continue
            if key == "arb" and s.get("wave", ref.get("wave")) != "ARB":
                continue
            if key != "arb" and same(ref.get(key), s[key]):
                continue
            self.dev.set(ch, key, s[key])
            sent.append(f"{PARAMS[key][1]} {describe(key, s[key])}")
            for e in self.dev.errors():            # report each error next to the command that caused it
                errors.append(f"{PARAMS[key][1]}: {e}")
        for e in errors:
            self.log.emit(f"TFG6806 CH{ch} {e}", "err")
        if sent:
            self.log.emit(f"TFG6806 CH{ch} {label}: " + "; ".join(sent), "warn" if errors else "ok")
        self.read(ch)
        if errors:
            raise RuntimeError(f"{len(errors)} SCPI error(s): " + " | ".join(errors))

    def command(self, c: str, label=None):
        """System command or console line; returns the reply (queries) and reports SCPI errors."""
        if self.dev is None:
            raise RuntimeError("TFG6806 not connected")
        reply = self.dev.raw(c)
        errs = self.dev.errors() if not c.strip().upper().startswith("SYST") else []
        self.console.emit(c, reply)
        if label:
            self.log.emit(f"TFG6806 {label}", "ok" if not errs else "warn")
        for e in errs:
            self.log.emit(f"TFG6806 '{c}': {e}", "err")
        if errs:
            raise RuntimeError("; ".join(errs))
        return reply


# --------------------------------------------------------------------------- #
#  Field widgets (also used by the sequence step editor)                       #
# --------------------------------------------------------------------------- #
class Field:
    """Editor for one parameter: .w (widget), get(), put(v)."""

    def __init__(self, key):
        self.key = key
        _, label, kind, _, _, _, choices = PARAMS[key]
        self.kind = kind
        if kind == "enum":
            self.w = QtWidgets.QComboBox()
            for lbl, val in choices:
                self.w.addItem(lbl, val)
        elif kind == "bool":
            self.w = QtWidgets.QCheckBox("on")
        elif kind == "int":
            self.w = QtWidgets.QSpinBox()
            lo, hi = choices or (0, 1_000_000_000)
            self.w.setRange(lo, hi)
        elif kind in ("deg", "pct"):
            self.w = QtWidgets.QDoubleSpinBox()
            self.w.setDecimals(4 if kind == "pct" else 2)
            self.w.setRange(-360 if kind == "deg" else 0, 360 if kind == "deg" else 120)
            self.w.setSuffix(" °" if kind == "deg" else " %")
        else:
            self.w = QtWidgets.QLineEdit()
            self.w.setPlaceholderText({"hz": "e.g. 10k, 1.5M", "s": "e.g. 10m, 200n", "vpp": "e.g. 1.2",
                                       "v": "e.g. -0.5", "ohm": "e.g. 50"}.get(kind, ""))
            self.w.setToolTip(f"Unit {UNITS.get(kind, '')}; SI prefixes allowed (n u m k M)")
        if isinstance(self.w, (QtWidgets.QAbstractSpinBox,)):
            self.w.setKeyboardTracking(False)

    def get(self):
        if self.kind == "enum":
            return self.w.currentData()
        if self.kind == "bool":
            return self.w.isChecked()
        if self.kind in ("int", "deg", "pct"):
            return self.w.value()
        t = self.w.text().strip()
        return parse_si(t) if t and t != "—" else None       # empty = not known yet, not sent

    def put(self, v):
        if v is None:
            return
        self.w.blockSignals(True)
        if self.kind == "enum":
            i = self.w.findData(v)
            if i >= 0:
                self.w.setCurrentIndex(i)
        elif self.kind == "bool":
            self.w.setChecked(bool(v))
        elif self.kind in ("int", "deg", "pct"):
            self.w.setValue(v)
        else:
            self.w.setText(fmt_si(v, UNITS.get(self.kind, "")))
        self.w.blockSignals(False)


# --------------------------------------------------------------------------- #
#  Tab                                                                         #
# --------------------------------------------------------------------------- #
class ChannelPanel(QtWidgets.QWidget):
    apply_req = QtCore.pyqtSignal(int, object)       # channel, {key: value}
    out_req = QtCore.pyqtSignal(int, bool)
    read_req = QtCore.pyqtSignal(int)
    read_keys = QtCore.pyqtSignal(int, object)       # channel, keys of a section just shown

    def __init__(self, ch):
        super().__init__()
        self.ch = ch
        self.fields: dict[str, Field] = {}
        self.rows: dict[str, tuple] = {}
        self.state: dict = {}
        self.dirty: set[str] = set()                 # edited here since the last full read
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.out_btn = QtWidgets.QPushButton()
        self.out_btn.setCheckable(True)
        self.out_btn.setMinimumHeight(38)
        self.out_btn.clicked.connect(lambda on: self.out_req.emit(self.ch, on))
        self._show_out(False)
        v.addWidget(self.out_btn)
        self.groups = {}
        for gname in GROUPS:
            g = QtWidgets.QGroupBox(f"CH{ch} · {gname}")
            grid = QtWidgets.QGridLayout(g)
            grid.setVerticalSpacing(3)
            r = 0
            for key, p in PARAMS.items():
                if p[4] != gname or key == "out":
                    continue
                f = Field(key)
                lbl = QtWidgets.QLabel(p[1])
                grid.addWidget(lbl, r, 0)
                grid.addWidget(f.w, r, 1)
                self.fields[key] = f
                self.rows[key] = (lbl, f.w)
                sig = {"enum": "currentIndexChanged", "bool": "toggled"}.get(
                    f.kind, "valueChanged" if f.kind in ("int", "deg", "pct") else "textEdited")
                getattr(f.w, sig).connect(lambda *_, k=key: self.dirty.add(k))
                r += 1
            grid.setColumnStretch(1, 1)
            self.groups[gname] = g
            v.addWidget(g)
        for key in ("wave", "mode", "modtype", "load"):
            self.fields[key].w.currentIndexChanged.connect(self._visibility)
        self.fields["mode"].w.currentIndexChanged.connect(self._mode_picked)
        bar = QtWidgets.QHBoxLayout()
        self.apply_btn = QtWidgets.QPushButton(f"Apply CH{ch}")
        self.apply_btn.setToolTip("Send the fields that differ from the generator's current settings")
        self.apply_btn.clicked.connect(self._apply)
        self.read_btn = QtWidgets.QPushButton("Read")
        self.read_btn.clicked.connect(lambda: self.read_req.emit(self.ch))
        bar.addWidget(self.apply_btn, 1)
        bar.addWidget(self.read_btn)
        v.addLayout(bar)
        v.addStretch(1)
        self._visibility()

    def _mode_picked(self, *_):
        """Mode chosen in the panel: fetch that section's current values from the generator."""
        grp = MODE_GROUP.get(self.fields["mode"].get())
        if grp:
            self.read_keys.emit(self.ch, [k for k, p in PARAMS.items() if p[4] == grp])

    def _show_out(self, on):
        self.out_btn.setChecked(on)
        self.out_btn.setText(f"CH{self.ch} OUTPUT: {'ON' if on else 'OFF'}")
        self.out_btn.setStyleSheet("background:#8b1d1d;border-color:#f85149;font-weight:700;"
                                   if on else "font-weight:700;")

    def _visibility(self, *_):
        cur = {"wave": self.fields["wave"].get(), "mode": self.fields["mode"].get(),
               "mod": self.fields["modtype"].get()}
        for key, (lbl, w) in self.rows.items():
            cond = PARAMS[key][5]
            vis = cond is None or cur.get(cond[0]) in cond[1]
            if key == "loadr":
                vis = self.fields["load"].get() == "RES"
            lbl.setVisible(vis)
            w.setVisible(vis)
        mode_grp = MODE_GROUP.get(cur["mode"])
        for gname in ("Modulation", "Sweep", "Burst", "Double pulse"):
            self.groups[gname].setVisible(gname == mode_grp)

    def put_state(self, st: dict, partial=False):
        """partial: a section fetched while the user is editing - leave edited fields alone."""
        self.state.update(st)
        if not partial:
            self.dirty.clear()
        for key, f in self.fields.items():
            if key in st and not (partial and key in self.dirty):
                f.put(st[key])
        if "out" in st:
            self._show_out(bool(st["out"]))
        self._visibility()

    def collect(self) -> dict:
        """Values of the visible fields (hidden ones belong to other waveforms / modes)."""
        out = {}
        for key, f in self.fields.items():
            if f.w.isVisibleTo(self):
                v = f.get()
                if v is not None:
                    out[key] = v
        return out

    def _apply(self):
        try:
            s = self.collect()
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "TFG6806", f"Could not read a field: {e}")
            return
        self.apply_req.emit(self.ch, s)


class TfgTab(QtWidgets.QWidget):
    log = QtCore.pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.is_connected = False
        self.idn = ""
        self.worker = TfgWorker()
        self.worker.log.connect(self.log)
        self.worker.connected.connect(self._on_connected)
        self.worker.state.connect(self._on_state)
        self.worker.console.connect(self._on_console)
        self.worker.start()
        self._build()
        self._set_enabled(False)
        self._iface_changed()

    @property
    def out_on(self) -> dict:
        return {ch: bool(self.worker.last[ch].get("out")) for ch in CHANNELS}

    def _build(self):
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        left = QtWidgets.QVBoxLayout()
        g = QtWidgets.QGroupBox("Connection")
        f = QtWidgets.QFormLayout(g)
        self.iface = QtWidgets.QComboBox()
        self.iface.addItem("LAN (Ethernet)", "lan")
        self.iface.addItem("USB (rear USB Device port)", "usb")
        self.iface.currentIndexChanged.connect(self._iface_changed)
        self.host = QtWidgets.QLineEdit()
        self.host.setPlaceholderText("IP address, e.g. 192.168.1.60")
        self.port = QtWidgets.QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(DEFAULT_PORT)
        self.usb = QtWidgets.QComboBox()
        self.usb.setEditable(True)
        self.usb.setMinimumContentsLength(18)
        self.usb.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.usb.lineEdit().setPlaceholderText("USB0::1267::260::…::INSTR")
        self.usb.setToolTip("VISA address of the generator. ⟳ lists USB instruments; ★ = looks like a TFG.\n"
                            "Needs a VISA library (Keysight IO Libraries / NI-VISA) or pyvisa-py + pyusb.")
        self.usb_find = QtWidgets.QPushButton("⟳")
        self.usb_find.setFixedWidth(34)
        self.usb_find.setToolTip("Search USB instruments")
        self.usb_find.clicked.connect(self.on_find_usb)
        usb_row = QtWidgets.QHBoxLayout()
        usb_row.addWidget(self.usb, 1)
        usb_row.addWidget(self.usb_find)
        self.demo = QtWidgets.QCheckBox("Demo (no instrument)")
        self.conn_btn = QtWidgets.QPushButton("Connect")
        self.conn_btn.clicked.connect(self.on_connect)
        self.status = QtWidgets.QLabel("not connected")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#8a96a3;")
        self.usb_w = QtWidgets.QWidget()
        self.usb_w.setLayout(usb_row)
        usb_row.setContentsMargins(0, 0, 0, 0)
        self._rows = {}
        f.addRow("Interface", self.iface)
        for key, text, w in (("lan", "IP address", self.host), ("lan2", "Port", self.port),
                             ("usb", "Device", self.usb_w)):
            lbl = QtWidgets.QLabel(text)
            f.addRow(lbl, w)
            self._rows[key] = (lbl, w)
        f.addRow(self.demo)
        f.addRow(self.conn_btn)
        f.addRow(self.status)
        left.addWidget(g)

        self.g_sys = g = QtWidgets.QGroupBox("Instrument")
        grid = QtWidgets.QGridLayout(g)
        btns = [("Trigger (*TRG)", lambda: self._cmd("*TRG", "manual trigger sent")),
                ("Align phases", lambda: self._cmd("SOURce:PHASe:ALIgn", "phases of CH1 and CH2 aligned")),
                ("Read all", self.on_read_all),
                ("Reset (*RST)", self.on_reset)]
        for k, (text, fn) in enumerate(btns):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            grid.addWidget(b, k // 2, k % 2)
        self.slot = QtWidgets.QSpinBox()
        self.slot.setRange(0, 4)
        sv = QtWidgets.QPushButton("Save state")
        sv.clicked.connect(lambda: self._cmd(f"*SAV {self.slot.value()}", f"state saved to slot {self.slot.value()}"))
        rc = QtWidgets.QPushButton("Recall state")
        rc.clicked.connect(self.on_recall)
        grid.addWidget(QtWidgets.QLabel("Slot"), 2, 0)
        grid.addWidget(self.slot, 2, 1)
        grid.addWidget(sv, 3, 0)
        grid.addWidget(rc, 3, 1)
        left.addWidget(g)

        self.g_con = g = QtWidgets.QGroupBox("SCPI console")
        cv = QtWidgets.QVBoxLayout(g)
        self.con_in = QtWidgets.QLineEdit()
        self.con_in.setPlaceholderText("e.g. COUPle1:FREQuency:STATe ON   or   SOURce1:FREQuency?")
        self.con_in.returnPressed.connect(self.on_console)
        self.con_out = QtWidgets.QPlainTextEdit()
        self.con_out.setReadOnly(True)
        self.con_out.setMaximumBlockCount(500)
        send = QtWidgets.QPushButton("Send")
        send.clicked.connect(self.on_console)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.con_in, 1)
        row.addWidget(send)
        cv.addLayout(row)
        cv.addWidget(self.con_out, 1)
        note = QtWidgets.QLabel("Commands from the TFG6800 Programmer's Guide; a '?' command shows the reply. "
                                "Errors from SYSTem:ERRor? appear in the log.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#8a96a3;font-size:11px;")
        cv.addWidget(note)
        left.addWidget(g, 1)
        lw = QtWidgets.QWidget()
        lw.setLayout(left)
        lw.setMaximumWidth(380)
        root.addWidget(lw)

        self.panels = {}
        for ch in CHANNELS:
            p = ChannelPanel(ch)
            p.apply_req.connect(self.on_apply)
            p.out_req.connect(self.on_output)
            p.read_req.connect(lambda ch: self.worker.submit(lambda: self.worker.read(ch)))
            p.read_keys.connect(self._read_keys)
            self.panels[ch] = p
            sc = QtWidgets.QScrollArea()
            sc.setWidget(p)
            sc.setWidgetResizable(True)
            sc.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            sc.setMinimumWidth(330)
            root.addWidget(sc, 1)

    def _set_enabled(self, on):
        for w in list(self.panels.values()) + [self.g_sys, self.g_con]:
            w.setEnabled(on)
        for w in (self.iface, self.host, self.port, self.usb_w, self.demo):
            w.setEnabled(not on)
        self.conn_btn.setText("Disconnect" if on else "Connect")

    # ---------------------------------------------------------------- actions
    def _iface_changed(self, *_):
        usb = self.iface.currentData() == "usb"
        for key, (lbl, w) in self._rows.items():
            show = usb if key == "usb" else not usb
            lbl.setVisible(show)
            w.setVisible(show)
        if usb and self.usb.count() == 0 and self.isVisible():
            self.on_find_usb()

    def on_find_usb(self):
        found = find_usb()
        cur = self.usb.currentText()
        self.usb.clear()
        for res, tfg in found:
            self.usb.addItem(("★ " if tfg else "") + res, res)
        if cur:
            self.usb.setEditText(cur)
        elif found:
            self.usb.setCurrentIndex(0)
        if pyvisa is None:
            self.status.setText("USB needs pyvisa:  pip install pyvisa pyvisa-py pyusb")
        elif not found:
            self.status.setText("no USB instrument found - cable in the rear USB Device port? VISA installed?")

    def _usb_resource(self) -> str:
        i = self.usb.currentIndex()
        txt = self.usb.currentText().strip()
        if i >= 0 and self.usb.itemText(i) == txt:
            return self.usb.itemData(i) or txt.lstrip("★ ").strip()
        return txt.lstrip("★ ").strip()

    def on_connect(self):
        if self.is_connected:
            self.worker.submit(self.worker.disconnect_dev)
            return
        demo, iface = self.demo.isChecked(), self.iface.currentData()
        addr = self.host.text().strip() if iface == "lan" else self._usb_resource()
        port = self.port.value()
        if not demo and not addr:
            self.log.emit("TFG6806: enter the generator's IP address (Utility -> LAN on the generator)"
                          if iface == "lan" else "TFG6806: choose the USB device (press ⟳ to search)", "warn")
            return
        self.status.setText("connecting…")
        self.conn_btn.setEnabled(False)
        self.worker.submit(lambda: self.worker.connect_dev(demo, iface, addr, port))

    def _on_connected(self, ok, info):
        self.conn_btn.setEnabled(True)
        self.is_connected = ok
        self.idn = info if ok else ""
        self._set_enabled(ok)
        self.status.setText(f"✓ {info}" if ok else (info or "not connected"))
        self.status.setStyleSheet(f"color:{'#4fd18b' if ok else ('#ff6b6b' if info else '#8a96a3')};")
        if ok:
            self.log.emit(f"TFG6806 connected: {info}", "ok")
        elif info:
            self.log.emit(f"TFG6806: {info}", "err")

    def _on_state(self, ch, st, partial):
        self.panels[ch].put_state(st, partial)

    def on_apply(self, ch, s):
        self.worker.submit(lambda: self.worker.apply(ch, s))

    def on_output(self, ch, on):
        self.panels[ch]._show_out(not on)      # real state shown when the generator confirms
        self.worker.submit(lambda: self.worker.apply(ch, {"out": on}, label="output"))

    def _read_keys(self, ch, keys):
        if self.is_connected:
            self.worker.submit(lambda: self.worker.read(ch, keys, keep_mode=True))

    def on_read_all(self):
        for ch in CHANNELS:
            self.worker.submit(lambda ch=ch: self.worker.read(ch))

    def _cmd(self, c, label):
        def job():
            self.worker.command(c, label)
            if c.startswith(("*RST", "*RCL")):
                for ch in CHANNELS:
                    self.worker.read(ch)
        self.worker.submit(job)

    def on_reset(self):
        if QtWidgets.QMessageBox.question(self, "TFG6806", "Reset the generator to factory defaults (*RST)?") \
                == QtWidgets.QMessageBox.Yes:
            self._cmd("*RST", "reset (*RST)")

    def on_recall(self):
        self._cmd(f"*RCL {self.slot.value()}", f"state recalled from slot {self.slot.value()}")

    def on_console(self):
        c = self.con_in.text().strip()
        if not c:
            return
        self.worker.submit(lambda: self.worker.command(c))

    def _on_console(self, c, reply):
        self.con_out.appendPlainText(f"> {c}" + (f"\n{reply}" if reply else ""))

    def shutdown(self):
        self.worker.shutdown()
