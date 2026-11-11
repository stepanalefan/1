# -*- coding: utf-8 -*-
"""
bos_shr.py
==========
ШР (wideband, spread-spectrum OQPSK) downlink of the BOS: transmitter model and receiver.
Document 0202, sections 4.1, 4.3, 6 and appendices Г / Д. Pure NumPy.

Packet (fig. 4.2 c): n × preamble (32 bit, default 0xC6DFA611) + end-of-preamble marker 0xDA2B1E5E,
both at R/4 and spread by PRS 3 (poly 0x00008805, x16+x12+x3+x+1, reset to its initial state at the
start of every bit), then КБД at R, spread by PRS 1 ⊕ PRS 2 (0x00800043 / 0x00400010, initial
states 1 / 1) generated continuously - not reset at КБД boundaries.
КБД (4.3): synchromarker 0x034776C7272895B0 (64 bit) + code word 2231 bytes = transport frame
(1115 bytes) turbo coded r = 1/2 (CCSDS 131.0-B-3, k = 8920) and randomised (CCSDS 131.0-B-3
section 9, h(x) = x^8+x^7+x^5+x^3+1, all ones).
Chips at 262.144 Mchip/s, OQPSK at 131.072 Msym/s: chip 2n on I, chip 2n+1 on Q (Q half a symbol
later). Chips per bit (table 6.1): 262.144 M / R, preamble × 4.

ASSUMPTIONS to confirm with a real capture (the document does not fix them; all are parameters):
* LFSR form: polynomial word bit (k-1) = term x^k; Fibonacci register shifting right, output = bit 0,
  feedback into the top bit from the taps (this form gives the maximal period for 0x8805)
* chip 0 of each bit on I; data bit 1 -> symbol -1 (bit XOR chip, 0 -> +1)
* the КБД PRS starts with the first bit of the first КБД (after the marker)
* turbo code tail / puncturing as CCSDS 131.0-B-3 rate 1/2 (systematic + alternating parities)

Long captures are processed by a chip-level receiver (`chips_from_iq`) that needs ≥ 4 samples per
symbol; the despreading itself is pure correlation, so the receiver works at strongly negative chip
SNR as well (processing gain 10·log10(chips per bit)).
"""

from __future__ import annotations

import math

import numpy as np

import bos_decode as bd
import iq_demod

CHIP_RATE = 262.144e6
SYMBOL_RATE = CHIP_RATE / 2
ALPHA = 0.25
PREAMBLE = 0xC6DFA611
EOP = 0xDA2B1E5E
ASM_SR = 0x034776C7272895B0
POLY1, POLY2, POLY3 = 0x00800043, 0x00400010, 0x00008805
INIT1, INIT2, INIT3 = 1, 1, 1
K_INFO = 8920                                   # 1115 bytes
N_CODE = 2 * (K_INFO + 4)                       # 17848 bits = 2231 bytes
CBD_SR_BITS = 64 + N_CODE                       # 2239 bytes


def chips_per_bit(rate_kbps: float, preamble=False) -> int:
    n = int(round(CHIP_RATE / (rate_kbps * 1e3)))
    return 4 * n if preamble else n


# ------------------------------------------------------------------------------------------- LFSR
def _deg(poly):
    return poly.bit_length()


def _mask(poly):
    d = _deg(poly)
    m = 0
    for k in range(1, d + 1):
        if poly >> (k - 1) & 1:
            m |= 1 << (d - k)
    return m


