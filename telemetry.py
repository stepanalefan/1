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
MD_TYPES = {t for t, _, _ in MD_ARRAYS}


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
                 compare=None):
        self.byte, self.name, self.desc = byte, name, desc
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
        if self.kind in ("i", "ang", "temp"):
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
        if self.kind in ("i", "temp"):
            return f"{v}"
        return f"0x{v:0{2 * self.size}X}  ({v})" if self.size > 1 or v > 9 else f"0x{v:02X}"

    def meaning(self, v: int) -> tuple[str, bool]:
        """(text, warn)"""
        if self.kind == "ang":
            return ("no angle (0x80)", False) if v == -128 else (f"{v * 0.2:+.1f}°", False)
        if self.kind == "temp":
            return f"{v} °C", False
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


def tlm_header(md_type, name, length):
    return [
        Field(0, "Marker", "Start of file marker", expect=0x04),
        Field(1, "Type", f"Array type: {name}", expect=md_type),
        Field(2, "Length", "Packet length in bytes excluding marker, type, length", size=2, expect=length),
        Field(4, "AT", "Sender's address"),
        Field(5, "TLM_EXP", "TLM output frame format version"),
        _cnt(6, "TLM_CNT", "Cyclic counter of TLM frames of this type since power-on / reset (0 after 0xFFFF)"),
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

WARN_ENUM = {("Flag_Alarm_Temp", 1), ("Cfg_CRC_nOK", 1), ("Sts_CRC_nOK", 1)}
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
    """Top-level Telemetry tab: BOS / UPNG / AFAR."""

    LAYOUTS = {0xFC: IOK_OSN, 0xFA: IOK_SOST, 0xF9: IOC_CONFIG, 0xF8: IOC_FAILURE}

    def __init__(self):
        super().__init__()
        self.rx = Reassembler()
        self.views = {}
        for unit in ("BOS", "UPNG", "AFAR"):
            if unit != "UPNG":
                lbl = QtWidgets.QLabel(f"{unit}: telemetry not described yet.")
                lbl.setAlignment(QtCore.Qt.AlignCenter)
                lbl.setStyleSheet("color:#8a96a3;")
                self.addTab(lbl, unit)
                continue
            sub = QtWidgets.QTabWidget()
            for t, name, size in MD_ARRAYS:
                if t in self.LAYOUTS:
                    view = FieldView(t, name, size, self.LAYOUTS[t], notes=NOTES if t == 0xFC else "")
                elif t == 0xFB:
                    view = DopView(t, name, size)
                else:
                    view = RawView(t, name, size)
                self.views[t] = view
                sub.addTab(view, f"{name} (0x{t:02X})")
            self.addTab(sub, unit)
        self.setCurrentIndex(1)

    def on_frames(self, frames):
        """Listener for canable_analyzer.SnifferWindow.ingest()."""
        for f in frames:
            r = self.rx.feed(f)
            if r is not None:
                base, t, data, ts = r
                self.views[t].update_array(base, data, ts, self.rx.errors)
