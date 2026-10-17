# -*- coding: utf-8 -*-
"""
telemetry.py
============
"Telemetry" tab for fsw_vsa_monitor.py: decodes the telemetry arrays (MD) that
the units send over CAN. Frames come from the CAN tab (canable_analyzer.py), so
live capture and opened candump logs are both decoded.

Transport (seen on the bus, e.g. UPNG IOK OSN):
    29-bit ID  0x1A86 SS TT
        TT = array type (MD id): FC IOK OSN, FB IOK DOP, FA IOK SOST, F9 IOC CONFIG, F8 IOC FAILURE
        SS = segment: bits 7-6 = 00 first / 01 middle / 10 last, bits 5-0 = segment index
             (00, 41, 42 ... 48, 89  ->  10 x 8 bytes = 80 bytes)
    Array: [0] marker 0x04, [1] type, [2..3] length (little-endian, without these 4 bytes), ...
    Multi-byte values are little-endian (length 4C 00 = 76 = 80 - 4).
"""

from __future__ import annotations

import datetime as _dt

from PyQt5 import QtCore, QtGui, QtWidgets

# --------------------------------------------------------------------------- #
#  Arrays                                                                      #
# --------------------------------------------------------------------------- #
MD_ARRAYS = [  # type, name, size
    (0xFC, "IOK OSN", 80),
    (0xFB, "IOK DOP", 128),
    (0xFA, "IOK SOST", 48),
    (0xF9, "IOC CONFIG", 40),
    (0xF8, "IOC FAILURE", 16),
]
BOS_ARRAYS = [  # MD_BOS table
    (0xFC, "IOK OSN", 104),
    (0xFA, "IOK CONFIG", 144),
    (0xFD, "IOK SOST", 48),
    (0xFB, "IOK DOP", 32),          # MD_BOS table says 16 B; the unit sends Length 28 = 32 B
    (0xF9, "IOK ARCHIVE", 272),
]
# unit, CAN ID prefix (bits 28-16 of the 29-bit ID), arrays. Arrays are routed by
# prefix AND type: the BOS and the UPNG both send type 0xFC.
UNITS = [
    ("BOS", 0x1A46, BOS_ARRAYS),
    ("UPNG", 0x1A86, MD_ARRAYS),
    ("AFAR", None, []),
]
MD_TYPES = {t for _, _, arrays in UNITS for t, _, _ in arrays}


class Reassembler:
    """Collects the 8-byte segments of an array; returns complete arrays."""

    def __init__(self):
        self.parts: dict[int, dict[int, bytes]] = {}     # base id -> {index: data}
        self.errors = 0

    def feed(self, f):
        """f: canable_analyzer.Frame. Returns (base_id, type, data, ts) or None."""
        if not f.ext or f.rtr or (f.can_id & 0xFF) not in MD_TYPES:
            return None
        base = f.can_id & ~0xFF00
        seg = (f.can_id >> 8) & 0xFF
        kind, idx = seg >> 6, seg & 0x3F
        if kind == 0:                                  # first segment: start a new array
            if base in self.parts:
                self.errors += 1                       # previous one never completed
            self.parts[base] = {idx: f.data}
            return None
        buf = self.parts.get(base)
        if buf is None:
            return None                                # started before the capture began
        buf[idx] = f.data
        if kind != 2:
            return None
        del self.parts[base]
        if sorted(buf) != list(range(idx + 1)):
            self.errors += 1                           # a segment is missing
            return None
        return base, f.can_id & 0xFF, b"".join(buf[i] for i in range(idx + 1)), f.ts


# --------------------------------------------------------------------------- #
#  Field description                                                           #
# --------------------------------------------------------------------------- #
class Field:
    """byte = first byte, size = bytes (multi-byte, little-endian) or bits=(hi, lo)
    inside one byte. kind: u / i (signed) / bits / ang (INT8, 0.2 deg, 0x80 = none)
    / mask (channel bits) / res (reserve)."""

    def __init__(self, byte, name, desc, size=1, bits=None, kind="u", enum=None, expect=None,
                 compare=None, limit=None):
        self.byte, self.name, self.desc = byte, name, desc
        self.limit = limit              # warn when abs(value) > limit
        self.compare = compare          # (byte, name): value must equal that field (CRC check)
        self.size, self.bits, self.kind, self.enum, self.expect = size, bits, kind, enum or {}, expect

    def pos(self) -> tuple[str, str]:
        b = str(self.byte) if self.size == 1 else f"{self.byte}..{self.byte + self.size - 1}"
        if self.bits:
            hi, lo = self.bits
            return b, (str(hi) if hi == lo else f"{hi}-{lo}")
        return b, f"{8 * self.size - 1}-0"

    def raw(self, data: bytes) -> int | None:
        if self.byte + self.size > len(data):
            return None
        v = int.from_bytes(data[self.byte:self.byte + self.size], "little")
        if self.bits:
            hi, lo = self.bits
            v = (v >> lo) & ((1 << (hi - lo + 1)) - 1)
        if self.kind in ("i", "ang", "temp", "talarm"):
            n = 8 * self.size
            if v >= 1 << (n - 1):
                v -= 1 << n
        return v

    def value_str(self, v: int) -> str:
        if self.bits:
            w = self.bits[0] - self.bits[1] + 1
            return f"0b{v:0{w}b}"
        if self.kind in ("ang", "byte"):
            return f"0x{v & 0xFF:02X}"
        if self.kind in ("i", "temp", "talarm"):
            return f"{v}"
        return f"0x{v:0{2 * self.size}X}  ({v})" if self.size > 1 or v > 9 else f"0x{v:02X}"

    def meaning(self, v: int) -> tuple[str, bool]:
        """(text, warn)"""
        if self.kind == "ang":
            return ("no angle (0x80)", False) if v == -128 else (f"{v * 0.2:+.1f}°", False)
        if self.kind == "temp":
            return f"{v} °C", False
        if self.kind == "talarm":
            return ("not triggered (initial -128)", False) if v == -128 else (f"{v} °C at shutdown", True)
        if self.kind == "trig":
            hit = [str(i + 1) for i in range(4) if v >> i & 1]
            return (("TRIGGERED: sensor " + ", ".join(hit)), True) if hit else ("normal", False)
        if self.kind == "ms10":
            ms = v * 10
            return f"{ms // 3600000} h {ms // 60000 % 60:02d} min {ms // 1000 % 60:02d}.{ms % 1000:03d} s", False
        if self.kind == "fix3":     # bit 15 sign, 14..3 integer, 2..0 fraction; unit mV
            mv = (v & 0x7FFF) / 8 * (-1 if v & 0x8000 else 1)
            return f"{mv / 1000:.4f} V  ({mv:.3f} mV)", False
        if self.kind == "period":
            return ("disabled", False) if v == 0 else (f"every {v} s", False)
        if self.limit is not None and abs(v) > self.limit:
            return f"outside ±{self.limit}", True
        if self.kind == "byte":
            return (f"{v:3d}   '{chr(v)}'" if 32 <= v < 127 else f"{v:3d}"), False
        if self.kind == "dur":
            return f"{v // 3600} h {v % 3600 // 60:02d} min {v % 60:02d} s  ({v / 86400:.1f} days)", False
        if self.kind == "mask":
            on = [str(i + 1) for i in range(self.bits[0] - self.bits[1] + 1) if v >> i & 1]
            return (("channels " + ", ".join(on) + " on") if on else "all off"), False
        if self.kind == "res":
            return ("" if v == 0 else "reserve bits not 0"), v != 0
        if self.expect is not None and v != self.expect:
            return f"expected 0x{self.expect:02X}", True
        if self.enum:
            if v in self.enum:
                return self.enum[v], False
            return "undefined value", True
        return "", False


