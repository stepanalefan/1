# -*- coding: utf-8 -*-
"""
bos_decode.py
=============
Decoding of the BOS downlink data (document 0202, section 4). Pure NumPy / Python.

УР (narrowband, BPSK ±90°, SP-L = Manchester-II, symbol rate = 2 × data rate), section 4.2:
    КБД = 1279 bytes: synchromarker 0x1ACFFC1D (4) + code block 1275 = transport frame 1115
    + 160 check bytes, Reed-Solomon (255,223) with interleaving depth I = 5, CCSDS 131.0-B-3
    (dual basis, field polynomial 0x187, first root 112, primitive element step 11). No randomizer.
Transport frame (1115 bytes, 4.5): CCSDS TM header, last two bytes CRC-16 (CCSDS 132.0-B-2:
    poly 0x1021, init 0xFFFF). Idle frame (table 4.4): byte 0-1 SCID / VCID 001 / OCF flag 0,
    byte 2 = 0x00, byte 3 = virtual channel frame counter, bytes 4-5 = 0x5F 0xFF, 6-17 = 0x00,
    bytes 18…1112 = (index & 0xFF).

What `decode_ur` reports: frames found, frame sync quality, RS corrections per frame, uncorrectable
frames, CRC, header fields, frame-counter continuity, idle-frame content check, and the channel
bit error rate (raw received code bits vs. the re-encoded corrected block).
"""

from __future__ import annotations

import numpy as np

ASM_UR = 0x1ACFFC1D
CBD_UR = 1279
TF_LEN = 1115
RS_N, RS_K, RS_I = 255, 223, 5

# ---------------------------------------------------------------------------------------- GF(256)
_GF_POLY, _FCR, _PRIM, _NROOTS = 0x187, 112, 11, 32
EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= _GF_POLY
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]
IPRIM = next(i for i in range(1, 256) if (i * _PRIM) % 255 == 1)     # 1/prim mod 255


def _mul(a, b):
    return 0 if a == 0 or b == 0 else EXP[LOG[a] + LOG[b]]


# Berlekamp dual basis <-> conventional (CCSDS 131.0-B, libfec gen_ccsds_tal.c)
_TAL = (0x8D, 0xEF, 0xEC, 0x86, 0xFA, 0x99, 0xAF, 0x7B)
TALTAB = [0] * 256
TAL1TAB = [0] * 256
for _i in range(256):
    v = 0
    for _j in range(8):
        for _k in range(8):
            if _i & (1 << _k):
                v ^= _TAL[7 - _k] & (1 << _j)
    TALTAB[_i] = v
    TAL1TAB[v] = _i

# generator polynomial, roots alpha^(prim*(fcr+i))
_GEN = [1]
for _i in range(_NROOTS):
    root = EXP[(_PRIM * (_FCR + _i)) % 255]
    ng = [0] * (len(_GEN) + 1)
    for j, c in enumerate(_GEN):
        ng[j] ^= c
        ng[j + 1] ^= _mul(c, root)
    _GEN = ng


def rs_encode(data223: bytes) -> bytes:
    """CCSDS RS(255,223) codeword (dual basis in and out)."""
    msg = [TAL1TAB[b] for b in data223]
    par = [0] * _NROOTS
    for b in msg:
        fb = b ^ par[0]
        par = par[1:] + [0]
        if fb:
            for j in range(_NROOTS):
                par[j] ^= _mul(fb, _GEN[j + 1])
    return bytes(data223) + bytes(TALTAB[p] for p in par)


