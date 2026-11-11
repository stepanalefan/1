# -*- coding: utf-8 -*-
"""
iq_demod.py
===========
Software demodulator for raw I/Q captured with the FSW I/Q Analyzer (no FSW-K70 needed).

    x (complex baseband, fs = sps x symbol rate)
      -> coarse frequency offset   (FFT of x^M, M = 2 BPSK, 4 QPSK/OQPSK/QAM, 8 for 8PSK)
      -> matched filter            (RRC if the transmitter uses RRC)
      -> coarse carrier phase      (angle of mean(y^M))
      -> symbol timing + clock     (Oerder & Meyr square-law line per block, linear fit
                                    = timing offset and symbol rate error)
      -> interpolation at the symbol instants (OQPSK: Q rail half a symbol later)
      -> residual frequency + phase (power-M phase per block, unwrapped, linear fit)
      -> gain / phase refinement on decisions, MER, EVM, I/Q offset

Results use the same keys as the K70 results of fsw_vsa_monitor.py:
    i, q (symbols), mer [dB], evm [%], power [dBm], cfe [Hz], srerr [ppm], iqoff [dB]

Pure NumPy. Not a calibrated measurement: MER/EVM are close to K70 for good signals but are
not traceable. The constellation has the usual 2*pi/M rotation ambiguity (no sync pattern).
"""

from __future__ import annotations

import math

import numpy as np

MODULATIONS = ["BPSK", "QPSK", "OQPSK", "8PSK", "16QAM"]
FILTERS = [("Root raised cosine (RRC)", "RRC"), ("Raised cosine (RC, no RX filter)", "RC"),
           ("None (rectangular)", "NONE")]
POWER_M = {"BPSK": 2, "QPSK": 4, "OQPSK": 4, "8PSK": 8, "16QAM": 4}
R_LOAD = 50.0                      # FSW I/Q data in volts into 50 ohm


def ideal_points(mod: str) -> np.ndarray:
    """Ideal constellation, unit average power."""
    if mod == "BPSK":
        p = np.array([1, -1], dtype=complex)
    elif mod in ("QPSK", "OQPSK"):
        p = np.exp(1j * (np.pi / 4 + np.pi / 2 * np.arange(4)))
    elif mod == "8PSK":
        p = np.exp(1j * 2 * np.pi * np.arange(8) / 8)
    elif mod == "16QAM":
        ax = np.array([-3, -1, 1, 3], float)
        p = (ax[None, :] + 1j * ax[:, None]).ravel()
    else:
        raise ValueError(f"unknown modulation {mod!r}")
    return p / np.sqrt(np.mean(np.abs(p) ** 2))


def rrc_taps(alpha: float, sps: int, span: int = 24) -> np.ndarray:
    """Root-raised-cosine impulse response, unit energy, span symbols."""
    a = max(float(alpha), 1e-3)
    t = np.arange(-span * sps // 2, span * sps // 2 + 1) / sps
    h = np.empty_like(t)
    for k, tt in enumerate(t):
        if abs(tt) < 1e-9:
            h[k] = 1 - a + 4 * a / np.pi
        elif abs(abs(4 * a * tt) - 1) < 1e-9:
            h[k] = a / np.sqrt(2) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * a)) + (1 - 2 / np.pi) * np.cos(np.pi / (4 * a)))
        else:
            h[k] = (np.sin(np.pi * tt * (1 - a)) + 4 * a * tt * np.cos(np.pi * tt * (1 + a))) / \
                   (np.pi * tt * (1 - (4 * a * tt) ** 2))
    return h / np.sqrt(np.sum(h ** 2))