def _cnt(byte, name, desc, size=2):
    return Field(byte, name, desc, size=size)


IOK_OSN = [
    Field(0, "Marker", "Start of file marker", expect=0x04),
    Field(1, "Type", "Array type: IOK OSN", expect=0xFC),
    Field(2, "Length", "Packet length in bytes excluding marker, type, length", size=2, expect=76),
    Field(4, "AT", "Sender's address"),
    Field(5, "TLM_EXP", "TLM output frame format version"),
    _cnt(6, "TLM_CNT", "Cyclic counter of TLM frames of this type since power-on / reset (0 after 0xFFFF)"),
    Field(8, "Sts_mode", "Operating mode of the UPNG (from MD CONFIG or commands)", bits=(7, 4),
          enum={1: "JR mode", 2: "OZhR mode", 3: "SNR mode", 4: "OZhR/SNR mode"}),
    Field(8, "Sts_RF_Chnl", "Receiving channel status, bit 0..3 = channel 1..4 (1 = enabled)",
          bits=(3, 0), kind="mask"),
    Field(9, "Sts_wide_band", "Band used", bits=(7, 7), enum={0: "narrow band", 1: "wide band operation"}),
    Field(9, "Sts_Config", "Source of settings", bits=(6, 6),
          enum={0: "default (factory) parameters", 1: "from the MD CONFIG file"}),
    Field(9, "Sts_Mod_Idx", "Modulation index (from 'Set Modulation Index')", bits=(5, 4),
          enum={1: "±60°", 2: "±90°"}),
    Field(9, "Sts_LVDS", "Operating mode for the IPPD-KPI", bits=(3, 2),
          enum={1: "TC only via line 1", 2: "TC only via line 2", 3: "TC on both lines"}),
    Field(9, "Sts_CAN", "IU operating mode", bits=(1, 0),
          enum={1: "via bus IU1", 2: "via bus IU2", 3: "on both buses"}),
    Field(10, "Sts_freq", "Letter number, from 'Set reception frequency' (0x0000…0x063E)", size=2),
    Field(12, "Sts_bitrate", "Data transfer rate (from 'Set bit rate')", bits=(7, 6),
          enum={0: "carrier reception", 1: "8 kbps", 2: "16 kbps"}),
    Field(12, "reserve", "reserve", bits=(5, 4), kind="res"),
    Field(12, "ADC_valid", "ADC health per channel, bit 0..3 = channel 1..4 (1 = ADC OK, 0 = faulty)",
          bits=(3, 0), kind="mask"),
    Field(13, "Sts_mask_S", "Mask of working channels in SNR mode (0b1111 = all on, default)",
          bits=(7, 4), kind="mask"),
    Field(13, "Sts_mask_O", "Mask of working channels in OZhR mode (0b1111 = all on, default)",
          bits=(3, 0), kind="mask"),
    Field(14, "reserve", "reserve", bits=(7, 5), kind="res"),
    Field(14, "Synt_lock_detect", "Reference synthesizer PLL capture flag", bits=(4, 4),
          enum={0: "no PLL capture at last start", 1: "PLL frequency capture at last start"}),
    Field(14, "Sts_synth", "Which synthesizer is working", bits=(3, 0),
          enum={0: "all off", 1: "main", 2: "reserve"}),
    Field(15, "angX", "Angle angX at the time of IOC issuance, step 0.2° (0x80 = no angles)", kind="ang"),
    Field(16, "angY", "Angle angY at the time of IOC issuance, step 0.2° (0x80 = no angles)", kind="ang"),
    Field(17, "reserve", "reserve", bits=(7, 5), kind="res"),
    Field(17, "Angl_valid", "Target designation validity flag (angX, angY)", bits=(4, 4),
          enum={0: "angles are invalid", 1: "angles are valid"}),
    Field(17, "Sts_auto_S_crit", "Criterion for automatic transition of the OZh to SNR", bits=(3, 3),
          enum={0: "on 'KBD-own' command", 1: "on a packet of correct structure (CRC ok)"}),
    Field(17, "BKU_Angl_True", "Angular ambiguity resolution bit on the ES", bits=(2, 2),
          enum={0: "rough orientation to ZS not in tolerance", 1: "rough orientation to ZS in tolerance"}),
    Field(17, "First_True", "Direction determination bit to the ES", bits=(1, 1),
          enum={0: "no direction finding / no signal from ZS", 1: "direction to the ZS found"}),
    Field(17, "Sts_CLTU_Ok", "Friend/Foe confirmation bit of the ZS", bits=(0, 0),
          enum={0: "no 'KBD-own' command in the interval", 1: "'KBD-own' command in the interval"}),
    Field(18, "ADC_Chnl1", "Signal power in channel 1 after all AGC and transformations *1", size=2),
    Field(20, "AGC1", "AGC coefficient 1 *1", size=2),
    Field(22, "AGC2", "AGC coefficient 2 *1"),
    Field(23, "VGA", "Parameters of the amplifier *1"),
    Field(24, "ADC_Chnl2", "Signal power in channel 2 after all AGC and transformations *1", size=2),
    Field(26, "FFT_bin", "Wideband FFT frequency index of the signal *1", kind="i"),
    Field(27, "NCO", "Frequency loop readings *1", size=3, kind="i"),
    Field(30, "ADC_Chnl3", "Signal power in channel 3 after all AGC and transformations *1", size=2),
    Field(32, "reserve", "reserve", size=4, kind="res"),
    Field(36, "ADC_Chnl4", "Signal power in channel 4 after all AGC and transformations *1", size=2),
    Field(38, "reserve", "reserve", size=4, kind="res"),
    _cnt(42, "Cnt_CLTU_All", "Counter of detected CBDs since power-on *2"),
    _cnt(44, "Cnt_CLTU_Correct", "Counter of KBDs received without error correction *2"),
    _cnt(46, "Cnt_CLTU_Revised", "Counter of CBDs accepted with error correction *2"),
    _cnt(48, "Cnt_TF_Transf", "Counter of sent TCs received from the KBD (IPPD-KPI) *2"),
    _cnt(50, "Cnt_TF_Test", "Counter of sent TC-Tests *2", size=1),
    _cnt(51, "Cnt_UPNG_Angl", "Counter of 'Target designation to the ZS' commands from the UPNG *2"),
    _cnt(53, "Cnt_Raw_Dir", "Counter of received 'Rough direction to ZS' commands *2"),
    _cnt(55, "Cnt_Com_CLTU_Ok", "Counter of accepted 'KBD-own' commands *2"),
    Field(57, "Last_Cmd", "Last accepted command (except 'Rough direction to ZS', 'KBD-your') *2"),
    Field(58, "Last_Cmd_Param_L", "Parameters of the last received command, bytes 0-3 *2", size=4),
    Field(62, "Last_Cmd_Param_H", "Parameters of the last received command, bytes 4-7 *2", size=4),
    _cnt(66, "Cnt_Cmd", "Counter of executed commands (except 'Rough direction', 'KBD-your') *2", size=1),
    _cnt(67, "Cnt_Cmd_Ignor", "Counter of ignored commands: not in the UPNG list / invalid parameters *2",
         size=1),
    _cnt(68, "Cnt_Mode_S", "Counter of SNR-mode activations in the current session *2", size=1),
    Field(69, "reserve", "reserve", bits=(7, 6), kind="res"),
    Field(69, "Cfg_CRC_nOK", "Checksum of the factory CONFIG file in ROM", bits=(5, 5),
          enum={0: "OK", 1: "error in both memories"}),
    Field(69, "Sts_CRC_nOK", "Checksum of the IOK_STATUS memory in EPROM", bits=(4, 4),
          enum={0: "OK", 1: "error in both memories"}),
    Field(69, "Sts_temp_behav", "Temperature response setting", bits=(3, 2),
          enum={0: "auto power-off disabled", 1: "auto-off by sensor 1",
                2: "auto-off by sensor 2", 3: "auto-off by both sensors"}),
    Field(69, "Flag_Alarm_Temp", "Temperature status", bits=(1, 1),
          enum={0: "normal", 1: "TEMPERATURE EXCESS"}),
    Field(69, "reserve", "reserve", bits=(0, 0), kind="res"),
    Field(70, "Temp1", "Temperature sensor 1 readings, degrees", kind="temp"),
    Field(71, "Temp2", "Temperature sensor 2 readings, degrees", kind="temp"),
    _cnt(72, "Cnt_Cfg_cmd", "Counter of commands changing MD CONFIG parameters"),
    _cnt(74, "Cnt_config", "Counter of received MD_CONFIG arrays"),
    Field(76, "angX1", "Angle angX at IOC issuance, option 1, step 0.2°", kind="ang"),
    Field(77, "angY1", "Angle angY at IOC issuance, option 1, step 0.2°", kind="ang"),
    Field(78, "angX2", "Angle angX at IOC issuance, option 2, step 0.2°", kind="ang"),
    Field(79, "angY2", "Angle angY at IOC issuance, option 2, step 0.2°", kind="ang"),
]


