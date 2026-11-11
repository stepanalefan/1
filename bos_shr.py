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

Long captures are processed by a chip-level receiver (`chips_from_iq`) streamed in blocks at the file's
own sample rate (e.g. 275 MS/s, parallel FFT threads, cubic interpolation); the despreading itself is pure correlation, so the receiver works at strongly negative chip
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
    AB = P.astype(np.int64)                         # A^B
    nb = (n + B - 1) // B
    st = np.array([init >> i & 1 for i in range(d)], np.int64)
    states = np.zeros((nb, d), np.uint8)
    for k in range(nb):
        states[k] = st
        st = (AB @ st) % 2
    # outputs in row blocks (uint8): long sequences (10^8 chips) without a full int64 matrix
    out = np.empty(nb * B, np.uint8)
    Mt = M.T.astype(np.float32)                    # exact: sums ≤ d
    for r in range(0, nb, 2048):
        e = min(nb, r + 2048)
        out[r * B:e * B] = ((states[r:e].astype(np.float32) @ Mt).astype(np.int32) & 1).ravel()
    return out[:n]


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


def _pred_tables():
    """For each next state: its two (previous state, input) transitions."""
    pp = [[] for _ in range(16)]
    for s in range(16):
        for u in (0, 1):
            pp[_NXT[s, u]].append((s, u))
    P = np.array([[p[0][0], p[1][0]] for p in pp])
    U = np.array([[p[0][1], p[1][1]] for p in pp])
    return P, U


_PRV, _PRU = _pred_tables()


def _bcjr(Ls, Lp, La):
    """Max-log-MAP for the RSC. Ls/Lp/La: LLRs (positive = bit 0) of systematic, parity, a priori.
    Returns extrinsic LLRs for the information bits."""
    n = len(Ls)
    NEG = -1e9
    xs = np.array([1.0, -1.0])                     # bit 0 -> +1
    gam = np.zeros((n, 16, 2))
    for u in (0, 1):
        gam[:, :, u] = 0.5 * xs[u] * (Ls + La)[:, None] + 0.5 * Lp[:, None] * (1 - 2 * _PAR[:, u])[None, :]
    # branch metrics into each next state from its two predecessors
    g0 = gam[:, _PRV[:, 0], _PRU[:, 0]]
    g1 = gam[:, _PRV[:, 1], _PRU[:, 1]]
    p0, p1 = _PRV[:, 0], _PRV[:, 1]
    alpha = np.empty((n + 1, 16))
    a = np.full(16, NEG)
    a[0] = 0.0
    alpha[0] = a
    mx = np.maximum
    for k in range(n):
        a = mx(a[p0] + g0[k], a[p1] + g1[k])
        a -= a.max()
        alpha[k + 1] = a
    n0, n1 = _NXT[:, 0], _NXT[:, 1]
    gk0, gk1 = gam[:, :, 0], gam[:, :, 1]
    beta = np.full(16, NEG)
    beta[0] = 0.0
    m0 = np.empty((n, 16))
    m1 = np.empty((n, 16))
    for k in range(n - 1, -1, -1):
        b0 = gk0[k] + beta[n0]
        b1 = gk1[k] + beta[n1]
        m0[k] = b0
        m1[k] = b1
        beta = mx(b0, b1)
        beta -= beta.max()
    m0 += alpha[:n]
    m1 += alpha[:n]
    out = m0.max(axis=1) - m1.max(axis=1)
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
def rrc_response(f, sr, alpha):
    """|H(f)| of the root-raised-cosine filter (unity in the flat part)."""
    af = np.abs(f)
    f1, f2 = (1 - alpha) * sr / 2, (1 + alpha) * sr / 2
    h = np.zeros(len(f), np.float32)
    h[af <= f1] = 1.0
    m = (af > f1) & (af < f2)
    h[m] = np.sqrt(0.5 * (1 + np.cos(np.pi / (alpha * sr) * (af[m] - f1))))
    return h


def _workers():
    import os
    return max(1, min(8, (os.cpu_count() or 2)))