def lfsr(poly: int, init: int, n: int) -> np.ndarray:
    """n output bits (uint8) of the LFSR, vectorised over GF(2) matrix powers."""
    d = _deg(poly)
    mask = _mask(poly)
    init &= (1 << d) - 1
    if n <= 0:
        return np.zeros(0, np.uint8)
    if init == 0:
        return np.zeros(n, np.uint8)
    # state transition as a d×d matrix over GF(2): new = A @ old
    A = np.zeros((d, d), np.uint8)
    for j in range(d):                              # column j = image of unit vector e_j
        s = 1 << j
        fb = bin(s & mask).count("1") & 1
        ns = (s >> 1) | (fb << (d - 1))
        for i in range(d):
            A[i, j] = ns >> i & 1
    B = 4096
    # M: outputs of B steps from a state (row t = bit 0 of A^t)
    M = np.zeros((B, d), np.uint8)
    P = np.eye(d, dtype=np.uint8)
    for t in range(B):
        M[t] = P[0]
        P = (A.astype(np.int64) @ P) % 2
        P = P.astype(np.uint8)
    AB = P                                          # A^B
    nb = (n + B - 1) // B
    st = np.array([init >> i & 1 for i in range(d)], np.uint8)
    states = np.zeros((nb, d), np.uint8)
    for k in range(nb):
        states[k] = st
        st = (AB.astype(np.int64) @ st) % 2
    out = (states.astype(np.int64) @ M.T.astype(np.int64)) % 2
    return out.ravel()[:n].astype(np.uint8)


def randomizer(n: int) -> np.ndarray:
    """CCSDS pseudo-randomizer (h(x) = x^8+x^7+x^5+x^3+1, all-ones start): FF 48 0E C0 9A 0D 70 BC …"""
    a = [1] * 8
    while len(a) < 255:
        k = len(a) - 8
        a.append(a[k + 7] ^ a[k + 5] ^ a[k + 3] ^ a[k])
    return np.resize(np.array(a[:255], np.uint8), n)


# ------------------------------------------------------------------------------------------- turbo
_PRIMES = (31, 37, 43, 47, 53, 59, 61, 67)