def tlm_header(md_type, name, length, tm="TLM"):
    return [
        Field(0, "Marker", "Start of file marker", expect=0x04),
        Field(1, "Type", f"Array type: {name}", expect=md_type),
        Field(2, "Length", "Packet length in bytes excluding marker, type, length", size=2, expect=length),
        Field(4, "AT", "Sender's address"),
        Field(5, f"{tm}_EXP", f"{tm} output frame format version"),
        _cnt(6, f"{tm}_CNT" if tm == "TLM" else "TM_COUNT",
             f"Cyclic counter of {tm} frames of this type since power-on / reset (0 after 0xFFFF)"),
    ]


def _ep(addr, mask):
    return f"  [EPROM addr {addr}, mask 0x{mask:X}]"


_EPROM_CNT = " Read from EPROM at power-up / reset, written back once per Tzap EPROM."
IOK_SOST = tlm_header(0xFA, "IOK SOST", 44) + [
    Field(8, "Firmware_Ver", "FPGA firmware version" + _ep(0, 0x3), size=2),
    Field(10, "Firmware_CRC", "Firmware checksum (p.4.1.2.3)" + _ep(0, 0xC), size=2),
    Field(12, "Instruction_Ver", "softCPU instruction set version" + _ep(1, 0x3), size=2),
    Field(14, "Instruction_CRC", "softCPU instruction set checksum (p.4.1.2.3)" + _ep(1, 0xC), size=2),
    _cnt(16, "Cnt_config", "Counter of received valid CONFIG MD arrays / 'Save settings in CONFIG file' "
         "commands (default 0x01)." + _EPROM_CNT + _ep(2, 0x3)),
    _cnt(18, "Cnt_Alarm_Temp", "Temperature protection response counter." + _EPROM_CNT + _ep(2, 0xC)),
    _cnt(20, "Cnt_Reset", "Reset counter on command 'Reset'." + _EPROM_CNT + _ep(3, 0x3)),
    _cnt(22, "Cnt_Pwr_ON", "Counter of power-on cycles over the whole life of the UPNG." + _ep(3, 0xC)),
    Field(24, "Work_Time", "Total operating time of the UPNG over its service life, 1 s per unit "
          "(counted from power-on, regardless of mode)" + _ep(4, 0xF), size=4, kind="dur"),
    Field(28, "Work_Time_Chnl_1", "Operating time of receiving channel 1, 1 s per unit" + _ep(5, 0xF),
          size=3, kind="dur"),
    Field(31, "Work_Time_Chnl_2", "Operating time of receiving channel 2, 1 s per unit" + _ep(6, 0xF),
          size=3, kind="dur"),
    Field(34, "Work_Time_Chnl_3", "Operating time of receiving channel 3, 1 s per unit" + _ep(7, 0xF),
          size=3, kind="dur"),
    Field(37, "Work_Time_Chnl_4", "Operating time of receiving channel 4, 1 s per unit" + _ep(8, 0xF),
          size=3, kind="dur"),
    Field(40, "Work_Time_Cynt_1", "Operating time of the main synthesizer, 1 s per unit" + _ep(9, 0xF),
          size=4, kind="dur"),
    Field(44, "Work_Time_Cynt_2", "Operating time of the backup synthesizer, 1 s per unit" + _ep(10, 0xF),
          size=4, kind="dur"),
]

