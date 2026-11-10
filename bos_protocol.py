# -*- coding: utf-8 -*-
"""
bos_protocol.py
===============
BOS control protocol over CAN 2.0B (ПИЛВ № 0102-004, part 1). Pure Python, no GUI.

29-bit identifier
    bit 28      C/D: 0 = command / acknowledgement, 1 = data array
    bits 27-22  sender address (AO)          bits 21-16  receiver address (AP)
    commands:   bit 15 = 0 command / 1 acknowledgement, bit 14 = acknowledgement request (in an
                acknowledgement: 1 = negative), bit 13 = command has a data field, bits 12-8 = 0,
                bits 7-0 = command code
    arrays:     bits 15-12 start 0000 / current 0100 / stop 1000 / stop + ack 1100,
                bits 11-8 packet counter, bits 7-0 array number
Addresses (table 3.1 / 3.2): BOS 0x29; senders БИОС 1.1-2.2 0x1C-0x1F, ВМ 1-2 0x20-0x21, БЭС 0x16,
КПА ИВУ 0x18, УПНГ 0x2A, АФАР 0x2B; group УПНГ+АФАР+БОС 0x0D (target designation), telemetry
from the BOS goes to БИОС+ВМ 0x06 (-> the 0x1A46xxxx prefix).
Acknowledgement (4.3): sender 0x29, receiver = command sender, bit 15 = 1, bit 14 = 1 negative,
data[0..4] = command data[0..4], data[5] = acknowledgement byte, data[6..7] = array packets.
Data are little-endian. CRC-16 (CCITT, 0x1021, init 0xFFFF) protects МД_КОНФИГ (bytes 134-135).
"""

from __future__ import annotations

BOS = 0x29
GROUP_ANGLE = 0x0D            # УПНГ+АФАР+БОС: target designation commands, no acknowledgement
SENDERS = [  # address, name
    (0x18, "КПА ИВУ (test equipment)"), (0x20, "ВМ 1"), (0x21, "ВМ 2"),
    (0x1C, "БИОС 1.1"), (0x1D, "БИОС 1.2"), (0x1E, "БИОС 2.1"), (0x1F, "БИОС 2.2"), (0x16, "БЭС"),
]

# code: (mnemonic, data bytes, description)
COMMANDS = {
    0x00: ("NOP", 0, "Empty command (link check, acknowledged)"),
    0x01: ("MODE_D", 0, "Standby mode (ДЖР): ends any session, RF off"),
    0x02: ("SET_Sour_10MHz", 1, "10 MHz reference source (ДЖР only)"),
    0x03: ("SET_Data_Randomiz", 1, "Randomisation in ШР/КБД (ДЖР only)"),
    0x04: ("SET_Time_SNR", 1, "Session duration limit, s (ДЖР only)"),
    0x06: ("SET_CAN", 1, "CAN buses for transmission (ДЖР only)"),
    0x0C: ("Angle_ErdStation_BKU", 2, "Target designation to the ground station from the БКУ"),
    0x0D: ("SET_Angle_ErdStation_Err", 1, "Permissible target designation angle (ДЖР only)"),
    0x0E: ("SAVE_CONFIG_NEW", 0, "Save settings in the КОНФИГ file"),
    0x10: ("SET_Period_TM", 8, "Telemetry output period"),
    0x11: ("REQ_TM", 2, "Request telemetry now"),
    0x17: ("SET_Temp_mode", 1, "Over-temperature shutdown mode"),
    0x19: ("Park", 0, "Data parking"),
    0xA0: ("Send_Carrier", 8, "Session: transmit carrier"),
    0xA1: ("Send_UB_Data", 8, "Session: narrowband (УР) КБД"),
    0xA2: ("Send_Preamb", 8, "Session: wideband (ШР) preambles"),
    0xA3: ("Send_SNR_Sequens", 8, "Session: ШР ПООСШ (SNR sequence)"),
    0xA4: ("Send_KBD", 8, "Session: ШР КБД"),
}
SR_COMMANDS = {0xA0: "Carrier", 0xA1: "УР КБД", 0xA2: "ШР preambles", 0xA3: "ШР ПООСШ", 0xA4: "ШР КБД"}
TM_TYPES = {0xFC: "ИОК_ОСН", 0xFB: "ИОК_ДОП", 0xFD: "ИОК_СОСТОЯНИЕ", 0xFA: "ИОК_КОНФИГ", 0xF9: "ИОК_АРХИВ"}
RATES = {0: 8, 1: 16, 2: 32, 3: 64, 4: 128, 5: 256, 6: 512, 7: 1024}     # kbit/s
F0_MHZ, F_STEP_MHZ = 37650.0, 0.125                                        # Freq = 37650 + 0.125·N MHz
FREQ_N_RANGE = {0xA0: (8, 1592), 0xA1: (8, 1592), 0xA2: (656, 944), 0xA3: (656, 944), 0xA4: (656, 944)}
ATTEN_MAX_DB = 20                                                          # Atten_EIRP saturates above 20 dB
T_KVIT = 0.05                                                              # acknowledgement delay < 50 ms
T_ANGLE_VALID = 3.0                                                        # target designation valid for 3 s


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