def rs_decode(cw: bytes):
    """Decode one CCSDS RS(255,223) codeword. Returns (data223, n_corrected) or (data223 raw, -1)."""
    r = [TAL1TAB[b] for b in cw]
    synd = []
    for i in range(_NROOTS):
        s = 0
        a = EXP[(_PRIM * (_FCR + i)) % 255]
        for c in r:
            s = _mul(s, a) ^ c
        synd.append(s)
    if not any(synd):
        return bytes(cw[:RS_K]), 0
    # Berlekamp-Massey
    lam, b = [1], [1]
    L, m, bb = 0, 1, 1
    for n in range(_NROOTS):
        d = synd[n]
        for i in range(1, L + 1):
            if i < len(lam):
                d ^= _mul(lam[i], synd[n - i])
        if d == 0:
            m += 1
            continue
        coef = _mul(d, EXP[255 - LOG[bb]])
        t = lam + [0] * max(0, len(b) + m - len(lam))
        for i, bi in enumerate(b):
            t[i + m] ^= _mul(coef, bi)
        if 2 * L <= n:
            b, L, bb, m = lam, n + 1 - L, d, 1
        else:
            m += 1
        lam = t
    lam = lam[:L + 1]
    if L > _NROOTS // 2:
        return bytes(cw[:RS_K]), -1
    # Chien search: positions where lambda(X^-1) = 0, X = alpha^(prim * (254 - pos))
    errs = []
    for pos in range(RS_N):
        xinv_log = (-_PRIM * (RS_N - 1 - pos)) % 255
        s = 0
        for i, li in enumerate(lam):
            if li:
                s ^= EXP[(LOG[li] + i * xinv_log) % 255]
        if s == 0:
            errs.append(pos)
    if len(errs) != L:
        return bytes(cw[:RS_K]), -1
    # Forney: omega = S(x) lambda(x) mod x^nroots
    omega = [0] * _NROOTS
    for i in range(_NROOTS):
        for j, lj in enumerate(lam):
            if i + j < _NROOTS:
                omega[i + j] ^= _mul(synd[i], lj)
    for pos in errs:
        xl = (_PRIM * (RS_N - 1 - pos)) % 255           # log of X
        xinv = (-xl) % 255
        num = 0
        for i, o in enumerate(omega):
            if o:
                num ^= EXP[(LOG[o] + i * xinv) % 255]
        den = 0
        for i in range(1, len(lam), 2):                  # formal derivative: odd terms
            if lam[i]:
                den ^= EXP[(LOG[lam[i]] + (i - 1) * xinv) % 255]
        if den == 0:
            return bytes(cw[:RS_K]), -1
        e = EXP[(LOG[num] + xl * (1 - _FCR) - LOG[den]) % 255] if num else 0
        r[pos] ^= e
    return bytes(TALTAB[c] for c in r[:RS_K]), len(errs)


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


# ---------------------------------------------------------------------------------------- frames
def idle_frame(scid: int = 0, vcfc: int = 0) -> bytes:
    f = bytearray(TF_LEN)
    f[0] = (scid >> 4) & 0x3F
    f[1] = ((scid & 0x0F) << 4) | (0b001 << 1)
    f[2] = 0
    f[3] = vcfc & 0xFF
    f[4], f[5] = 0x5F, 0xFF
    for i in range(18, TF_LEN - 2):
        f[i] = i & 0xFF
    f[-2:] = crc16(bytes(f[:-2])).to_bytes(2, "big")
    return bytes(f)