# IOK DOP: header + 40 records x 3 bytes (one every 25 ms)
DOP_RECORDS, DOP_STEP_MS = 40, 25
DOP_COLS = [  # name, byte in record, (hi, lo), tooltip
    ("Corr_peak_f", 0, (7, 7), "Convolution peak exceeded threshold (packet detection): 1 = CBD detected (see byte #5 CONFIG)"),
    ("Ch_Inv", 0, (6, 4), "Channel inversion flags, channels 2-3-4 relative to 1: 1 = inverted"),
    ("Freq_sync", 0, (2, 2), "Frequency synchronization: 1 = achieved"),
    ("Bit_err", 0, (1, 1), "1 = at least one BCH block in the CBD has errors"),
    ("Err_rev", 0, (0, 0), "1 = all erroneous BCH blocks fixed, 0 = correction impossible"),
    ("Ph_sync", 1, (7, 4), "Channel phase synchronization flags (bits = channels 4321): 1 = phase error minimized"),
    ("Rx_on", 1, (3, 3), "1 = active state, 0 = passive state"),
    ("Bit_sync", 1, (2, 0), "Bit synchronization reference index (0..7)"),
    ("Conv_peak", 2, None, "Magnitude of the convolution peak, -80..80; negative = phase inversion"),
]

IOC_CONFIG = tlm_header(0xF9, "IOC CONFIG", 36) + [   # table says Length 28, capture + bytes 8-39 give 36
    Field(8 + i, f"CONFIG[{i}]", f"MD CONFIG byte {i} (clause 4.1.2.1)", kind="byte") for i in range(32)]

IOC_FAILURE = tlm_header(0xF8, "IOC FAILURE", 12)
IOC_FAILURE[-1] = Field(6, "TLM_CNT", "This TLM type has no cyclic frame counter, constant 0x0000",
                        size=2, expect=0)
IOC_FAILURE += [
    Field(8, "CRC1_MEM", "CRC of the program read from main memory", size=2),
    Field(10, "CRC1_CALC", "CRC of the program calculated for main memory", size=2,
          compare=(8, "CRC1_MEM")),
    Field(12, "CRC2_MEM", "CRC of the program read from backup memory", size=2),
    Field(14, "CRC2_CALC", "CRC of the program calculated for the backup memory", size=2,
          compare=(12, "CRC2_MEM")),
]