def freq_hz(n: int) -> float:
    return (F0_MHZ + F_STEP_MHZ * n) * 1e6


def cmd_id(code: int, sender: int, receiver: int = BOS, ack: bool = True, has_data: bool = True) -> int:
    return ((sender & 0x3F) << 22) | ((receiver & 0x3F) << 16) | (int(ack) << 14) | (int(has_data) << 13) | (code & 0xFF)


def command(code: int, sender: int, data: bytes = b"", ack: bool = True, receiver: int = BOS):
    """(can_id, data) of a command frame. Commands with parameters always carry 8 bytes."""
    n = COMMANDS.get(code, ("", 8, ""))[1]
    payload = (bytes(data) + bytes(8))[:8] if n else b""
    return cmd_id(code, sender, receiver, ack, bool(n)), payload


# ---- parameter builders (tables 4.9 - 4.20, 4.35) ----------------------------------------------
def p_byte(v: int) -> bytes:
    return bytes([v & 0xFF])


def p_period_tm(tm_type: int, seconds: float) -> bytes:
    """SET_Period_TM: type, TM_EXP 0, 2 reserve, period in 1/16 s (0 = off), 2 reserve."""
    v = max(0, min(0x0FFF, int(round(seconds * 16))))
    return bytes([tm_type, 0, 0, 0, v & 0xFF, v >> 8, 0, 0])


def p_req_tm(tm_type: int) -> bytes:
    return bytes([tm_type, 0])


def angle_code(deg: float) -> int:
    """0.2° per LSB, two's complement, ±25°; 0x80 = no data / out of range."""
    if abs(deg) > 25:
        return 0x80
    return int(round(deg / 0.2)) & 0xFF


def p_angle(angx: float = 0.0, angy: float = 0.0) -> bytes:
    return bytes([angle_code(angx), angle_code(angy)])


def p_session(code: int, freq_n: int, atten_db: int = 0, coding: int = 0, polyn1: int = 0, polyn2: int = 0,
              continuous: bool = True, rate: int = 0, count: int = 2047, reg: int = 0) -> bytes:
    """Session (СР) command parameters, table 4.35.
    byte 0 + byte 1 bits 2-0: Freq N (11 bit), byte 1 bits 7-3: Atten_EIRP (1 dB),
    byte 2: Mode_Coding (7-6), PolynX.1 / preamble polynomial (5-3), PolynX.2 (2-0),
    byte 3: bit 0 Mode_Send (0 = continuous NeprRP, 1 = pulse ImpRP), bits 4-1 Rate,
    byte 3 bits 7-5 + byte 4: N (КБД count / preambles / ПООСШ length / carrier time in s, 11 bit),
    bytes 5-7: register start value / number of idle frames (24 bit)."""
    lo, hi = FREQ_N_RANGE.get(code, (0, 0x7FF))
    if not lo <= freq_n <= hi:
        raise ValueError(f"frequency code {freq_n} outside {lo}…{hi} for {COMMANDS[code][0]}")
    a = max(0, min(31, int(atten_db)))
    b = bytearray(8)
    b[0] = freq_n & 0xFF
    b[1] = ((freq_n >> 8) & 0x07) | (a << 3)
    b[2] = ((coding & 3) << 6) | ((polyn1 & 7) << 3) | (polyn2 & 7)
    count = max(0, min(2047, int(count)))
    b[3] = (0 if continuous else 1) | ((rate & 0x7) << 1) | ((count & 0x7) << 5)
    b[4] = count >> 3
    b[5:8] = (reg & 0xFFFFFF).to_bytes(3, "little")
    return bytes(b)