def code_block(tf: bytes) -> bytes:
    """Interleaved RS (I = 5): codeword j takes bytes j, j+5, … of the block."""
    cws = [rs_encode(tf[j::RS_I]) for j in range(RS_I)]
    return bytes(cws[k % RS_I][k // RS_I] for k in range(RS_N * RS_I))


def cbd_ur(tf: bytes) -> bytes:
    return ASM_UR.to_bytes(4, "big") + code_block(tf)


def parse_tf(tf: bytes) -> dict:
    return {"scid": ((tf[0] & 0x3F) << 4) | (tf[1] >> 4), "vcid": (tf[1] >> 1) & 7, "ocf": tf[1] & 1,
            "mcfc": tf[2], "vcfc": tf[3], "fhp": ((tf[4] & 7) << 8) | tf[5],
            "idle": tf[4] == 0x5F and tf[5] == 0xFF,
            "crc_ok": crc16(tf[:-2]) == int.from_bytes(tf[-2:], "big")}


def bytes_to_bits(b: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(bytes(b), np.uint8))


def bits_to_bytes(b) -> bytes:
    return np.packbits(np.asarray(b, np.uint8)).tobytes()


# ---------------------------------------------------------------------------------------- SP-L
def manchester_encode(bits) -> np.ndarray:
    """SP-L (Manchester-II, ECSS-E-ST-50-05C): 1 -> (+1, -1), 0 -> (-1, +1)."""
    b = np.asarray(bits, np.int8) * 2 - 1
    return np.column_stack([b, -b]).ravel().astype(float)


def manchester_decode(sym):
    """Soft symbols (real) -> (hard bits, soft bit values, phase used, violation rate)."""
    s = np.asarray(sym, float)
    best = None
    for ph in (0, 1):
        n = (len(s) - ph) // 2
        a, b = s[ph:ph + 2 * n:2], s[ph + 1:ph + 2 * n:2]
        soft = (a - b) / 2
        viol = float(np.mean(np.sign(a) == np.sign(b))) if n else 1.0
        if best is None or viol < best[3]:
            best = ((soft > 0).astype(np.uint8), soft, ph, viol)
    return best


# ---------------------------------------------------------------------------------------- decoder
def _find_asm(bits, asm=ASM_UR, nbits=32, max_err=3):
    """Positions of the synchromarker (and inverted), allowing max_err bit errors."""
    pat = np.array([(asm >> (nbits - 1 - i)) & 1 for i in range(nbits)], np.int8) * 2 - 1
    x = np.asarray(bits, np.int8) * 2 - 1
    if len(x) < nbits:
        return [], []
    c = np.correlate(x.astype(float), pat.astype(float), mode="valid")
    thr = nbits - 2 * max_err
    return list(np.where(c >= thr)[0]), list(np.where(c <= -thr)[0])


def decode_ur(sym, scid=None) -> dict:
    """Decode УР КБД from demodulated BPSK symbols (real part, 2 symbols per bit)."""
    bits, soft, ph, viol = manchester_decode(sym)
    out = {"bits": len(bits), "manchester_violations": viol, "frames": [], "inverted": False}
    pos, neg = _find_asm(bits)
    if len(neg) > len(pos):                                # BPSK 180° ambiguity
        bits = 1 - bits
        pos, neg = neg, pos
        out["inverted"] = True
    out["asm_found"] = len(pos)
    nb = CBD_UR * 8
    raw_err = raw_tot = 0
    for p in pos:
        if p + nb > len(bits):
            break
        blk = bits_to_bytes(bits[p + 32:p + nb])
        tf = bytearray(TF_LEN)
        corr, bad = [], False
        for j in range(RS_I):
            data, n = rs_decode(blk[j::RS_I])
            corr.append(n)
            bad |= n < 0
            tf[j::RS_I] = data
        tf = bytes(tf)
        fr = {"pos": int(p), "rs": corr, "rs_ok": not bad, "asm_err": int(np.sum(
            bits[p:p + 32] != bytes_to_bits(ASM_UR.to_bytes(4, "big"))))}
        fr.update(parse_tf(tf))
        fr["tf"] = tf
        if not bad:                                        # channel BER: received vs re-encoded
            ref = bytes_to_bits(code_block(tf))
            e = int(np.sum(ref != bits[p + 32:p + nb]))
            raw_err += e
            raw_tot += len(ref)
            fr["raw_bit_err"] = e
            if fr["idle"]:
                exp = idle_frame(fr["scid"], fr["vcfc"])
                fr["idle_content_ok"] = tf[6:TF_LEN - 2] == exp[6:TF_LEN - 2]
        out["frames"].append(fr)
    fs = out["frames"]
    out["n_frames"] = len(fs)
    out["rs_fail"] = sum(not f["rs_ok"] for f in fs)
    out["rs_corrected"] = sum(sum(n for n in f["rs"] if n > 0) for f in fs)
    out["crc_fail"] = sum(f["rs_ok"] and not f["crc_ok"] for f in fs)
    out["idle_bad"] = sum(f.get("idle_content_ok") is False for f in fs)
    out["idle_checked"] = sum(f.get("idle_content_ok") is not None for f in fs)
    out["raw_bits"] = raw_tot
    out["ber"] = raw_err / raw_tot if raw_tot else float("nan")
    out["ber_upper"] = (3.0 / raw_tot) if raw_tot and raw_err == 0 else float("nan")   # 95 % with 0 errors
    vc = [f["vcfc"] for f in fs if f["rs_ok"]]
    out["vcfc_gaps"] = sum(((b - a) & 0xFF) != 1 for a, b in zip(vc, vc[1:]))
    out["scid"] = fs[0]["scid"] if fs and fs[0]["rs_ok"] else None
    out["scid_ok"] = scid is None or out["scid"] is None or out["scid"] == scid
    return out