_rst = " Reset on power-on / reset."
BOS_OSN = tlm_header(0xFC, "IOK OSN (BOS)", 100, tm="TM") + [
    Field(8, "BOS_Time", "BOS time (format see p.4.2.3.12)", size=4),
    Field(12, "Cnt_1C", "Received pulse counter 1C." + _rst),
    Field(13, "Cnt_Cmd_Time_BSHV", "Counter of received 'BSHV Time' commands." + _rst),
    Field(14, "Itime_SNR", "Integer part of the time from 'Set transfer time in SR', 1 s per unit (1)", size=4),
    Field(18, "Ftime_SNR", "Fractional part of the time from 'Set transfer time in SR', 10 ms per unit (1)"),
    Field(19, "PulsNumb_10MHz", "Pulses of the external 10 MHz generator in the interval Tinterv10MHz", size=3),
    Field(22, "Err_Ref_LO", "EG tuning code (external - internal EG pulse counters), should approach 0, "
          "normal ±1000", size=2, kind="i", limit=1000),
    Field(24, "Stat_10MHz", "External 10 MHz generator validity (checked every 1 s) (2)", bits=(0, 0),
          enum={0: "no signal from the generator", 1: "norm"}),
    Field(24, "Sour_10MHz", "Current 10 MHz reference oscillator source (3)", bits=(1, 1),
          enum={0: "internal", 1: "external 10 MHz"}),
    Field(24, "Random_mode", "Data randomization mode in SR", bits=(2, 2), enum={0: "no", 1: "yes"}),
    Field(24, "Config_Type", "Current configuration", bits=(3, 3),
          enum={0: "factory (from BOS software)", 1: "custom (from CONFIG file)"}),
    Field(24, "Numb_BOS", "Number of the BOS kit issuing the IOC", bits=(4, 4),
          enum={0: "first set", 1: "second set"}),
    Field(24, "Mode", "Current operating mode of the BOS (4)", bits=(5, 5),
          enum={0: "standby mode", 1: "session mode (SR)"}),
    Field(24, "BPU_AFAR_err", "Error flag for automatic selection of the BPU kit", bits=(6, 6),
          enum={0: "normal", 1: "BPU not selected"}),
    Field(24, "reserve", "reserve", bits=(7, 7), kind="res"),
    Field(25, "BPU_AFAR_Status", "Included BPU kit", bits=(1, 0),
          enum={0: "both disabled", 1: "BPU1 included", 2: "BPU2 included", 3: "both sets included"}),
    Field(25, "QuickOn_mode", "Quick-start control mode of SR in AFAR", bits=(3, 2),
          enum={1: "QuickOn signal only", 2: "commands via CAN only", 3: "signal and command"}),
    Field(25, "CAN_Mode", "Operating mode of the BOS according to CAN", bits=(5, 4),
          enum={1: "via CAN1 bus", 2: "via CAN2 bus", 3: "on both CAN buses"}),
    Field(25, "reserve", "reserve", bits=(7, 6), kind="res"),
    Field(26, "Sour_Angle_Enable", "Current source of target designations used for the ZS", bits=(1, 0),
          enum={0: "no reliable designations, source not selected", 1: "UPNG", 2: "BKU", 3: "BKU and UPNG"}),
    Field(26, "Ang_Valid", "Target designation validity (angX, angY) (5)", bits=(3, 2),
          enum={0: "invalid from both sources", 1: "BKU invalid, UPNG valid",
                2: "BKU valid, UPNG invalid", 3: "valid from both sources"}),
    Field(26, "reserve", "reserve", bits=(7, 4), kind="res"),
    Field(27, "Temp_mode", "Automatic shutdown when the temperature is exceeded", bits=(3, 0),
          enum={0: "auto power-off disabled", 1: "on sensor 1", 2: "on sensor 2", 4: "on sensor 3",
                8: "on sensor 4", 15: "on any sensor"}),
    Field(27, "Adrss_TD", "Temperature sensor that caused automatic shutdown (trigger, bit = sensor) (6)",
          bits=(7, 4), kind="trig"),
    Field(28, "Mode_SNR", "Sub-mode of the BOS in the SR (current / last) (7)",
          enum={0x00: "no transmission since power-on", 0x40: "carrier transmission",
                0xA1: "CBD transfer to UR", 0xA2: "preamble in SR at reception rate",
                0xA3: "POOSSH in SR at reception rate", 0xA4: "KBD in SR at reception rate",
                0xA5: "preamble in SR at delayed time", 0xA6: "POOSSH in SR at delayed time",
                0xA7: "KBD in SR at delayed time"}),
    Field(29, "Cnt_Alarm_Templ", "SR shutdowns by automatic temperature shutdown (8)"),
    Field(30, "Cnt_All_SNR", "SR shutdowns because the SR duration ('Time SNR' in CONFIG) was exceeded (8)"),
    Field(31, "Cnt_New_Cmd", "SR shutdowns on receipt of a new SR control command (8)"),
    Field(32, "Cnt_Cmd_SNR_Off", "Forced SR shutdowns by the 'Turn on the JR' command (8)"),
    Field(33, "Cnt_Ang_Err", "SR shutdowns on unacceptable angles angX/angY in 'Target designations' (9)"),
    Field(34, "Last_Cmd_SNR", "Number of the last received SR control command"),
    Field(35, "Last_Param_SNR", "Parameters of the last received SR control command", size=8),
    Field(43, "Cnt_SNR_Cmd", "Logical SR counter for the current BOS enable." + _rst),
    Field(44, "Cnt_SNR_Sgnl", "Physical SR counter (RF signal generated) for the current enable." + _rst + " (10)"),
    Field(45, "AngX_UPNG", "angX from 'Target designation to the ZS from the UPNG' (format p.4.2.3.18) (11)"),
    Field(46, "AngY_UPNG", "angY from 'Target designation to the ZS from the UPNG' (format p.4.2.3.18) (11)"),
    Field(47, "AngX_BKU", "angX from 'Target designation to the ZS from the BKU' (format p.4.2.3.17) (11)"),
    Field(48, "AngY_BKU", "angY from 'Target designation to the ZS from the BKU' (format p.4.2.3.17) (11)"),
    Field(49, "Config_Nmb", "'User' configuration file number (from CONFIG, low byte)", size=2),
    Field(51, "Config_Nmb_2", "Sub-number of the 'user' configuration file, +1 per valid CONFIG change (12)"),
    _cnt(52, "Cnt_RX_KBD+", "Received KBD via IPPD-TMI in the current BOS session (all SRs)"),
    _cnt(54, "Cnt_RX_KBD_IgnrL", "Ignored KBD via IPPD-TMI ('short' criterion)", size=1),
    _cnt(55, "Cnt_RX_KBD_IgnrH", "Ignored KBD via IPPD-TMI ('long' criterion)", size=1),
    _cnt(56, "Cnt_TX_KBD+", "Sent KBDs in the current BOS session (all SRs)." + _rst),
    _cnt(58, "Cnt_TX_KBD_BB+", "Sent CBDs in the current BOS SR session"),
    _cnt(60, "Cnt_UPNG_Ang", "Received 'Target designation to the ZS from the UPNG' commands"),
    _cnt(62, "Cnt_BKU_Ang", "Received 'Target designation to the ZS from the BKU' commands." + _rst),
    Field(64, "Last_Cmd", "Last received command (except cyclic BShV / target designation and SR control)."
          + _rst),
] + [Field(65 + i, f"byte {65 + i}", "NOT IN THE PROVIDED TABLES (bytes 65-72)", kind="byte")
     for i in range(8)] + [
    _cnt(73, "Cnt_Cmd_True", "Executed commands (except cyclic BShV / target designation and SR control)"),
    _cnt(75, "Cnt_Cmd_False", "Ignored commands: invalid parameters / not in the BOS list", size=1),
    _cnt(76, "Cnt_Req_KBD", "Non-zero KBD requests issued in BARL (reset by SR control command)", size=1),
    Field(77, "Last_Nmb_KBD_Req", "Last non-zero number of requested CBDs ('Request CBD in BARL')"),
    Field(78, "T_SNR+", "Total duration of operation in the SR, 10 ms per unit (13)", size=3, kind="ms10"),
    Field(81, "T_SNR_BB+", "Total duration of work in the SR, 10 ms per unit (13)", size=3, kind="ms10"),
    Field(84, "Period_FA", "Period of issuance of IOK CONFIG, 1 s per unit, 0 = disabled (14)", kind="period"),
    Field(85, "Period_FC", "Period of issuance of IOK OSN, 1 s per unit, 0 = disabled (14)", kind="period"),
    Field(86, "Period_FD", "Period of issuance of IOK SOST, 1 s per unit, 0 = disabled (14)", kind="period"),
    Field(87, "Period_FB", "Period of issuance of IOK DOP, 1 s per unit, 0 = disabled (14)", kind="period"),
    Field(88, "Temp_DT1", "Temperature sensor 1, degrees", kind="temp"),
    Field(89, "Temp_DT2", "Temperature sensor 2, degrees", kind="temp"),
    Field(90, "Temp_DT3", "Temperature sensor 3, degrees", kind="temp"),
    Field(91, "Temp_DT4", "Temperature sensor 4, degrees (not used)", kind="temp"),
    Field(92, "Temp_Alarm", "Temperature at the moment of automatic shutdown (trigger) (15)", kind="talarm"),
    Field(93, "Point_Adrss", "Flash write pointer of IOK_ARCHIVE, in 16-byte steps (one SR entry)", size=2),
    _cnt(95, "Cnt_BPU1", "Data received from BPU1 via feedback, +1 per second (changing = BPU1 works)", size=1),
    _cnt(96, "Cnt_BPU2", "Data received from BPU2 via feedback, +1 per second (changing = BPU2 works)", size=1),
    Field(97, "reserve", "Reserve, padding to a multiple of 8", size=7, kind="res"),
]
BOS_OSN_NOTES = (
    "(1) set by 'Set transmission time in SR', 0 after power-on.  (2) checked every 1 s.  "
    "(3) falls back to internal if the external 10 MHz is missing/lost.  (4) set while the SR is logically "
    "on.  (5) UPNG validity from its command, BKU validity vs. CONFIG limits.  (6) trigger, cleared on "
    "power-up.  (7) 0x00 after power-on; A2/A3/A4 become A5/A6/A7 with a delayed time.  (8) counted "
    "while in logical SR.  (10) may grow faster than Cnt_SNR_Cmd in ImpRP mode.  (11) written whether "
    "or not the angles are reliable.  (12) save CONFIG before the sub-number overflows.  (13) since "
    "power-on, physical SR (RF radiated).  (14) from CONFIG / 'Set telemetry output period'.  "
    "(15) -128 after power-on/reset.")