def parse_session(data: bytes) -> dict:
    d = bytes(data) + bytes(8)
    return {"freq_n": d[0] | (d[1] & 7) << 8, "atten_db": d[1] >> 3, "coding": d[2] >> 6,
            "polyn1": (d[2] >> 3) & 7, "polyn2": d[2] & 7, "continuous": not (d[3] & 1),
            "rate": (d[3] >> 1) & 7, "count": (d[3] >> 5) | (d[4] << 3), "reg": int.from_bytes(d[5:8], "little")}


# ---- acknowledgement ---------------------------------------------------------------------------
def parse_ack(can_id: int, ext: bool, data: bytes, sender: int):
    """Acknowledgement from the BOS to `sender`, or None."""
    if not ext or can_id >> 28 & 1 or (can_id >> 22) & 0x3F != BOS or (can_id >> 16) & 0x3F != sender:
        return None
    if not can_id >> 15 & 1:
        return None
    d = bytes(data) + bytes(8)
    kb = d[5]
    return {"code": can_id & 0xFF, "negative": bool(can_id >> 14 & 1), "byte": kb, "can2": bool(kb >> 7 & 1),
            "array": bool(kb >> 6 & 1), "unknown": bool(kb >> 3 & 1), "echo": d[:5]}


def ack_text(a: dict) -> str:
    name = COMMANDS.get(a["code"], (f"0x{a['code']:02X}",))[0]
    if not a["negative"]:
        return f"{name}: КВ+ (via CAN{2 if a['can2'] else 1})"
    why = "command does not exist" if a["unknown"] else "sender address not allowed / command not in the list"
    return f"{name}: КВ- ({why})"


# ---- КОНФИГ (table 4.4), 136 bytes; ИОК_КОНФИГ = 8-byte header + КОНФИГ ---------------------------
CONFIG_LEN = 136


def decode_config(cfg: bytes) -> dict:
    c = bytes(cfg)
    if len(c) < CONFIG_LEN:
        raise ValueError(f"КОНФИГ must be {CONFIG_LEN} bytes, got {len(c)}")
    u32 = lambda i: int.from_bytes(c[i:i + 4], "little")
    s8 = lambda v: v - 256 if v > 127 else v
    tm = (c[3] >> 5) & 7
    d = {
        "Config_Number": c[0], "Config_Nmb_Cmd": c[1], "Config_Type": c[2] >> 7 & 1,
        "Sour_10MHz": c[2] >> 6 & 1, "Data_Random": c[2] >> 5 & 1, "BPU_AFAR_mode": c[2] >> 2 & 3,
        "QuickOn_mode": c[2] & 3, "Temp_mode": tm, "Sour_Angle": c[3] >> 2 & 3, "Numb_BOS": c[3] >> 1 & 1,
        "Time_SNR": c[4], "Angle_ErdStation_Err": c[5], "Numb_Preamb": c[6] + 1,
        "ID_KA": int.from_bytes(c[7:9], "little") & 0x3FF, "Preamb": u32(9), "Registr_Preamb": u32(13),
        "Registr1_KBD": u32(49), "Registr2_KBD": u32(53),
        "Ttlm_main_FC": c[121], "Ttlm_dop_FB": c[122], "Ttlm_stat_FD": c[123], "Ttlm_conf_FA": c[124],
        "Temp_Max_DT1": s8(c[125]), "Temp_Max_DT2": s8(c[126]), "Temp_Max_DT3": s8(c[127]),
        "Checksum": int.from_bytes(c[134:136], "little"),
    }
    d["Polyn_Preamb"] = [u32(17 + 4 * k) for k in range(8)]
    d["Polyn_KBD"] = [(u32(57 + 8 * k), u32(61 + 8 * k)) for k in range(8)]
    calc = crc16(c[:134])
    d["crc_calc"] = calc
    d["crc_ok"] = calc == d["Checksum"] or crc16(c[:134]) == int.from_bytes(c[134:136], "big")
    return d


# ---- МД_СОСТОЯНИЕ (40 bytes) -----------------------------------------------------------------------
STATUS_FIELDS = [(0, 2, "Cnt_Config_Save"), (2, 2, "Cnt_Data_Save"), (4, 2, "Cnt_Alarm_Temp_All"),
                 (6, 2, "Cnt_Turn_ON"), (8, 3, "Cnt_SNR_Cmd_All"), (11, 3, "Cnt_SNR_Sgnl_All"),
                 (14, 3, "Cnt_SNR_BB_Sgnl_All"), (17, 4, "Cnt_TX_KBD_All"), (21, 4, "Cnt_TX_KBD_BB_All"),
                 (25, 4, "Work_Time"), (29, 4, "T_SNR_All"), (33, 4, "T_SNR_BB_All")]