def chips_from_iq(x, fs, sr=SYMBOL_RATE, alpha=ALPHA, block=4096, progress=None, cancel=None):
    """Chip-level OQPSK receiver for long captures, streamed in blocks at the file's own sample rate
    (any rate with ≥ ~1.6 samples per symbol, e.g. 275 MS/s; upsampled ×2 in the frequency domain when
    below 3 samples per symbol). Memory: the soft chips (8 bytes per symbol) plus a few blocks.
    FFT blocks run in parallel threads. Returns soft chips (I0, Q0, I1, Q1, …), up to the 4-fold phase /
    1-chip ambiguity resolved later by the preamble search."""
    from concurrent.futures import ThreadPoolExecutor
    n = len(x)
    if fs < 1.25 * sr:
        raise ValueError(f"sample rate {fs / 1e6:g} MS/s too low for ШР ({sr / 1e6:g} Msym/s): "
                         f"needs ≥ {1.25 * sr / 1e6:.0f} MS/s")
    up = 1 if fs / sr >= 3 else 2                        # interpolation needs ~≥ 3 samples per symbol
    fsu = fs * up
    sps = fsu / sr
    n0 = min(n, 1 << 18)
    # coarse frequency and level from the strongest of up to 32 blocks (not the noise before the RF onset)
    st = np.unique(np.linspace(0, n - n0, min(32, max(1, n // n0))).astype(np.int64))
    p0 = [float(np.mean(np.abs(np.asarray(x[s:s + 4096], np.complex64)) ** 2)) for s in st]
    s0 = int(st[int(np.argmax(p0))])
    x0 = np.asarray(x[s0:s0 + n0], np.complex64)
    f1 = iq_demod._coarse_cfo(x0, fs, 4)
    lvl = float(np.mean(np.abs(x0) ** 2))
    # block layout (input samples): FFT length L, valid V in the middle, margin M either side
    L = 1 << 21
    M = 8192
    while L - 2 * M < 4 * block * fs / sr and L < (1 << 24):
        L <<= 1
    blk = int(round(block * sps))                        # timing block, in output samples
    Vu = ((L - 2 * M) * up) // blk * blk                 # valid output samples per chunk (whole blocks)
    V = Vu // up
    Vu = V * up
    blk_per = Vu // blk
    H = rrc_response(np.fft.fftfreq(L, 1 / fs), sr, alpha)
    nch = max(1, -(-n // V))
    ph_step = f1 / fs

    def front(c):
        """chunk c -> matched-filtered y on [c·Vu, c·Vu + Vu + tail) output samples + its timing phasors."""
        a = c * V - M
        seg = np.zeros(L, np.complex64)
        s0, s1 = max(0, a), min(n, a + L)
        if s1 > s0:
            idx = np.arange(s0, s1, dtype=np.float64)
            seg[s0 - a:s1 - a] = np.asarray(x[s0:s1], np.complex64) * \
                np.exp(-2j * np.pi * ((ph_step * idx) % 1.0)).astype(np.complex64)
        S = np.fft.fft(seg) * H
        if up > 1:                                       # zero-pad the spectrum: exact ×up interpolation
            h = L // 2
            S = np.concatenate([S[:h], np.zeros(L * (up - 1), S.dtype), S[h:]]) * up
        y = np.fft.ifft(S).astype(np.complex64)
        y = y[M * up:M * up + Vu + 16 * up]             # valid part + a short tail for interpolation
        # timing: the clock line of real² - imag² (phase vs. the global grid). That line depends on the
        # carrier phase θ (it fades at 45° and flips sign beyond: a half-symbol slip), so per block the
        # sums for y² and conj(y²) are kept and combined later with θ from y⁴:
        # Σ Re(y² e^{-2jθ})·rot = (e^{-2jθ} Σ y²·rot + e^{2jθ} Σ conj(y²)·rot) / 2
        yv = y[:Vu]
        y2 = yv * yv
        g = c * Vu + np.arange(Vu, dtype=np.float64)
        rot = np.exp(-2j * np.pi * ((g / sps) % 1.0)).astype(np.complex64)
        A = (y2 * rot).reshape(blk_per, blk).sum(axis=1)
        B = (np.conj(y2) * rot).reshape(blk_per, blk).sum(axis=1)
        m4 = (y2 * y2).reshape(blk_per, blk).sum(axis=1)
        return y, (A, B, m4)

    nsym = int(n * up / sps) + 2
    z = np.empty(nsym, np.complex64)
    kz = 0                                               # output position
    kn = 0                                               # next symbol index (time grid k·sps + tau)
    cents, taus = [], []
    prev_y = None
    prev_g0 = 0
    scale = None
    last_ang = None
    last_a4 = None

    def interp(v, t):
        """Catmull-Rom cubic interpolation of the real array v at fractional positions t."""
        i = t.astype(np.int64)
        u = (t - i).astype(np.float32)
        p0, p1, p2, p3 = v[i - 1], v[i], v[i + 1], v[i + 2]
        return p1 + 0.5 * u * (p2 - p0 + u * (2 * p0 - 5 * p1 + 4 * p2 - p3 + u * (3 * (p1 - p2) + p3 - p0)))

    def sample(buf, lt, k0):
        """I at t, Q half a symbol later, into z[k0:…] (runs in a worker thread)."""
        z.real[k0:k0 + len(lt)] = interp(buf.real.copy(), lt) * scale
        z.imag[k0:k0 + len(lt)] = interp(buf.imag.copy(), lt + sps / 2) * scale

    with ThreadPoolExecutor(_workers()) as ex:
        futs = {}
        jobs = []
        nxt = 0
        for c in range(nch):
            while nxt < nch and nxt < c + 2 * _workers():
                futs[nxt] = ex.submit(front, nxt)
                nxt += 1
            if cancel and cancel():
                raise RuntimeError("cancelled")
            y, (A, B, m4) = futs.pop(c).result()
            g0 = c * Vu
            if scale is None:
                scale = np.float32(1.0 / (math.sqrt(lvl) or 1.0))
            # carrier phase per block (4θ from y⁴, unwrapped across chunks), then the phase-free clock line
            a4 = np.angle(-m4)                          # ±1±j chips: y⁴ = -4 when aligned
            a4 = np.unwrap(np.r_[last_a4, a4])[1:] if last_a4 is not None else np.unwrap(a4)
            last_a4 = a4[-1]
            th2 = np.exp(-0.5j * a4)                    # e^{-2jθ}, θ = a4 / 4 (0 when aligned)
            ph = 0.5 * (th2 * A + np.conj(th2) * B)
            ang = np.angle(ph)
            if last_ang is not None:
                ang = np.unwrap(np.r_[last_ang, ang])[1:]
            else:
                ang = np.unwrap(ang)
            last_ang = ang[-1]
            cents.extend(g0 + (np.arange(blk_per) + 0.5) * blk)
            taus.extend(-ang / (2 * np.pi) * sps)
            # buffer = tail of the previous chunk + this chunk
            if prev_y is not None:
                keep = prev_y[Vu - 4 * blk:Vu]
                buf = np.concatenate([keep, y])
                b0 = prev_g0 + Vu - 4 * blk
            else:
                buf, b0 = y, g0
            last = c == nch - 1
            end_out = min(n * up, g0 + Vu)              # last valid output sample of the record
            t_lim = (end_out - sps - 3) if last else (cents[-1])
            ca, ta = np.asarray(cents), np.asarray(taus)
            # symbols k with k·sps (+tau) up to t_lim
            k_hi = min(int(math.floor((t_lim - ta[-1]) / sps)), kn + nsym - kz)
            if k_hi > kn:
                k = np.arange(kn, k_hi, dtype=np.float64)
                lt = k * sps + np.interp(k * sps, ca, ta) - b0
                ok = (lt >= 1) & (lt + sps / 2 + 3 < len(buf))
                good = np.where(ok)[0]
                if len(good):
                    i0, i1 = good[0], good[-1] + 1
                    if kz > 0 and i0 > 0:               # a gap inside the stream cannot happen; keep contiguous
                        i0 = 0
                        lt = np.clip(lt, 1, len(buf) - sps / 2 - 4)
                    lt = lt[i0:i1]
                    jobs.append(ex.submit(sample, buf, lt, kz))
                    kz += len(lt)
                    kn += i1
                elif kz == 0:
                    kn = k_hi                            # all before the record start
            prev_y, prev_g0 = y, g0
            while len(jobs) > 2 * _workers():
                jobs.pop(0).result()
            if progress:
                progress((c + 1) / nch)
        for j in jobs:
            j.result()
    z = z[:kz]
    # carrier phase per 1024 symbols (power 4 vs the 45° QPSK reference), unwrapped, chunked in place
    pb = 1024
    nzb = max(1, len(z) // pb)
    ph = np.empty(nzb)
    for s in range(0, nzb, 4096):
        e = min(nzb, s + 4096)
        zz = z[s * pb:e * pb].reshape(e - s, pb) if e * pb <= len(z) else z[s * pb:].reshape(1, -1)
        ph[s:e] = np.angle(-np.mean(zz.astype(np.complex128) ** 4, axis=1))
    ph = np.unwrap(ph) / 4
    zc = (np.arange(nzb) + 0.5) * pb
    step = 1 << 22
    for s in range(0, len(z), step):
        e = min(len(z), s + step)
        p = np.interp(np.arange(s, e), zc, ph) if nzb > 1 else np.full(e - s, ph[0])
        z[s:e] *= np.exp(-1j * p).astype(np.complex64)
    chips = z.view(np.float32)                          # I0, Q0, I1, Q1, … (no copy)
    return chips, {"cfo_coarse": f1, "fs": fs, "sps": sps, "upsampled": up}


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
    prs = lfsr(POLY1, INIT1, nbits * sf)
    prs ^= lfsr(POLY2, INIT2, nbits * sf)
    pat = rail_sign[[(start + i) % 2 for i in (0, 1)]] * np.float32(sgn)
    sb = np.empty(nbits, np.float32)
    abs_sum = 0.0
    step = max(1, (1 << 22) // sf)                 # despread in blocks of ~4 M chips
    for b0 in range(0, nbits, step):
        b1 = min(nbits, b0 + step)
        i0, i1 = b0 * sf, b1 * sf
        seg = c[start + i0:start + i1] * (np.float32(1) - np.float32(2) * prs[i0:i1].astype(np.float32))
        seg *= np.tile(pat if i0 % 2 == 0 else pat[::-1], (i1 - i0) // 2 + 1)[:i1 - i0]
        sb[b0:b1] = seg.reshape(b1 - b0, sf).sum(axis=1)
        abs_sum += float(np.abs(seg).sum(dtype=np.float64))
    del prs
    out["cbd_snr_db"] = 10 * math.log10(np.mean(np.abs(sb)) ** 2 / (np.var(np.abs(sb)) + 1e-30))
    out["cbd_eff"] = float(np.mean(np.abs(sb))) / (abs_sum / (nbits * sf) * sf + 1e-30)
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