BOS_SOST = tlm_header(0xFD, "IOK SOST (IOC CONDITION)", 44, tm="TM") + [
    Field(8, "Numb_BOS", "Number of the BOS kit issuing the IOC", bits=(7, 7),
          enum={0: "first set", 1: "second set"}),
    Field(8, "reserve", "reserve", bits=(6, 0), kind="res"),
    Field(9, "Soft_MORE_Ver", "FPGA firmware version", size=2),
    _cnt(11, "Cnt_Config_Save", "MD_CONFIG save counter in flash memory: +1 per valid configuration "
         "array or 'Save settings in CONFIG file' command (1)"),
    _cnt(13, "Cnt_Data_Save", "Flash data storage counter: +1 each time the IOK_STATE parameters are "
         "written to flash ('Data Parking' and 'Change' commands) (2)"),
    _cnt(15, "Cnt_Alarm_Temp_All", "Automatic shutdown counter based on temperature"),
    _cnt(17, "Cnt_Turn_ON", "Power-on counter"),
    _cnt(19, "Cnt_SNR_Cmd_All", "Logical SR inclusions (= SR control commands)", size=3),
    _cnt(22, "Cnt_SNR_Sgnl_All", "Physical SR inclusions (RF signal formed), all modes (UR + SR + CARRIER)",
         size=3),
    _cnt(25, "Cnt_SNR_BB_Sgnl_All", "Physical SR inclusions (RF signal formed), SR mode only", size=3),
    _cnt(28, "Cnt_TX_KBD_All", "KBD sent to the radio line, all modes (3)", size=4),
    _cnt(32, "Cnt_TX_KBD_BB_All", "CBDs sent to the radio line, SR only (3)", size=4),
    Field(36, "Work_Time", "Total operating time of the BOS in all modes (LH + SR), 1 s per unit",
          size=4, kind="dur"),
    Field(40, "T_SNR_All", "Total duration of operation in SR regardless of BOS mode, 10 ms per unit",
          size=4, kind="ms10"),
    Field(44, "T_SNR_BB_All", "Total duration of work in the SR, 10 ms per unit", size=4, kind="ms10"),
]
BOS_SOST_NOTES = ("(1) counts valid CONFIG arrays and 'Save settings in CONFIG file'.  (2) counts writes of "
                  "IOK_STATE to flash.  (3) only KBD generated from IPPD data; KBD-HC generated by the BOS "
                  "are not counted.")

BOS_DOP = tlm_header(0xFB, "IOK DOP (BOS)", 28, tm="TM") + [
    Field(8, "Numb_BOS", "Number of the BOS kit issuing the IOC", bits=(0, 0),
          enum={0: "first set", 1: "second set"}),
    Field(8, "pll_lmk", "Encoder synthesizer PLL capture", bits=(1, 1), enum={0: "PLL unlock", 1: "PLL lock"}),
    Field(8, "pll_jesd", "JESD PLL capture FPGA", bits=(2, 2), enum={0: "PLL unlock", 1: "PLL lock"}),
    Field(8, "pll_all", "PLL capture FPGA (except JESD PLL)", bits=(3, 3), enum={0: "PLL unlock", 1: "PLL lock"}),
    Field(8, "pll_lmx (DAC)", "Encoder PLL DAC capture", bits=(4, 4), enum={0: "PLL unlock", 1: "PLL lock"}),
    Field(8, "pll_lmx (mod)", "PLL synthesizer modulator lock (table names it pll_lmx too)", bits=(5, 5),
          enum={0: "PLL unlock", 1: "PLL lock"}),
    Field(8, "jesd_err", "JESD DAC interface", bits=(6, 6), enum={0: "error", 1: "no errors"}),
    Field(8, "reserve", "reserve (bit 7, not in the table)", bits=(7, 7), kind="res"),
    Field(9, "vdd_1v", "Voltage 1 V FPGA (1)", size=2, kind="fix3"),
    Field(11, "vdd_1_8v", "Voltage 1.8 V FPGA (1)", size=2, kind="fix3"),
    Field(13, "vdd_2_5v", "Voltage 2.5 V FPGA (1)", size=2, kind="fix3"),
    Field(15, "reserve", "Reserve, padding to a multiple of 8", kind="res"),
] + [Field(16 + i, f"byte {16 + i}", "NOT IN THE TABLE (the table ends at byte 15, the unit sends 32 B)",
           kind="byte") for i in range(16)]