def turbo_perm(k=K_INFO, k1=8):
    """CCSDS turbo interleaver π (0-based): input position of output s."""
    k2 = k // k1
    pi = np.zeros(k, np.int64)
    for s in range(1, k + 1):
        m = (s - 1) % 2
        i = (s - 1) // (2 * k2)
        j = (s - 1) // 2 - i * k2
        t = (19 * i + 1) % (k1 // 2)
        q = t % 8 + 1
        c = (_PRIMES[q - 1] * j + 21 * m) % k2
        pi[s - 1] = 2 * (t + c * (k1 // 2) + 1) - m - 1
    return pi


_PI = None


def _perm():
    global _PI
    if _PI is None:
        _PI = turbo_perm()
    return _PI


# constituent RSC, 16 states: feedback G0 = 1+D^3+D^4, parity G1 = 1+D+D^3+D^4
_G0, _G1 = 0b10011, 0b11011                         # bit i = coefficient of D^i


def _rsc_tables():
    nxt = np.zeros((16, 2), np.int64)
    par = np.zeros((16, 2), np.int64)
    for s in range(16):                            # s bit i = register cell D^(i+1)
        for u in (0, 1):
            fb = u
            for i in range(1, 5):
                if _G0 >> i & 1:
                    fb ^= s >> (i - 1) & 1
            reg = (s << 1 | fb) & 0x1F             # bit 0 = a_k, bit i = a_{k-i}
            p = 0
            for i in range(5):
                if _G1 >> i & 1:
                    p ^= reg >> i & 1
            nxt[s, u] = reg & 0xF
            par[s, u] = p
    return nxt, par


_NXT, _PAR = _rsc_tables()


def _rsc_encode(u):
    s = 0
    par = np.zeros(len(u) + 4, np.uint8)
    sysb = np.zeros(len(u) + 4, np.uint8)
    for k, b in enumerate(u):
        sysb[k] = b
        par[k] = _PAR[s, b]
        s = _NXT[s, b]
    for k in range(4):                             # termination: input = feedback -> zero state
        fbk = 0
        for i in range(1, 5):
            if _G0 >> i & 1:
                fbk ^= s >> (i - 1) & 1
        b = fbk
        sysb[len(u) + k] = b
        par[len(u) + k] = _PAR[s, b]
        s = _NXT[s, b]
    return sysb, par


def turbo_encode(info_bits) -> np.ndarray:
    u = np.asarray(info_bits, np.uint8)
    sa, pa = _rsc_encode(u)
    sb, pb = _rsc_encode(u[_perm()])
    k = len(u)
    out = np.zeros(2 * (k + 4), np.uint8)
    out[0:2 * k:2] = sa[:k]
    par = np.where(np.arange(k) % 2 == 0, pa[:k], pb[:k])
    out[1:2 * k:2] = par
    # tail: encoder a (systematic, parity) for 2 steps, encoder b for 2 steps (all 4 tails kept as
    # (0, 1) pairs of the respective encoder - CCSDS rate 1/2 termination, assumption)
    t = []
    for j in range(4):
        t += [sa[k + j], pa[k + j]] if j % 2 == 0 else [sb[k + j], pb[k + j]]
    out[2 * k:] = t
    return out


def _bcjr(Ls, Lp, La):
    """Max-log-MAP for the RSC. Ls/Lp/La: LLRs (positive = bit 0) of systematic, parity, a priori.
    Returns extrinsic LLRs for the information bits."""
    n = len(Ls)
    NEG = -1e9
    xs = np.array([1.0, -1.0])                     # bit 0 -> +1
    gam = np.zeros((n, 16, 2))
    for u in (0, 1):
        gam[:, :, u] = 0.5 * xs[u] * (Ls + La)[:, None] + 0.5 * Lp[:, None] * (1 - 2 * _PAR[:, u])[None, :]
    alpha = np.full((n + 1, 16), NEG)
    alpha[0, 0] = 0.0
    for k in range(n):
        a = np.full(16, NEG)
        for u in (0, 1):
            v = alpha[k] + gam[k, :, u]
            np.maximum.at(a, _NXT[:, u], v)
        alpha[k + 1] = a - a.max()
    beta = np.full(16, NEG)
    beta[0] = 0.0
    out = np.zeros(n)
    for k in range(n - 1, -1, -1):
        m0 = alpha[k] + gam[k, :, 0] + beta[_NXT[:, 0]]
        m1 = alpha[k] + gam[k, :, 1] + beta[_NXT[:, 1]]
        out[k] = m0.max() - m1.max()
        b = np.maximum(gam[k, :, 0] + beta[_NXT[:, 0]], gam[k, :, 1] + beta[_NXT[:, 1]])
        beta = b - b.max()
    return out - Ls - La


def turbo_decode(llr, iters=8):
    """llr: soft code bits (positive = bit 0), length 2(k+4). Returns hard info bits."""
    llr = np.asarray(llr, float)
    k = (len(llr) // 2) - 4
    pi = _perm()
    Ls = np.zeros(k + 4)
    Lpa = np.zeros(k + 4)
    Lpb = np.zeros(k + 4)
    Ls[:k] = llr[0:2 * k:2]
    par = llr[1:2 * k:2]
    even = np.arange(k) % 2 == 0
    Lpa[:k][even] = par[even]
    Lpb[:k][~even] = par[~even]
    tl = llr[2 * k:]
    Lsa_t, Lsb_t = np.zeros(4), np.zeros(4)
    for j in range(4):
        if j % 2 == 0:
            Lsa_t[j], Lpa[k + j] = tl[2 * j], tl[2 * j + 1]
        else:
            Lsb_t[j], Lpb[k + j] = tl[2 * j], tl[2 * j + 1]
    Lsa = np.r_[Ls[:k], Lsa_t]
    Lsb = np.r_[Ls[:k][pi], Lsb_t]
    ext_b = np.zeros(k)
    for _ in range(iters):
        ea = _bcjr(Lsa, Lpa, np.r_[ext_b, np.zeros(4)])[:k]
        eb = _bcjr(Lsb, Lpb, np.r_[ea[pi], np.zeros(4)])[:k]
        ext_b = np.zeros(k)
        ext_b[pi] = eb
    post = Ls[:k] + ea + ext_b
    return (post < 0).astype(np.uint8)


# ------------------------------------------------------------------------------------------- TX
def _word_bits(v, n):
    return np.array([(v >> (n - 1 - i)) & 1 for i in range(n)], np.uint8)


def cbd_sr_bits(tf: bytes) -> np.ndarray:
    code = turbo_encode(bd.bytes_to_bits(tf)) ^ randomizer(N_CODE)
    return np.r_[_word_bits(ASM_SR, 64), code]


def packet_chips(rate_kbps, n_pre=1, tfs=(), preamble=PREAMBLE, prs=None):
    """±1 chips of one ШР packet: preambles + marker (PRS 3, reset per bit) + КБД (PRS 1 ⊕ 2)."""
    sfp, sf = chips_per_bit(rate_kbps, True), chips_per_bit(rate_kbps)
    p3 = lfsr(POLY3, INIT3, sfp)
    hdr = np.r_[np.tile(_word_bits(preamble, 32), n_pre), _word_bits(EOP, 32)]
    parts = [(np.repeat(hdr, sfp) ^ np.tile(p3, len(hdr)))]
    if tfs:
        data = np.concatenate([cbd_sr_bits(tf) for tf in tfs])
        c12 = prs if prs is not None else (lfsr(POLY1, INIT1, len(data) * sf) ^ lfsr(POLY2, INIT2, len(data) * sf))
        parts.append(np.repeat(data, sf) ^ c12[:len(data) * sf])
    bits = np.concatenate(parts)
    return 1.0 - 2.0 * bits


def oqpsk_waveform(chips, sps=4, alpha=ALPHA):
    """Chips -> OQPSK baseband at sps samples per symbol (RRC), unit power."""
    c = np.asarray(chips, float)
    if len(c) % 2:
        c = np.r_[c, 1.0]
    i, q = c[0::2], c[1::2]
    n = len(i) * sps
    up = np.zeros(n + sps, complex)
    up[:n:sps] += i
    up[sps // 2:n + sps // 2:sps] += 1j * q
    h = iq_demod.rrc_taps(alpha, sps)
    s = np.convolve(up, h)[len(h) // 2:len(h) // 2 + n + sps]
    return (s / math.sqrt(np.mean(np.abs(s) ** 2))).astype(np.complex64)


# ------------------------------------------------------------------------------------------- RX
def chips_from_iq(x, fs, sr=SYMBOL_RATE, alpha=ALPHA, block=4096):
    """Chip-level OQPSK receiver for long captures. Returns soft chips (I0, Q0, I1, Q1, …),
    up to the 4-fold phase / 1-chip ambiguity resolved later by the preamble search."""
    x = np.asarray(x, np.complex64)
    if abs(fs / sr - round(fs / sr)) > 1e-6 or round(fs / sr) < 4:
        import bos_analysis as ba
        x, fs = ba.resample(x, fs, 4 * sr)
        x = x.astype(np.complex64)
    sps = int(round(fs / sr))
    n0 = min(len(x), 1 << 18)
    f1 = iq_demod._coarse_cfo(x[:n0], fs, 4)
    x = x * np.exp(-2j * np.pi * f1 / fs * np.arange(len(x))).astype(np.complex64)
    h = iq_demod.rrc_taps(alpha, sps).astype(np.float32)
    y = np.convolve(x, h, mode="same").astype(np.complex64)
    y /= np.float32(math.sqrt(float(np.mean(np.abs(y[:n0]) ** 2))) or 1.0)
    # timing per block (squared OQPSK: real² - imag² carries the symbol clock line)
    e = y.real ** 2 - y.imag ** 2
    blk = block * sps
    nb = max(1, len(e) // blk)
    rot = np.exp(-2j * np.pi * np.arange(blk) / sps)
    taus = []
    for b in range(nb):
        seg = e[b * blk:(b + 1) * blk]
        taus.append(-np.angle(np.sum(seg * rot[:len(seg)])) / (2 * np.pi) * sps)
    taus = np.unwrap(np.array(taus) * 2 * np.pi / sps) * sps / (2 * np.pi)
    cents = (np.arange(nb) + 0.5) * blk
    nsym = int((len(y) - 2 * sps) // sps)
    k = np.arange(nsym)
    tau = np.interp(k * sps, cents, taus) if nb > 1 else np.full(nsym, taus[0])
    t = k * sps + tau
    t = t[(t >= 0) & (t < len(y) - sps)]
    grid = np.arange(len(y))

    def at(tt):
        return np.interp(tt, grid, y.real) + 1j * np.interp(tt, grid, y.imag)
    z = at(t).real + 1j * at(t + sps / 2).imag
    # carrier phase per block (power 4 vs the 45° QPSK reference), unwrapped
    pb = 1024
    nzb = max(1, len(z) // pb)
    ph = [np.angle(-np.mean(z[b * pb:(b + 1) * pb] ** 4)) for b in range(nzb)]
    ph = np.unwrap(np.array(ph)) / 4
    zc = (np.arange(nzb) + 0.5) * pb
    phase = np.interp(np.arange(len(z)), zc, ph) if nzb > 1 else np.full(len(z), ph[0])
    z = z * np.exp(-1j * phase)
    chips = np.empty(2 * len(z), np.float32)
    chips[0::2] = z.real
    chips[1::2] = z.imag
    return chips, {"cfo_coarse": f1, "fs": fs, "sps": sps}


def _corr_fft(a, b):
    """Valid cross-correlation of a with template b (FFT)."""
    n = len(a) + len(b)
    nf = 1 << int(math.ceil(math.log2(n)))
    A = np.fft.rfft(a, nf)
    B = np.fft.rfft(b[::-1], nf)
    return np.fft.irfft(A * B, nf)[len(b) - 1:len(a)]


def _find_bits(bits, word, nbits, max_err=2):
    pat = _word_bits(word, nbits).astype(int) * 2 - 1
    x = bits.astype(int) * 2 - 1
    if len(x) < nbits:
        return []
    c = np.correlate(x, pat, "valid")
    return list(np.where(c >= nbits - 2 * max_err)[0])


def decode_shr(chips, rate_kbps, preamble=PREAMBLE, max_cbd=4, turbo_iters=8) -> dict:
    """Despread a ШР packet from soft chips: preambles + marker, then КБД (turbo decoded)."""
    out = {"rate": rate_kbps, "preamble_found": 0, "marker": False, "frames": []}
    c = np.asarray(chips, np.float32)
    sfp, sf = chips_per_bit(rate_kbps, True), chips_per_bit(rate_kbps)
    p3 = 1.0 - 2.0 * lfsr(POLY3, INIT3, sfp).astype(np.float32)
    # preamble region: first (n_pre + 1) * 32 bits, search over the first ~64 preamble bits
    span = min(len(c), 72 * sfp)
    if span < 2 * sfp:
        out["error"] = "capture too short for one preamble bit"
        return out
    # rail-separated correlation: chip index parity = rail; signs per rail unknown
    pI = p3.copy()
    pI[1::2] = 0
    pQ = p3.copy()
    pQ[0::2] = 0
    best = None
    for par in (0, 1):                             # which chip parity is "I" for the template
        tI, tQ = (pI, pQ) if par == 0 else (pQ, pI)
        cI = _corr_fft(c[:span], tI)
        cQ = _corr_fft(c[:span], tQ)
        nbit = (len(cI) - 1) // sfp
        if nbit < 2:
            continue
        for sQ in (1.0, -1.0):
            comb = cI + sQ * cQ
            # energy per offset (mod sfp) over all bits
            e = np.zeros(sfp)
            for b in range(nbit):
                e += comb[b * sfp:(b + 1) * sfp] ** 2
            o = int(np.argmax(e))
            if best is None or e[o] > best[0]:
                best = (e[o], o, par, sQ, comb)
    _, o, par, sQ, comb = best
    # whole packet header bits at offset o (continue past the search span)
    nh = (len(c) - o) // sfp
    nh = min(nh, 400)
    tI, tQ = (pI, pQ) if par == 0 else (pQ, pI)
    soft = np.array([float(np.dot(c[o + b * sfp:o + (b + 1) * sfp], tI + sQ * tQ)) for b in range(nh)])
    hb = (soft < 0).astype(np.uint8)
    pre = _find_bits(hb, preamble, 32)
    inv = False
    if not pre:
        hb = 1 - hb
        pre = _find_bits(hb, preamble, 32)
        inv = True
    out["preamble_found"] = len(pre)
    out["chip_offset"] = o
    eop = [p for p in _find_bits(hb, EOP, 32) if not pre or p >= pre[0]]
    if pre:
        end = (eop[0] + 32) if eop else min(len(soft), pre[-1] + 32)
        s = np.abs(soft[pre[0]:end])
        out["despread_snr_db"] = 10 * math.log10(np.mean(s) ** 2 / (np.var(s) + 1e-30))
        chip_e = float(np.mean(np.abs(c[o + pre[0] * sfp:o + end * sfp]))) * sfp
        out["despread_eff"] = float(np.mean(s)) / chip_e if chip_e > 0 else float("nan")   # 1 = PRS matches
        out["preamble_value"] = f"0x{preamble:08X}"
        out["preamble_bits_ok"] = len(pre)
    if not eop:
        return out
    out["marker"] = True
    start_bit = eop[0] + 32
    start = o + start_bit * sfp                    # first КБД chip
    rail_sign = np.ones(2, np.float32)
    rail_sign[(start + (1 if par == 0 else 0)) % 2] = sQ    # Q-rail sign at absolute parity
    sgn = -1.0 if inv else 1.0
    nbits = min((len(c) - start) // sf, max_cbd * CBD_SR_BITS)
    out["cbd_bits"] = int(nbits)
    if nbits < CBD_SR_BITS:
        out["note"] = f"capture ends {CBD_SR_BITS - nbits} bits before the end of the first КБД"
        return out
    prs = lfsr(POLY1, INIT1, nbits * sf) ^ lfsr(POLY2, INIT2, nbits * sf)
    seg = c[start:start + nbits * sf] * (1.0 - 2.0 * prs.astype(np.float32))
    seg = seg * np.tile(rail_sign[[(start + i) % 2 for i in (0, 1)]], nbits * sf // 2 + 1)[:nbits * sf] * sgn
    sb = seg.reshape(nbits, sf).sum(axis=1)
    out["cbd_snr_db"] = 10 * math.log10(np.mean(np.abs(sb)) ** 2 / (np.var(np.abs(sb)) + 1e-30))
    out["cbd_eff"] = float(np.mean(np.abs(sb))) / (float(np.mean(np.abs(seg))) * sf + 1e-30)
    hard = (sb < 0).astype(np.uint8)
    out["bitstream"] = bd.bits_to_bytes(hard[:len(hard) // 8 * 8])        # despread КБД bits, for export
    err_tot = bits_tot = 0
    pos = 0
    while pos + CBD_SR_BITS <= nbits:
        asm_err = int(np.sum(hard[pos:pos + 64] != _word_bits(ASM_SR, 64)))
        code_soft = sb[pos + 64:pos + CBD_SR_BITS] * (1.0 - 2.0 * randomizer(N_CODE))
        scale = 2 * np.mean(np.abs(code_soft)) / (np.var(np.abs(code_soft)) + 1e-12)
        info = turbo_decode(code_soft * scale, turbo_iters)
        tf = bd.bits_to_bytes(info)
        fr = {"asm_err": asm_err}
        fr.update(bd.parse_tf(tf))
        fr["tf"] = tf
        ref = turbo_encode(info)
        e = int(np.sum(ref != (code_soft < 0)))
        fr["raw_bit_err"] = e
        err_tot += e
        bits_tot += N_CODE
        if fr["idle"]:
            exp = bd.idle_frame(fr["scid"], fr["vcfc"])
            fr["idle_content_ok"] = tf[6:bd.TF_LEN - 2] == exp[6:bd.TF_LEN - 2]
        out["frames"].append(fr)
        pos += CBD_SR_BITS
    fs_ = out["frames"]
    out["n_frames"] = len(fs_)
    out["crc_fail"] = sum(not f["crc_ok"] for f in fs_)
    out["idle_bad"] = sum(f.get("idle_content_ok") is False for f in fs_)
    out["ber"] = err_tot / bits_tot if bits_tot else float("nan")
    out["raw_bits"] = bits_tot
    vc = [f["vcfc"] for f in fs_ if f["crc_ok"]]
    out["vcfc_gaps"] = sum(((b - a) & 0xFF) != 1 for a, b in zip(vc, vc[1:]))
    out["scid"] = fs_[0]["scid"] if fs_ and fs_[0]["crc_ok"] else None
    return out