def rc_taps(alpha: float, sps: int, span: int = 24) -> np.ndarray:
    a = max(float(alpha), 1e-3)
    t = np.arange(-span * sps // 2, span * sps // 2 + 1) / sps
    den = 1 - (2 * a * t) ** 2
    h = np.where(np.abs(den) < 1e-9, np.pi / 4 * np.sinc(1 / (2 * a)), np.sinc(t) * np.cos(np.pi * a * t) / np.where(
        np.abs(den) < 1e-9, 1, den))
    return h / np.max(h)


def _filt(x, h):
    return np.convolve(x, h, mode="same")


def _decide(z, pts):
    return pts[np.argmin(np.abs(z[:, None] - pts[None, :]), axis=1)]


def _coarse_cfo(x, fs, m):
    """Frequency offset from the line of x^M; search |f| < fs / (2 M)."""
    n = 1 << int(math.ceil(math.log2(max(len(x), 1024)))) + 2
    xm = x ** m
    spec = np.abs(np.fft.fft(xm * np.hanning(len(xm)), n))
    f = np.fft.fftfreq(n, 1 / fs)
    k = int(np.argmax(spec))
    if 0 < k < n - 1:                  # parabolic refinement
        a, b, c = np.log(spec[k - 1] + 1e-30), np.log(spec[k] + 1e-30), np.log(spec[(k + 1) % n] + 1e-30)
        d = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0.0
    else:
        d = 0.0
    return (f[k] + d * fs / n) / m


def _upsample(x, u):
    """Band-limited interpolation by FFT zero padding."""
    n = len(x)
    X = np.fft.fft(np.asarray(x, np.complex64))             # single precision: ~2× faster, ample for MER
    h = n // 2
    Y = np.zeros(n * u, np.complex64)
    Y[:h] = X[:h]
    Y[-(n - h):] = X[h:]
    return np.fft.ifft(Y) * u


def demodulate(x, fs: float, symbol_rate: float, mod: str = "QPSK", filt: str = "RRC",
               alpha: float = 0.35) -> dict:
    """Demodulate complex baseband samples x (volts) captured at fs. See module docstring."""
    x = np.asarray(x, dtype=complex)
    if mod not in POWER_M:
        raise ValueError(f"modulation must be one of {', '.join(MODULATIONS)}")
    sps = fs / symbol_rate
    if abs(sps - round(sps)) > 1e-6 or round(sps) < 2:
        raise ValueError("sample rate must be an integer multiple (>= 2) of the symbol rate")
    sps = int(round(sps))
    if len(x) < 64 * sps:
        raise ValueError(f"capture too short: {len(x)} samples = {len(x) // sps} symbols (need >= 64)")
    pts = ideal_points(mod)
    m = POWER_M[mod]
    oq = mod == "OQPSK"
    power_w = float(np.mean(np.abs(x) ** 2)) / R_LOAD
    power = 10 * math.log10(power_w * 1e3) if power_w > 0 else float("nan")

    # 1. coarse frequency
    n = np.arange(len(x))
    f1 = _coarse_cfo(x, fs, m)
    y = x * np.exp(-2j * np.pi * f1 / fs * n)
    # 2. matched filter
    if filt == "RRC":
        y = _filt(y, rrc_taps(alpha, sps))
    elif filt == "NONE":
        y = _filt(y, np.ones(sps) / sps)
    rms = math.sqrt(np.mean(np.abs(y) ** 2)) or 1.0
    y = y / rms
    # 3. coarse phase (constellations with a point at 45 deg: QPSK-like)
    ref = np.mean(pts ** m)
    y = y * np.exp(-1j * np.angle(np.mean(y ** m) / ref) / m)
    # 4. timing per block (Oerder & Meyr), linear fit -> offset + clock error
    e = (y.real ** 2 - y.imag ** 2) if oq else np.abs(y) ** 2
    blk = 128 * sps
    nb = max(1, len(e) // blk)
    rot = np.exp(-2j * np.pi * np.arange(blk) / sps)
    taus, cents = [], []
    for b in range(nb):
        seg = e[b * blk:(b + 1) * blk]
        v = np.sum(seg * rot[:len(seg)])
        taus.append(-np.angle(v) / (2 * np.pi) * sps)
        cents.append(b * blk + blk / 2)
    taus = np.unwrap(np.array(taus) * 2 * np.pi / sps) * sps / (2 * np.pi)
    if nb >= 3:
        slope, tau0 = np.polyfit(cents, taus, 1)
    else:
        slope, tau0 = 0.0, float(np.mean(taus))
        cents = [0.0]
    srerr = -slope * 1e6                               # sample drift per sample -> ppm of the symbol rate
    # symbol instants (in samples): k*sps*(1+slope) + tau0 + slope*... (tau measured at block centres)
    edge = 13 * sps                                    # skip filter transients (span / 2)
    k0 = int(math.ceil((edge - tau0) / sps))
    k1 = int((len(y) - edge - tau0 - (sps / 2 if oq else 0)) / sps / (1 + abs(slope))) - 1
    k = np.arange(max(k0, 0), max(k1, k0 + 1))
    t = k * sps + tau0 + slope * (k * sps)             # timing offset grows linearly
    t = t[(t >= 0) & (t < len(y) - sps)]
    u = 4
    yu = _upsample(y, u)
    grid = np.arange(len(yu)) / u

    def at(tt):
        return np.interp(tt, grid, yu.real) + 1j * np.interp(tt, grid, yu.imag)
    if oq:
        z = at(t).real + 1j * at(t + sps / 2).imag
    else:
        z = at(t)
    if len(z) < 32:
        raise ValueError("too few symbols after timing recovery")
    # 5. residual frequency + phase from power-M phase per block
    sb = 64
    nzb = max(1, len(z) // sb)
    ph, idx = [], []
    for b in range(nzb):
        seg = z[b * sb:(b + 1) * sb]
        ph.append(np.angle(np.mean(seg ** m) / ref))
        idx.append(b * sb + len(seg) / 2)
    ph = np.unwrap(np.array(ph)) / m
    if nzb >= 3:
        fslope, p0 = np.polyfit(idx, ph, 1)
    else:
        fslope, p0 = 0.0, float(np.mean(ph))
    z = z * np.exp(-1j * (p0 + fslope * np.arange(len(z))))
    # 5b. decision-directed residual frequency + phase (removes power-M self-noise, e.g. QAM)
    for _ in range(2):
        d = _decide(z / (np.sqrt(np.mean(np.abs(z) ** 2)) or 1.0), pts)
        dph, didx = [], []
        for b in range(nzb):
            sl = slice(b * sb, (b + 1) * sb)
            dph.append(np.angle(np.vdot(d[sl], z[sl])))
            didx.append(b * sb + len(z[sl]) / 2)
        dph = np.unwrap(np.array(dph))
        if nzb >= 3:
            ds, dp = np.polyfit(didx, dph, 1)
        else:
            ds, dp = 0.0, float(np.mean(dph))
        z = z * np.exp(-1j * (dp + ds * np.arange(len(z))))
        fslope += ds
    f2 = fslope * symbol_rate / (2 * np.pi)
    # 6. decision-directed refinement: complex gain (phase + amplitude), twice
    for _ in range(2):
        d = _decide(z, pts)
        g = np.vdot(d, z) / np.vdot(d, d)
        z = z / g
    d = _decide(z, pts)
    err = z - d
    ps, pe = float(np.mean(np.abs(d) ** 2)), float(np.mean(np.abs(err) ** 2))
    mer = 10 * math.log10(ps / pe) if pe > 0 else float("inf")
    evm = 100 * math.sqrt(pe / ps)
    off = abs(np.mean(err))
    iqoff = 20 * math.log10(off) if off > 0 else -120.0
    return {"i": z.real, "q": z.imag, "mer": mer, "evm": evm, "power": power, "cfe": f1 + f2,
            "srerr": srerr, "iqoff": iqoff, "nsym": len(z), "sps": sps}


def spectrum(x, fs: float, nfft: int = 1024):
    """Averaged power spectrum of the capture (dB, relative), for display."""
    x = np.asarray(x, dtype=complex)
    nfft = min(nfft, 1 << int(math.log2(max(len(x), 16))))
    w = np.hanning(nfft)
    nseg = max(1, len(x) // nfft)
    p = np.zeros(nfft)
    for s in range(nseg):
        seg = x[s * nfft:(s + 1) * nfft]
        if len(seg) < nfft:
            break
        p += np.abs(np.fft.fft(seg * w)) ** 2
    p = np.fft.fftshift(p / max(nseg, 1) / np.sum(w ** 2) / R_LOAD)
    f = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / fs))
    return f, 10 * np.log10(p * 1e3 + 1e-30)


def simulate(mod="QPSK", symbol_rate=16e3, sps=8, nsym=2000, snr_db=25.0, cfo=300.0, filt="RRC",
             alpha=0.35, clock_ppm=0.0, delay=0.37, phase=0.6, power_dbm=-30.0, iq_offset=0.0, rng=None,
             symbols=None):
    """Synthetic capture (volts) for tests and demo mode. symbols: given symbol values (e.g. coded data)."""
    rng = rng or np.random.default_rng()
    pts = ideal_points(mod)
    sym = pts[rng.integers(len(pts), size=nsym + 60)]
    if symbols is not None:
        s0 = np.asarray(symbols, complex)[:nsym + 60]
        sym[20:20 + len(s0) - 20] = s0[:len(s0) - 20] if len(s0) > 20 else sym[20:20]
    up = np.zeros((nsym + 60) * sps, complex)
    if mod == "OQPSK":
        up[::sps] = sym.real
        q = np.zeros_like(up)
        q[::sps] = sym.imag
        q = np.roll(q, sps // 2)
        up = up + 1j * q.real
    else:
        up[::sps] = sym
    if filt == "RRC":
        h = rrc_taps(alpha, sps)
    elif filt == "RC":
        h = rc_taps(alpha, sps)
    else:
        h = np.ones(sps)
    s = np.convolve(up, h, mode="same")
    # fractional delay + clock error by resampling
    n_out = nsym * sps
    tt = (np.arange(n_out) * (1 + clock_ppm * 1e-6) + 20 * sps + delay * sps) * 8
    su = _upsample(s, 8)
    s = np.interp(tt, np.arange(len(su)), su.real) + 1j * np.interp(tt, np.arange(len(su)), su.imag)
    s = s / math.sqrt(np.mean(np.abs(s) ** 2))
    nn = np.arange(n_out)
    s = (s + iq_offset) * np.exp(1j * (2 * np.pi * cfo / (symbol_rate * sps) * nn + phase))   # TX carrier leak
    sigma = math.sqrt(10 ** (-snr_db / 10) / 2)
    s = s + sigma * (rng.normal(size=n_out) + 1j * rng.normal(size=n_out))
    v_rms = math.sqrt(10 ** (power_dbm / 10) * 1e-3 * R_LOAD)
    return s * v_rms