BOS_DOP_NOTES = ("(1) bit 15 sign, bits 14..3 integer part, 2..0 fractional part (1/8); the values fit "
                 "millivolts (e.g. 0x20DF = 1051.875 mV).")
WARN_ENUM_BOS = {("pll_lmk", 0), ("pll_jesd", 0), ("pll_all", 0), ("pll_lmx (DAC)", 0), ("pll_lmx (mod)", 0),
                 ("jesd_err", 0)}

WARN_ENUM = WARN_ENUM_BOS | {("BPU_AFAR_err", 1), ("Stat_10MHz", 0), ("Flag_Alarm_Temp", 1), ("Cfg_CRC_nOK", 1), ("Sts_CRC_nOK", 1)}
NOTES = ("*1  processing algorithm in Appendix A; 0x0000 at power-on / reset / disabled channel, "
         "last value in DZR mode.     *2  cleared on power cycle and reset.     "
         "Angle options 1 and 2 differing by more than 2° indicates problems in the channels.")

C_WARN = QtGui.QColor("#ff6b6b")
C_DIM = QtGui.QColor("#5c6570")
C_NAME = QtGui.QColor("#79c0ff")
C_HL = QtGui.QColor("#5a3a12")


def _mono():
    f = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
    f.setPointSize(max(9, f.pointSize()))
    return f


def hexdump(data: bytes) -> str:
    return "\n".join(f"{i:3d}:  " + " ".join(f"{b:02X}" for b in data[i:i + 8])
                     for i in range(0, len(data), 8))


# --------------------------------------------------------------------------- #
#  Views                                                                       #
# --------------------------------------------------------------------------- #
class ArrayHeader(QtWidgets.QLabel):
    def __init__(self):
        super().__init__("no array received yet - connect the CAN tab or open a log there")
        self.setStyleSheet("color:#8a96a3;padding:4px;")
        self.setWordWrap(True)

    def show_info(self, base, name, size, data, ts, count, errors):
        ok = len(data) == size
        t = _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
        self.setText(f"<b>{name}</b> &nbsp; ID {base:08X} &nbsp;·&nbsp; last {t} &nbsp;·&nbsp; "
                     f"received {count} &nbsp;·&nbsp; "
                     + (f"{len(data)} B" if ok else f"<span style='color:#ff6b6b'>{len(data)} B, "
                                                   f"expected {size} B</span>")
                     + (f" &nbsp;·&nbsp; <span style='color:#f0b03e'>incomplete arrays: {errors}</span>"
                        if errors else ""))


class FieldView(QtWidgets.QWidget):
    """Table: Byte | Bits | Designation | Value | Meaning | Description."""
    COLS = ["Byte", "Bits", "Designation", "Value", "Meaning", "Description"]

    def __init__(self, md_type, name, size, fields, notes=""):
        super().__init__()
        self.md_type, self.name, self.size, self.fields = md_type, name, size, fields
        self.prev: list | None = None
        self.count = 0
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.header = ArrayHeader()
        v.addWidget(self.header)
        self.table = QtWidgets.QTableWidget(len(fields), len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setDefaultSectionSize(QtGui.QFontMetrics(_mono()).height() + 6)
        mono = _mono()
        for r, f in enumerate(fields):
            b, bits = f.pos()
            for c, text in enumerate((b, bits, f.name, "—", "", f.desc)):
                it = QtWidgets.QTableWidgetItem(text)
                if c in (0, 1, 3):
                    it.setFont(mono)
                    it.setTextAlignment(QtCore.Qt.AlignCenter if c < 3 else
                                        QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
                if c == 2:
                    it.setForeground(C_DIM if f.kind == "res" else C_NAME)
                if f.kind == "res":
                    it.setForeground(C_DIM)
                self.table.setItem(r, c, it)
            self.table.item(r, 5).setToolTip(f.desc)
        h = self.table.horizontalHeader()
        for c, w in enumerate((60, 50, 170, 185, 250)):
            self.table.setColumnWidth(c, w)
        h.setStretchLastSection(True)
        v.addWidget(self.table, 1)
        if notes:
            note = QtWidgets.QLabel(notes)
            note.setWordWrap(True)
            note.setStyleSheet("color:#6e7681;font-size:11px;")
            v.addWidget(note)
        self.hl = QtCore.QTimer(self, singleShot=True, interval=1500, timeout=self._clear_hl)

    def update_array(self, base, data, ts, errors):
        self.count += 1
        self.header.show_info(base, self.name, self.size, data, ts, self.count, errors)
        vals = [f.raw(data) for f in self.fields]
        for r, (f, v) in enumerate(zip(self.fields, vals)):
            val_it, mean_it = self.table.item(r, 3), self.table.item(r, 4)
            if v is None:
                val_it.setText("—")
                mean_it.setText("not in array")
                mean_it.setForeground(C_WARN)
                continue
            text, warn = f.meaning(v)
            warn = warn or (f.name, v) in WARN_ENUM
            if f.compare and f.byte + f.size <= len(data):
                ref = int.from_bytes(data[f.compare[0]:f.compare[0] + f.size], "little")
                text, warn = ((f"= {f.compare[1]}", False) if ref == v else
                              (f"MISMATCH with {f.compare[1]} (0x{ref:04X})", True))
            val_it.setText(f.value_str(v))
            mean_it.setText(text)
            mean_it.setForeground(C_WARN if warn else QtGui.QBrush())
            changed = self.prev is not None and self.prev[r] != v
            for it in (val_it, mean_it):
                it.setBackground(C_HL if changed else QtGui.QBrush())
        self.prev = vals
        self.hl.start()

    def _clear_hl(self):
        for r in range(self.table.rowCount()):
            for c in (3, 4):
                self.table.item(r, c).setBackground(QtGui.QBrush())


class RawView(QtWidgets.QWidget):
    """Arrays whose layout is not described yet: hex dump of the last one."""

    def __init__(self, md_type, name, size):
        super().__init__()
        self.md_type, self.name, self.size, self.count = md_type, name, size, 0
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.header = ArrayHeader()
        v.addWidget(self.header)
        info = QtWidgets.QLabel(f"Field layout of {name} not entered yet - raw bytes of the last array:")
        info.setStyleSheet("color:#8a96a3;")
        v.addWidget(info)
        self.dump = QtWidgets.QPlainTextEdit()
        self.dump.setReadOnly(True)
        self.dump.setFont(_mono())
        v.addWidget(self.dump, 1)

    def update_array(self, base, data, ts, errors):
        self.count += 1
        self.header.show_info(base, self.name, self.size, data, ts, self.count, errors)
        self.dump.setPlainText(hexdump(data))


class DopView(QtWidgets.QWidget):
    """IOK DOP: header fields + 40 records (25 ms apart) as a table and a Conv_peak plot."""

    def __init__(self, md_type, name, size):
        super().__init__()
        self.md_type, self.name, self.size = md_type, name, size
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.head = FieldView(md_type, name, size, tlm_header(md_type, name, size - 4), notes="")
        self.head.table.setMaximumHeight(QtGui.QFontMetrics(_mono()).height() * 9 + 40)
        v.addWidget(self.head)
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        cols = ["#", "t, ms"] + [c[0] for c in DOP_COLS]
        self.table = QtWidgets.QTableWidget(DOP_RECORDS, len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        for c, (_, _, _, tip) in enumerate(DOP_COLS):
            self.table.horizontalHeaderItem(c + 2).setToolTip(tip)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(QtGui.QFontMetrics(_mono()).height() + 4)
        mono = _mono()
        for r in range(DOP_RECORDS):
            for c in range(len(cols)):
                it = QtWidgets.QTableWidgetItem(str(r) if c == 0 else str(r * DOP_STEP_MS) if c == 1 else "—")
                it.setTextAlignment(QtCore.Qt.AlignCenter)
                it.setFont(mono)
                self.table.setItem(r, c, it)
        self.table.resizeColumnsToContents()
        split.addWidget(self.table)
        self.plot = None
        try:
            import pyqtgraph as pg
            self.plot = pg.PlotWidget(title="Conv_peak over the last second")
            self.plot.showGrid(x=True, y=True, alpha=0.25)
            self.plot.setLabel("bottom", "t, ms")
            self.plot.setYRange(-80, 80)
            self.plot.addLine(y=0, pen=pg.mkPen("#3a424c"))
            self.curve = self.plot.plot([], [], pen=pg.mkPen("#ffc400", width=2), symbol="o",
                                        symbolSize=5, symbolBrush="#ffc400", symbolPen=None)
            split.addWidget(self.plot)
        except Exception:
            pass
        split.setSizes([700, 500])
        v.addWidget(split, 1)
        note = QtWidgets.QLabel("Records are stored every 25 ms (40 per array). Hover the column headers "
                                "for the meaning of each flag.")
        note.setStyleSheet("color:#6e7681;font-size:11px;")
        v.addWidget(note)

    @property
    def count(self):
        return self.head.count

    def update_array(self, base, data, ts, errors):
        self.head.update_array(base, data, ts, errors)
        peaks = []
        for r in range(DOP_RECORDS):
            rec = data[8 + 3 * r: 11 + 3 * r]
            for c, (name, bi, bits, _) in enumerate(DOP_COLS):
                it = self.table.item(r, c + 2)
                if len(rec) < 3:
                    it.setText("—")
                    continue
                b = rec[bi]
                if bits is None:                      # Conv_peak, INT8
                    val = b - 256 if b > 127 else b
                    peaks.append(val)
                    it.setText(str(val))
                    it.setForeground(C_WARN if abs(val) > 80 else QtGui.QBrush())
                    continue
                hi, lo = bits
                val = (b >> lo) & ((1 << (hi - lo + 1)) - 1)
                it.setText(f"0b{val:0{hi - lo + 1}b}" if name in ("Ch_Inv", "Ph_sync") else str(val))
                it.setForeground(C_WARN if name == "Bit_err" and val else
                                 C_DIM if val == 0 else QtGui.QBrush())
        if self.plot is not None:
            self.curve.setData([i * DOP_STEP_MS for i in range(len(peaks))], peaks)


class TelemetryTab(QtWidgets.QTabWidget):
    """Top-level Telemetry tab: BOS / UPNG / AFAR, one sub-tab per array."""

    LAYOUTS = {  # (unit, type) -> (fields, notes)
        ("UPNG", 0xFC): (IOK_OSN, NOTES),
        ("UPNG", 0xFA): (IOK_SOST, ""),
        ("UPNG", 0xF9): (IOC_CONFIG, ""),
        ("UPNG", 0xF8): (IOC_FAILURE, ""),
        ("BOS", 0xFC): (BOS_OSN, BOS_OSN_NOTES),
        ("BOS", 0xFD): (BOS_SOST, BOS_SOST_NOTES),
        ("BOS", 0xFB): (BOS_DOP, BOS_DOP_NOTES),
    }

    def __init__(self):
        super().__init__()
        self.rx = Reassembler()
        self.views = {}                  # (unit, type) -> view
        self.prefix_unit = {p: u for u, p, _ in UNITS if p is not None}
        for unit, prefix, arrays in UNITS:
            if not arrays:
                lbl = QtWidgets.QLabel(f"{unit}: telemetry not described yet.")
                lbl.setAlignment(QtCore.Qt.AlignCenter)
                lbl.setStyleSheet("color:#8a96a3;")
                self.addTab(lbl, unit)
                continue
            sub = QtWidgets.QTabWidget()
            for t, name, size in arrays:
                key = (unit, t)
                if key in self.LAYOUTS:
                    fields, notes = self.LAYOUTS[key]
                    view = FieldView(t, name, size, fields, notes=notes)
                elif key == ("UPNG", 0xFB):
                    view = DopView(t, name, size)
                else:
                    view = RawView(t, name, size)
                self.views[key] = view
                sub.addTab(view, f"{name} (0x{t:02X})")
            self.addTab(sub, f"{unit}  (ID 0x{prefix:04X}xxxx)")
        self.setCurrentIndex(0)

    def on_frames(self, frames):
        """Listener for canable_analyzer.SnifferWindow.ingest()."""
        for f in frames:
            r = self.rx.feed(f)
            if r is None:
                continue
            base, t, data, ts = r
            view = self.views.get((self.prefix_unit.get(base >> 16), t))
            if view is not None:
                view.update_array(base, data, ts, self.rx.errors)
