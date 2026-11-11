# -*- coding: utf-8 -*-
"""
bos_analysis.py
===============
Measurement analysis and fault diagnosis for the BOS modulator test station (bos_station.py).
Pure NumPy, no GUI - every function can be tested on simulated captures (see `simulate_bos`).

Signal chain of the modulator (what the diagnosis reasons about)
---------------------------------------------------------------
    REF 10 MHz ─► CLK (LMK / DAC-clock PLL) ─► FPGA MPF300T ══JESD204B══► DAC38J82 (I, Q)
                                                                              │
    LMX2594 (LO) ──────────────────────────────────────────► ADMV1013 (LO ×4, I/Q upconverter)
                                                                              │
                                              ADRF5730 (0…31.5 dB, 0.5 dB) ◄──┘
                                                         │
                                              CHA3397 (PA) ─► output / cable ─► FSW

What is measured from one raw I/Q capture of the FSW (I/Q Analyzer, no K70 needed)
-----------------------------------------------------------------------------------
* modulation quality: MER, EVM, phase error RMS, magnitude error RMS (iq_demod.demodulate)
* frequency: carrier frequency error (Hz and ppm of the carrier), symbol-rate (clock) error (ppm)
* I/Q modulator impairments in the symbol frame: gain imbalance, quadrature error, I/Q offset
  (carrier leakage) - fitted as  [I', Q'] = M · [I, Q] + c  on the decided symbols
* phase noise signature: phase error split into slow (loop bandwidth) and fast components
* PA compression: PAPR of the capture vs. the PAPR of an ideal signal with the same filter
* spectrum: occupied bandwidth (99 %), ACLR, spectral asymmetry, carrier line, discrete spurs
* modulation identification: the capture is demodulated as BPSK, QPSK and OQPSK
* CW (carrier) captures: frequency, power, integrated phase noise, spurs
* ADRF5730 attenuator: least-squares bit weights from a few codes -> stuck / weak bits

Diagnosis
---------
`diagnose(results, telemetry, cfg)` turns measurements + BOS telemetry (PLL locks, JESD, rails,
temperatures, 10 MHz reference) into findings. Each finding names the suspect blocks with weights;
`suspicion()` combines them into a 0…1 score per block for the block diagram on the station tab.
The rules are engineering heuristics, not proof: they rank where to look first and say why.
"""

from __future__ import annotations

import math
import time

import numpy as np

import iq_demod

# --------------------------------------------------------------------------- #
#  Signal chain                                                                #
# --------------------------------------------------------------------------- #
# id, short name, part, role
BLOCKS = [
    ("REF", "Reference", "10 MHz TCXO / ext.", "Frequency reference for all synthesizers"),
    ("CLK", "Clock PLLs", "LMK + DAC clock PLL", "FPGA / DAC sample clocks, SYSREF (telemetry pll_lmk, pll_lmx (DAC))"),
    ("FPGA", "FPGA", "Microsemi MPF300T", "Modem: symbol mapping, BPSK / OQPSK, pulse shaping, JESD204B TX"),
    ("DAC", "DAC", "TI DAC38J82", "Dual 16-bit DAC (I, Q), JESD204B RX, QMC gain / phase / offset"),
    ("LMX", "LO synth", "TI LMX2594", "LO synthesizer for the upconverter (telemetry pll_lmx (mod))"),
    ("MIX", "Upconverter", "ADI ADMV1013", "I/Q upconverter with LO ×4 multiplier"),
    ("ATT", "Attenuator", "ADI ADRF5730", "6-bit digital step attenuator, 0.5 dB LSB, 31.5 dB"),
    ("PA", "Power amp", "UMS CHA3397", "Output power amplifier"),
    ("PWR", "Power rails", "1 V / 1.8 V / 2.5 V", "FPGA rails (telemetry) and the bench supply"),
    ("OUT", "Output / setup", "connectors, cable, FSW", "RF output, cable, fixture and measurement setup"),
]
BLOCK_IDS = [b[0] for b in BLOCKS]
BLOCK_NAME = {b[0]: f"{b[1]} ({b[2]})" for b in BLOCKS}
ATT_BITS = (1.0, 2.0, 4.0, 8.0, 16.0)               # Atten_EIRP bits (1 dB LSB, BOS command), dB
ATT_MAX = 20                                        # Atten_EIRP saturates above 20 dB


def _db(x):
    return 10 * math.log10(x) if x > 0 else -300.0


def _finite(v):
    return v is not None and isinstance(v, (int, float)) and math.isfinite(v)


# --------------------------------------------------------------------------- #
#  Spectrum helpers                                                            #
# --------------------------------------------------------------------------- #
def psd(x, fs, nfft=2048):
    """Welch PSD (Hann, 50 % overlap). Returns f (Hz, centred), p (W/Hz into 50 ohm)."""
    x = np.asarray(x, complex)
    nfft = int(min(nfft, 1 << int(math.log2(max(len(x), 64)))))
    w = np.hanning(nfft)
    step = nfft // 2
    acc = np.zeros(nfft)
    n = 0
    for s in range(0, len(x) - nfft + 1, step):
        acc += np.abs(np.fft.fft(x[s:s + nfft] * w)) ** 2
        n += 1
    p = np.fft.fftshift(acc / max(n, 1) / (fs * np.sum(w ** 2)) / iq_demod.R_LOAD)
    f = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / fs))
    return f, p


def _band(f, p, lo, hi):
    m = (f >= lo) & (f < hi)
    if not np.any(m):
        return 0.0
    return float(np.sum(p[m]) * (f[1] - f[0]))


def spectrum_metrics(x, fs, sr, alpha, cfe=0.0):
    """Occupied bandwidth, ACLR, asymmetry, carrier line and spurs of a modulated capture."""
    f, p = psd(x, fs, 4096 if len(x) >= 32768 else 2048)
    f = f - cfe                                           # centre the signal
    df = f[1] - f[0]
    bw = sr * (1 + alpha)
    total = float(np.sum(p) * df)
    # 99 % occupied bandwidth
    c = np.cumsum(p) * df
    lo = f[np.searchsorted(c, 0.005 * total)]
    hi = f[min(len(f) - 1, np.searchsorted(c, 0.995 * total))]
    obw = float(hi - lo)
    main = _band(f, p, -bw / 2, bw / 2)
    adj_l = _band(f, p, -1.5 * bw, -0.5 * bw)
    adj_u = _band(f, p, 0.5 * bw, 1.5 * bw)
    can_aclr = fs / 2 > 1.5 * bw + abs(cfe)
    aclr_l = _db(main / adj_l) if can_aclr and adj_l > 0 else float("nan")
    aclr_u = _db(main / adj_u) if can_aclr and adj_u > 0 else float("nan")
    up = _band(f, p, 0.05 * sr, 0.5 * sr)
    dn = _band(f, p, -0.5 * sr, -0.05 * sr)
    asym = _db(up / dn) if up > 0 and dn > 0 else float("nan")
    # carrier line: bins at 0 Hz vs. the signal density around it
    k0 = int(np.argmin(np.abs(f)))
    ring = np.r_[p[max(0, k0 - 12):max(0, k0 - 4)], p[k0 + 5:k0 + 13]]
    line = _db(p[max(0, k0 - 1):k0 + 2].max() / (np.median(ring) or 1e-30)) if ring.size else float("nan")
    # discrete spurs outside the occupied band: > 12 dB over the local median
    spurs = []
    pd = 10 * np.log10(p + 1e-30)
    med = np.convolve(pd, np.ones(31) / 31, mode="same")
    out = np.abs(f) > 0.6 * bw
    cand = np.where(out & (pd - med > 12))[0]
    for k in cand:
        if 0 < k < len(pd) - 1 and pd[k] >= pd[k - 1] and pd[k] >= pd[k + 1]:
            sp_pw = float(np.sum(p[k - 1:k + 2]) * df)      # Hann main lobe: 3 bins
            spurs.append((float(f[k]), _db(sp_pw / main) if main > 0 else float("nan")))
    spurs.sort(key=lambda s: -s[1])
    return {"obw": obw, "obw_ratio": obw / bw, "aclr_l": aclr_l, "aclr_u": aclr_u,
            "aclr": float(np.nanmin([aclr_l, aclr_u])) if can_aclr else float("nan"),
            "asym_db": asym, "carrier_line_db": line, "spurs": spurs[:8],
            "spur_max_dbc": spurs[0][1] if spurs else float("nan"), "spec_f": f + cfe, "spec_p": pd + 30}


# --------------------------------------------------------------------------- #
#  Modulated capture                                                           #
# --------------------------------------------------------------------------- #
_PAPR_REF: dict = {}


def papr(x, q=99.99):
    pw = np.abs(x) ** 2
    m = float(np.mean(pw))
    return _db(float(np.percentile(pw, q)) / m) if m > 0 else float("nan")


def papr_reference(mod, sps, alpha, bw_frac=None):
    """PAPR (99.99 %) of an ideal RRC signal with this modulation (cached), optionally limited to
    bw_frac × symbol rate (the FSW I/Q bandwidth) like the measured capture."""
    bl = None if bw_frac is None or bw_frac >= 1 + alpha else round(float(bw_frac), 3)
    key = (mod, int(sps), round(float(alpha), 3), bl)
    if key not in _PAPR_REF:
        x = iq_demod.simulate(mod, 1e6, int(sps), 6000, snr_db=80, cfo=0, alpha=alpha, delay=0.0, phase=0.0,
                              rng=np.random.default_rng(7))
        if bl:
            x = band_limit(x, 1e6 * sps, bl * 1e6)
        _PAPR_REF[key] = papr(x)
    return _PAPR_REF[key]


def band_limit(x, fs, bw):
    """Ideal (brick-wall) low-pass to ±bw/2, like the FSW I/Q bandwidth."""
    X = np.fft.fft(x)
    f = np.fft.fftfreq(len(x), 1 / fs)
    return np.fft.ifft(X * (np.abs(f) <= bw / 2))


def resample_block(fs, fs_new) -> int:
    """resample() works on multiples of this many input samples (e.g. 34375 for 275 -> 524.288 MS/s)."""
    from fractions import Fraction
    return Fraction(fs_new / fs).limit_denominator(1_000_000).denominator


def resample(x, fs, fs_new):
    """Band-limited (FFT) resampling to exactly fs_new: the record is shortened so that
    len × fs_new / fs is an integer. Returns (y, fs_new)."""
    from fractions import Fraction
    q = Fraction(fs_new / fs).limit_denominator(1_000_000)
    n = len(x) // q.denominator * q.denominator
    if n == 0:
        raise ValueError(f"capture too short for resampling {fs / 1e6:g} -> {fs_new / 1e6:g} MS/s: "
                         f"{len(x)} samples, needs at least {q.denominator}")
    m = n * q.numerator // q.denominator
    X = np.fft.fft(np.asarray(x[:n], complex))
    Y = np.zeros(m, complex)
    h = min(n, m) // 2
    Y[:h] = X[:h]
    Y[-h:] = X[-h:]
    return np.fft.ifft(Y) * (m / n), fs * q.numerator / q.denominator


def decimate(x, fs, f0, D, progress=None):
    """Shift f0 to 0 Hz, low-pass and keep every D-th sample, streamed in FFT blocks (overlap-save):
    a multi-GB narrowband record becomes a small one. Passband ±0.4·fs/D (gain 1), cosine roll-off to
    ±0.5·fs/D. Returns (y complex64, fs / D)."""
    n = len(x)
    D = int(D)
    Lo = 1 << 15
    Mo = 1024
    Vo = Lo - 2 * Mo
    L, M = Lo * D, Mo * D
    no = n // D
    y = np.empty(no, np.complex64)
    fo = np.fft.fftfreq(Lo, D / fs)
    af = np.abs(fo) / (fs / D)
    w = np.where(af <= 0.4, 1.0, np.where(af < 0.5, 0.5 * (1 + np.cos(np.pi * (af - 0.4) / 0.1)), 0.0))
    sel = np.r_[0:Lo // 2, L - Lo // 2:L]
    step = f0 / fs
    for c in range(0, -(-no // Vo)):
        a = c * Vo * D - M
        seg = np.zeros(L, np.complex64)
        s0, s1 = max(0, a), min(n, a + L)
        if s1 > s0:
            idx = np.arange(s0, s1, dtype=np.float64)
            seg[s0 - a:s1 - a] = np.asarray(x[s0:s1], np.complex64) * \
                np.exp(-2j * np.pi * ((step * idx) % 1.0)).astype(np.complex64)
        S = np.fft.fft(seg)[sel] * w
        yo = np.fft.ifft(S) * (Lo / L)
        o0 = c * Vo
        m = min(Vo, no - o0)
        y[o0:o0 + m] = yo[Mo:Mo + m]
        if progress:
            progress(min(1.0, (o0 + m) / max(no, 1)))
    return y, fs / D


DEMOD_SPS = 4                     # the software demodulator needs ≥ 4 samples/symbol for OQPSK


def fsw_bandwidth(options: str):
    """Widest I/Q analysis bandwidth (Hz) from the FSW *OPT? answer (B28, B40, B80, B160, B320, B512,
    B1200, B2001, B4001, B8001, also with an R suffix), or None if no bandwidth option is listed."""
    import re
    best = None
    for o in str(options).upper().replace('"', "").split(","):
        m = re.fullmatch(r"B(28|40|80|160|320|500|512|1200|2001|4001|6001|8001)R?", o.strip())
        if m:
            v = int(m.group(1))
            v = v - 1 if v % 1000 == 1 else v
            best = max(best or 0, v * 1e6)
    return best


def track_phase(z, m=2, blk=16):
    """Block-wise carrier phase (power m) tracking. Returns (corrected z, phase wander in deg RMS after
    removing the mean and linear part)."""
    z = np.asarray(z, complex)
    nz = len(z) // blk
    if nz < 4:
        return z, float("nan")
    ph = np.array([np.angle(np.mean(z[i * blk:(i + 1) * blk] ** m)) for i in range(nz)])
    if m == 4:
        ph = ph - np.pi                                   # QPSK points at 45°: z^4 = -1
    ph = np.unwrap(ph) / m
    zc = (np.arange(nz) + 0.5) * blk
    zt = z * np.exp(-1j * np.interp(np.arange(len(z)), zc, ph))
    zt = zt / (np.sqrt(np.mean(np.abs(zt) ** 2)) or 1.0)
    return zt, ph, zc


def freq_wander(ph, zc, sr, win_s=0.01) -> dict:
    """Carrier frequency per window (from the tracked phase): p-p, RMS and the largest drift rate."""
    t = np.asarray(zc) / sr
    m = max(3, int(win_s / (t[1] - t[0]))) if len(t) > 1 else 0
    if not m or len(t) < 3 * m:
        return {}
    tw, fw = [], []
    for i in range(0, len(t) - m, m):
        tw.append(t[i:i + m].mean())
        fw.append(np.polyfit(t[i:i + m], ph[i:i + m], 1)[0] / (2 * np.pi))
    tw, fw = np.array(tw), np.array(fw)
    k = max(2, int(0.1 / win_s))                       # drift rate over 100 ms
    rate = np.abs(np.diff(fw[::k])) / (k * win_s) if len(fw) > k else np.array([0.0])
    return {"t": tw, "f": fw - fw.mean(), "pp_hz": float(np.ptp(fw)), "rms_hz": float(np.std(fw)),
            "max_rate_hz_s": float(rate.max()) if rate.size else 0.0, "span_s": float(tw[-1] - tw[0])}


def iq_impairments(z, d):
    """Fit [zr, zi] = M [dr, di] + c on decided symbols. Returns gain imbalance (dB), quadrature
    error (deg), offset (relative to RMS symbol amplitude, complex) and the image rejection (dB)."""
    A = np.column_stack([d.real, d.imag, np.ones(len(d))])
    if np.linalg.matrix_rank(A[:, :2]) < 2:              # BPSK: Q rail never driven
        A1 = np.column_stack([d.real, np.ones(len(d))])
        cr = np.linalg.lstsq(A1, z.real, rcond=None)[0]
        ci = np.linalg.lstsq(A1, z.imag, rcond=None)[0]
        off = complex(cr[1], ci[1])
        return {"gain_imb": float("nan"), "quad_err": float("nan"), "offset": off,
                "irr": float("nan"), "q_leak_deg": float(np.degrees(math.atan2(ci[0], cr[0])))}
    cr = np.linalg.lstsq(A, z.real, rcond=None)[0]
    ci = np.linalg.lstsq(A, z.imag, rcond=None)[0]
    col_i = np.array([cr[0], ci[0]])
    col_q = np.array([cr[1], ci[1]])
    gi = 20 * math.log10(np.linalg.norm(col_q) / np.linalg.norm(col_i))
    ang = math.degrees(math.atan2(col_i[0] * col_q[1] - col_i[1] * col_q[0], float(np.dot(col_i, col_q))))
    qe = ang - 90.0
    g = 10 ** (gi / 20)
    phi = math.radians(qe)
    irr = _db((1 + g * g + 2 * g * math.cos(phi)) / max(1 + g * g - 2 * g * math.cos(phi), 1e-30))
    return {"gain_imb": gi, "quad_err": qe, "offset": complex(cr[2], ci[2]), "irr": irr}


def analyze_modulated(x, fs, cfg, expected_mod):
    """Full analysis of one modulated capture. cfg: symbol_rate, alpha, carrier, loss_db."""
    sr, alpha = cfg["symbol_rate"], cfg["alpha"]
    filt = cfg.get("filter", "RRC")
    out = {"kind": "mod", "expected": expected_mod, "fs": fs, "nsamp": len(x)}
    pw = float(np.mean(np.abs(x) ** 2)) / iq_demod.R_LOAD
    out["power_raw"] = _db(pw * 1e3)
    out["power"] = out["power_raw"] + cfg.get("loss_db", 0.0)
    # noise floor estimate from the band edges (if the capture is wide enough)
    f, p = psd(x, fs, 1024)
    edge = np.abs(f) > 0.8 * fs / 2
    sig = np.abs(f) < sr * (1 + alpha) / 2
    nf = float(np.median(p[edge])) if np.any(edge) else 0.0
    sd = float(np.mean(p[sig])) if np.any(sig) else 0.0
    out["snr_floor"] = _db(sd / nf) if nf > 0 and sd > 0 else float("nan")
    out["present"] = bool(out["snr_floor"] > 10) if _finite(out["snr_floor"]) else pw > 0
    # the demodulator needs an integer number (≥ 4) of samples per symbol: resample if the FSW
    # captured at another rate (e.g. 200 MS/s for 131.072 Msym/s with a 160 MHz I/Q bandwidth)
    xd, fsd = x, fs
    if abs(fs / sr - round(fs / sr)) > 1e-6 or round(fs / sr) < DEMOD_SPS:
        xd, fsd = resample(x, fs, sr * DEMOD_SPS)
        out["resampled"] = f"{fs / 1e6:g} MS/s -> {fsd / 1e6:g} MS/s ({DEMOD_SPS} samples/symbol)"
    sps = int(round(fsd / sr))
    bw = cfg.get("fsw_bw")
    # compression products (spectral regrowth) reach ~20 % beyond the channel: a narrower FSW cuts them
    out["band_limited"] = bool(bw and bw < sr * (1 + alpha) * 1.2)
    # identification: try the candidate modulations
    tries = {}
    for m in cfg.get("try_mods", ("BPSK", "QPSK", "OQPSK")):
        try:
            tries[m] = iq_demod.demodulate(xd, fsd, sr, m, filt, alpha)
        except Exception as e:
            tries[m] = {"error": str(e), "mer": float("nan")}
    out["mer_by_mod"] = {m: r.get("mer", float("nan")) for m, r in tries.items()}
    best = max(tries, key=lambda m: tries[m].get("mer", -1e9) if _finite(tries[m].get("mer")) else -1e9)
    # BPSK fits a QPSK capture badly, but QPSK fits a BPSK capture almost as well -> prefer the
    # lower order when it is within 1 dB, and OQPSK over QPSK only when clearly better
    mb = out["mer_by_mod"]
    if _finite(mb.get("BPSK")) and mb["BPSK"] >= max(v for v in mb.values() if _finite(v)) - 1.0:
        best = "BPSK"
    elif best == "QPSK" and _finite(mb.get("OQPSK")) and mb["OQPSK"] > mb["QPSK"] - 0.5:
        best = "OQPSK"
    out["detected"] = best
    r = tries.get(expected_mod) or {}
    if "error" in r or not r:
        out["error"] = r.get("error", "demodulation failed")
        out["spectrum"] = spectrum_metrics(x, fs, sr, alpha)
        return out
    z = r["i"] + 1j * r["q"]
    pts = iq_demod.ideal_points(expected_mod)
    if expected_mod == "BPSK" and len(z) > 2000:
        # the demodulator removes one frequency for the whole capture; a real carrier wanders (close-in
        # phase noise, FSW and BOS on different references): track the phase every 16 symbols
        z, ph_t, zc_t = track_phase(z, 2, 16)
        fwd = freq_wander(ph_t, zc_t, sr)
        if fwd:
            out["freq_wander"] = fwd
        dd = pts[np.argmin(np.abs(z[:, None] - pts[None, :]), axis=1)]
        e = z - dd
        r = dict(r, mer=_db(float(np.mean(np.abs(dd) ** 2)) / max(float(np.mean(np.abs(e) ** 2)), 1e-30)),
                 evm=100 * math.sqrt(float(np.mean(np.abs(e) ** 2)) / float(np.mean(np.abs(dd) ** 2))),
                 i=z.real, q=z.imag)
    if fs / sr < 4 and expected_mod == "BPSK":
        out["low_sps"] = fs / sr
    d = pts[np.argmin(np.abs(z[:, None] - pts[None, :]), axis=1)]
    ph = np.angle(z / d)
    out.update(mer=r["mer"], evm=r["evm"], cfe=r["cfe"], srerr=r["srerr"], iqoff=r["iqoff"],
               nsym=r["nsym"], i=r["i"], q=r["q"])
    out["cfe_ppm"] = r["cfe"] / cfg["carrier"] * 1e6 if cfg.get("carrier") else float("nan")
    out["phase_err_deg"] = float(np.degrees(np.sqrt(np.mean(ph ** 2))))
    out["mag_err_pct"] = float(100 * np.sqrt(np.mean((np.abs(z) / np.abs(d) - 1) ** 2)))
    out["phase_err_series"] = np.degrees(ph)
    imp = iq_impairments(z, d)
    out["gain_imb"], out["quad_err"], out["irr"] = imp["gain_imb"], imp["quad_err"], imp["irr"]
    # residual after removing the fitted I/Q impairments: what is left is noise / phase noise /
    # distortion, and the MER the unit would reach after a QMC correction
    zc = z - imp["offset"]
    if _finite(imp["gain_imb"]):
        A = np.column_stack([d.real, d.imag])
        M = np.linalg.lstsq(A, np.column_stack([zc.real, zc.imag]), rcond=None)[0].T
        v = np.linalg.solve(M, np.vstack([zc.real, zc.imag]))
        zc = v[0] + 1j * v[1]
    e = zc - d
    out["mer_corr"] = _db(float(np.mean(np.abs(d) ** 2)) / max(float(np.mean(np.abs(e) ** 2)), 1e-30))
    phr = np.angle(zc / d)
    out["phase_err_res_deg"] = float(np.degrees(np.sqrt(np.mean(phr ** 2))))
    k = max(4, min(64, len(phr) // 50))
    slow = np.convolve(phr, np.ones(k) / k, mode="same")
    out["phase_slow_deg"] = float(np.degrees(np.sqrt(np.mean(slow[k:-k] ** 2)))) if len(slow) > 2 * k else float("nan")
    out["offset_i"], out["offset_q"] = imp["offset"].real, imp["offset"].imag
    out["offset_dbc"] = _db(abs(imp["offset"]) ** 2) if abs(imp["offset"]) > 0 else -120.0
    pr = papr(xd)
    ref = papr_reference(expected_mod, sps, alpha, (min(bw, fs) / sr) if bw else fs / sr)
    out["papr"], out["papr_ref"], out["compression_db"] = pr, ref, ref - pr
    if filt == "NONE":
        out["compression_db"] = float("nan")
        out["compression_note"] = "n/a: constant envelope (gain compression: PA-linearity test)"
    elif out["band_limited"]:
        # cutting the band regrows the peaks the PA clipped: PAPR no longer shows compression
        out["compression_db"] = float("nan")
        out["compression_note"] = "n/a: FSW bandwidth cuts the spectral regrowth (see PA-linearity gain compression)"
    out["spectrum"] = spectrum_metrics(x, fs, sr, alpha, r["cfe"])
    if expected_mod == "BPSK" and cfg.get("decode_ur", True) and "i" in out:
        try:
            import bos_decode
            out["decode"] = bos_decode.decode_ur(out["i"], cfg.get("scid"))
        except Exception as e:
            out["decode"] = {"error": f"{type(e).__name__}: {e}"}
    dec = out.get("decode") or {}
    if expected_mod == "BPSK" and any(f.get("rs_ok") for f in dec.get("frames", [])):
        out["detected"] = "BPSK"                         # decoded УР frames prove the format
    if filt == "NONE":                               # rectangular pulses: no channel bandwidth
        out["spectrum"]["aclr"] = out["spectrum"]["obw_ratio"] = float("nan")
    if bw and bw < sr * (1 + alpha):                 # OBW can be at most the FSW I/Q bandwidth
        out["spectrum"]["obw_ratio"] = out["spectrum"]["obw"] / bw
    return out


# --------------------------------------------------------------------------- #
#  CW (carrier) capture                                                        #
# --------------------------------------------------------------------------- #
def analyze_cw(x, fs, cfg, pn_band=(1e3, 100e3)):
    """Frequency, power, integrated phase noise (deg RMS in pn_band) and spurs of a carrier."""
    x = np.asarray(x, complex)
    n = len(x)
    out = {"kind": "cw", "fs": fs, "nsamp": n}
    pw = float(np.mean(np.abs(x) ** 2)) / iq_demod.R_LOAD
    out["power_raw"] = _db(pw * 1e3)
    out["power"] = out["power_raw"] + cfg.get("loss_db", 0.0)
    nfft = 1 << int(math.ceil(math.log2(n))) + 1
    spec = np.abs(np.fft.fft(x * np.hanning(n), nfft))
    fr = np.fft.fftfreq(nfft, 1 / fs)
    k = int(np.argmax(spec))
    a, b, c = (np.log(spec[(k + d) % nfft] + 1e-30) for d in (-1, 0, 1))
    den = a - 2 * b + c
    d = 0.5 * (a - c) / den if den else 0.0
    f0 = float(fr[k] + d * fs / nfft)
    out["cfe"] = f0
    out["cfe_ppm"] = f0 / cfg["carrier"] * 1e6 if cfg.get("carrier") else float("nan")
    # phase noise: PSD of the carrier-normalised signal, single sideband, integrated in the band
    y = x * np.exp(-2j * np.pi * f0 / fs * np.arange(n))
    f, p = psd(y, fs, 4096 if n >= 16384 else 1024)
    p = p / (np.sum(p) * (f[1] - f[0]))                  # dBc/Hz scale
    lo, hi = pn_band
    m = (np.abs(f) >= lo) & (np.abs(f) <= min(hi, fs / 2 * 0.9))
    integ = float(np.sum(p[m]) * (f[1] - f[0]))          # both sidebands ~ phase variance (rad^2)
    out["pn_deg"] = float(np.degrees(math.sqrt(integ))) if integ > 0 else float("nan")
    out["pn_band"] = (lo, min(hi, fs / 2 * 0.9))
    pd = 10 * np.log10(p + 1e-30)
    out["spec_f"], out["spec_p"] = f + f0, pd
    med = np.convolve(pd, np.ones(41) / 41, mode="same")
    spurs = []
    for kk in np.where((np.abs(f) > 5 * (f[1] - f[0]) + lo) & (pd - med > 15))[0]:
        if 0 < kk < len(pd) - 1 and pd[kk] >= pd[kk - 1] and pd[kk] >= pd[kk + 1]:
            spurs.append((float(f[kk]), float(pd[kk] - pd[len(pd) // 2])))
    spurs.sort(key=lambda s: -s[1])
    out["spurs"] = spurs[:8]
    out["spur_max_dbc"] = spurs[0][1] if spurs else float("nan")
    nf = float(np.median(p[np.abs(f) > 0.5 * fs / 2])) if np.any(np.abs(f) > 0.5 * fs / 2) else 0.0
    out["present"] = bool(nf > 0 and _db(float(np.max(p)) / nf) > 20)
    return out


def tone_power(x, fs, loss_db=0.0):
    """Strongest tone in a capture: (offset Hz, power dBm incl. loss)."""
    x = np.asarray(x, complex)
    n = len(x)
    w = np.hanning(n)
    s = np.abs(np.fft.fft(x * w)) ** 2 / (n * np.sum(w ** 2)) / iq_demod.R_LOAD   # energy-correct: no scalloping
    k = int(np.argmax(s))
    idx = np.arange(k - 6, k + 7) % n                    # tone + phase-noise skirt, ±6 bins
    pk = float(s[idx].sum())
    return float(np.fft.fftfreq(n, 1 / fs)[k]), _db(pk * 1e3) + loss_db


# --------------------------------------------------------------------------- #
#  ADRF5730 attenuator                                                         #
# --------------------------------------------------------------------------- #
ATT_CODES_DEFAULT = [0, 1, 2, 4, 8, 16, 3, 12, 20, 5, 0]


def fit_attenuator(codes, powers):
    """Least-squares bit weights from measured output power per Atten_EIRP code (dB, 1 dB LSB;
    the BOS drives the ADRF5730 with it). Returns per-bit weight, error vs nominal, per-code residuals."""
    codes = np.asarray(codes, int)
    pw = np.asarray(powers, float)
    ok = np.isfinite(pw)
    codes, pw = codes[ok], pw[ok]
    nb = len(ATT_BITS)
    bits = ((codes[:, None] >> np.arange(nb)[None, :]) & 1).astype(float)
    A = np.column_stack([np.ones(len(codes)), -bits])
    if len(codes) < nb + 1 or np.linalg.matrix_rank(A) < nb + 1:
        return {"error": f"need at least the {nb} single-bit codes and code 0"}
    sol, *_ = np.linalg.lstsq(A, pw, rcond=None)
    p0, w = float(sol[0]), sol[1:]
    model = A @ sol
    res = []
    for b, (wb, nom) in enumerate(zip(w, ATT_BITS)):
        err = float(wb - nom)
        if abs(wb) < 0.3 * nom:
            state = "no effect (stuck / not driven)"
        elif abs(err) > max(0.25, 0.1 * nom):
            state = "weight error"
        else:
            state = "ok"
        res.append({"bit": b, "nominal": nom, "measured": float(wb), "error": err, "state": state})
    nominal = p0 - bits @ np.array(ATT_BITS)
    return {"p0": p0, "bits": res, "codes": codes.tolist(), "powers": pw.tolist(),
            "residual_rms": float(np.sqrt(np.mean((pw - model) ** 2))),
            "max_err": float(np.max(np.abs(pw - nominal))), "nominal": nominal.tolist()}


# --------------------------------------------------------------------------- #
#  Corrections (DAC38J82 QMC, frequency, power)                                #
# --------------------------------------------------------------------------- #
def corrections(res: dict, cfg: dict) -> list[tuple[str, str, str]]:
    """(what, value, how) - suggested adjustments from the last measurements."""
    out = []
    m = res.get("mod_OQPSK") or res.get("mod_BPSK")
    if m and _finite(m.get("gain_imb")) and abs(m["gain_imb"]) >= 0.05:
        g = m["gain_imb"]
        code = round(-g / 20 * math.log(10) * cfg.get("qmc_gain_lsb_per_unit", 1024))
        out.append(("I/Q gain balance (DAC38J82 QMC gain A/B)", f"{-g:+.3f} dB on Q relative to I",
                    f"Q path is {g:+.3f} dB vs I. Change qmc_gainb by ≈ {code:+d} LSB (scale "
                    f"{cfg.get('qmc_gain_lsb_per_unit', 1024)} LSB = ×1.0, check the datasheet), re-measure."))
    if m and _finite(m.get("quad_err")) and abs(m["quad_err"]) >= 0.3:
        q = m["quad_err"]
        code = round(-math.radians(q) * cfg.get("qmc_phase_lsb_per_rad", 4096))
        out.append(("Quadrature (DAC38J82 QMC phase)", f"{-q:+.2f}°",
                    f"Quadrature error {q:+.2f}°. Change qmc_phase by ≈ {code:+d} LSB "
                    f"({cfg.get('qmc_phase_lsb_per_rad', 4096)} LSB/rad assumed), re-measure."))
    if m and _finite(m.get("offset_dbc")) and m["offset_dbc"] > -60:
        fs_codes = cfg.get("dac_offset_lsb_per_rms", 8192)
        oi, oq = -m["offset_i"] * fs_codes, -m["offset_q"] * fs_codes
        out.append(("LO leakage nulling (DAC38J82 QMC offset A/B)", f"I {oi:+.0f} LSB, Q {oq:+.0f} LSB",
                    f"Carrier leakage {m['offset_dbc']:.1f} dBc. The demodulator's 90° rotation ambiguity means the "
                    f"sign / rail may be swapped: apply half, re-measure, keep the direction that lowers it."))
    for key, label in (("cw", "Carrier"), ("mod_OQPSK", "OQPSK"), ("mod_BPSK", "BPSK")):
        r = res.get(key)
        if r and _finite(r.get("cfe")) and cfg.get("carrier"):
            ppm = r["cfe"] / cfg["carrier"] * 1e6
            lo_in = cfg["carrier"] / cfg.get("lo_mult", 4)
            out.append(("Carrier frequency", f"{-r['cfe']:+.1f} Hz ({-ppm:+.3f} ppm)",
                        f"From the {label} capture. If the symbol rate error has the same ppm, trim the 10 MHz "
                        f"reference; otherwise correct the LMX2594 output by {-r['cfe'] / cfg.get('lo_mult', 4):+.1f} Hz "
                        f"(LO {lo_in / 1e9:.6f} GHz × {cfg.get('lo_mult', 4)})."))
            break
    for key in ("mod_OQPSK", "mod_BPSK", "cw"):
        r = res.get(key)
        if r and _finite(r.get("power")) and _finite(cfg.get("power_nom")):
            dp = r["power"] - cfg["power_nom"]
            steps = round(dp)
            out.append(("Output power", f"{dp:+.2f} dB vs nominal",
                        f"Change Atten_EIRP by {steps:+d} dB to reach {cfg['power_nom']:.1f} dBm (now "
                        f"{cfg.get('att_code', 0)} dB → {max(0, min(ATT_MAX, cfg.get('att_code', 0) + steps))} dB)."))
            break
    a = res.get("att")
    if a and "bits" in a:
        bad = [b for b in a["bits"] if b["state"] != "ok"]
        if bad:
            out.append(("Attenuator calibration table", ", ".join(f"{b['nominal']:g} dB step = {b['measured']:.2f} dB" for b in bad),
                        "Use the measured bit weights for a code→dB table, or replace the ADRF5730 if a bit has no effect."))
    return out


# --------------------------------------------------------------------------- #
#  Diagnosis                                                                   #
# --------------------------------------------------------------------------- #
class Finding:
    def __init__(self, sev, title, detail, suspects=None, actions=(), source=""):
        self.sev, self.title, self.detail = sev, title, detail          # sev: fail / warn / info / ok
        self.suspects = dict(suspects or {})
        self.actions = list(actions)
        self.source = source

    def as_dict(self):
        return {"sev": self.sev, "title": self.title, "detail": self.detail, "suspects": self.suspects,
                "actions": self.actions, "source": self.source}


SEV_WEIGHT = {"fail": 1.0, "warn": 0.5, "info": 0.15, "ok": 0.0}


def suspicion(findings) -> dict:
    """0…1 per block: 1 - prod(1 - weight * severity)."""
    s = {b: 1.0 for b in BLOCK_IDS}
    for f in findings:
        for b, w in f.suspects.items():
            if b in s:
                s[b] *= 1 - min(1.0, w * SEV_WEIGHT.get(f.sev, 0))
    return {b: 1 - v for b, v in s.items()}


def check_telemetry(tlm: dict, cfg: dict, lim: dict) -> list[Finding]:
    out = []
    if not tlm or tlm.get("age_dop") is None:
        if tlm and tlm.get("age_osn") is not None:
            out.append(Finding("warn", "No IOK DOP telemetry (PLL / JESD / rails)",
                               "IOK OSN arrives but IOK DOP does not - its output period may be 0 (Period_FB).",
                               {"FPGA": 0.2}, ["Set the IOK DOP period (telemetry period command) and re-run."],
                               "telemetry"))
        else:
            out.append(Finding("warn", "No BOS telemetry", "No BOS arrays (ID 0x1A46xxxx) were received - "
                               "PLL / JESD / rail checks are skipped and the diagnosis is less precise.",
                               {"OUT": 0.05}, ["Connect the BOS CAN line on the CAN tab", "Check CAN wiring / termination",
                                               "Check that the BOS is powered"], "telemetry"))
        return out
    stale = lim.get("tlm_stale_s", 10)
    if tlm["age_dop"] > stale:
        out.append(Finding("warn", "BOS telemetry is stale", f"Last IOK DOP {tlm['age_dop']:.0f} s ago.",
                           {"FPGA": 0.1}, ["Check the CAN line / BOS power"], "telemetry"))
    plls = [("pll_lmk", "Clock synthesizer (LMK) PLL unlocked", {"CLK": 0.7, "REF": 0.3},
             "All clocks derive from it: expect no / wrong symbol rate and JESD errors.",
             ["Check the 10 MHz reference at the LMK input", "Check LMK loop filter / supply", "Re-power and re-check"]),
            ("pll_lmx (DAC)", "DAC clock PLL unlocked", {"CLK": 0.75, "DAC": 0.15, "REF": 0.1},
             "The DAC sample clock is not locked: output spectrum is wrong or absent.",
             ["Check the DAC clock synthesizer programming and its reference"]),
            ("pll_lmx (mod)", "LO synthesizer LMX2594 unlocked", {"LMX": 0.8, "REF": 0.15, "PWR": 0.05},
             "The upconverter LO is not locked: carrier drifts or is missing, very high phase error.",
             ["Check LMX2594 VCO calibration / register load (SPI from the FPGA)",
              "Check its reference input and loop filter", "Check the 3.3 V supply of the LMX2594"]),
            ("pll_jesd", "FPGA JESD PLL unlocked", {"FPGA": 0.6, "CLK": 0.4},
             "The JESD204B transmitter clock in the FPGA is not locked.",
             ["Check the device clock from the clock tree to the FPGA transceiver reference"]),
            ("pll_all", "FPGA PLLs unlocked", {"FPGA": 0.6, "CLK": 0.3, "PWR": 0.1},
             "FPGA fabric PLLs (except JESD) are not locked.", ["Check the FPGA reference clock and the 1 V core rail"])]
    for key, title, sus, detail, acts in plls:
        v = tlm.get(key)
        if v == 0:
            out.append(Finding("fail", title, detail, sus, acts, "telemetry"))
    if tlm.get("jesd_err") == 0:
        out.append(Finding("fail", "JESD204B link error FPGA → DAC38J82",
                           "The DAC reports a JESD interface error: samples reaching the DAC are invalid or missing.",
                           {"DAC": 0.45, "FPGA": 0.35, "CLK": 0.2},
                           ["Check SYSREF / device clock alignment", "Check lane mapping and polarity (FPGA vs DAC38J82)",
                            "Check SYNC~ signal", "Inspect DAC38J82 BGA soldering / high-speed lanes (X-ray)"],
                           "telemetry"))
    for key, nom in (("vdd_1v", 1.0), ("vdd_1_8v", 1.8), ("vdd_2_5v", 2.5)):
        v = tlm.get(key)
        if _finite(v):
            dev = (v - nom) / nom * 100
            tol = lim.get("rail_tol_pct", 3.0)
            if abs(dev) > tol:
                out.append(Finding("fail" if abs(dev) > 2 * tol else "warn",
                                   f"FPGA rail {nom:g} V out of tolerance", f"{v:.3f} V ({dev:+.1f} %, limit ±{tol:g} %).",
                                   {"PWR": 0.8, "FPGA": 0.2},
                                   [f"Measure the {nom:g} V regulator output and its load", "Check the bench supply voltage / current limit"],
                                   "telemetry"))
    temps = [tlm.get(k) for k in ("Temp_DT1", "Temp_DT2", "Temp_DT3")]
    tmax = max((t for t in temps if _finite(t)), default=None)
    if tmax is not None and tmax > lim.get("temp_max", 70):
        out.append(Finding("warn" if tmax < lim.get("temp_max", 70) + 10 else "fail", "BOS temperature high",
                           f"Max sensor temperature {tmax:.0f} °C (limit {lim.get('temp_max', 70):g} °C).",
                           {"PA": 0.5, "PWR": 0.2, "OUT": 0.1},
                           ["Check heat sinking / thermal pad of the CHA3397", "Check the PA drain current"], "telemetry"))
    if tlm.get("Stat_10MHz") == 0 and cfg.get("ext_ref", False):
        out.append(Finding("fail", "External 10 MHz reference missing",
                           "Stat_10MHz = no signal (count outside 10 000 000 ± 1000 per second); the BOS runs on its "
                           "internal oscillator.",
                           {"REF": 0.8, "OUT": 0.2}, ["Check the TFG6806: output on, 10 MHz, level, cable to the BOS",
                                                      "Check the BOS 10 MHz input"], "telemetry"))
    elif tlm.get("Sour_10MHz") == 0 and cfg.get("ext_ref", False):
        out.append(Finding("warn", "BOS uses its internal 10 MHz oscillator",
                           "Sour_10MHz = internal although the external 10 MHz (TFG6806) is expected and valid: the "
                           "source is set to internal (КОНФИГ / SET_Sour_10MHz).", {"REF": 0.3},
                           ["Send SET_Sour_10MHz = external (0x02, in standby) and save the КОНФИГ if needed"],
                           "telemetry"))
    elif tlm.get("Sour_10MHz") == 1 and not cfg.get("ext_ref", False):
        out.append(Finding("info", "BOS is locked to the external 10 MHz",
                           "The test expects the internal oscillator ('External 10 MHz expected' is off).", {}, [],
                           "telemetry"))
    p = tlm.get("PulsNumb_10MHz")
    tol = lim.get("pulsnumb_tol", 5.0)
    if _finite(p) and cfg.get("ext_ref", False) and tlm.get("Stat_10MHz") != 0 and abs(p - 10_000_000) > tol:
        out.append(Finding("fail", "10 MHz reference frequency off",
                           f"PulsNumb_10MHz = {p:,.0f} pulses per second (required 10 000 000 ± {tol:g}, i.e. "
                           f"±{tol / 10:g} ppm): the 10 MHz given to the BOS is {(p - 1e7) / 10:+.1f} ppm off. Mode "
                           f"commands are not sent until it is inside.",
                           {"REF": 0.9, "OUT": 0.1},
                           ["Check the TFG6806: CH output frequency exactly 10 MHz, its own reference (Ext Ref On in "
                            "the status bar)", "Check the 10 MHz cable / level at the BOS"], "telemetry"))
    e = tlm.get("Err_Ref_LO")
    if _finite(e) and abs(e) > lim.get("err_ref_lo", 3):
        out.append(Finding("warn", "Reference tuning code out of range",
                           f"Err_Ref_LO = {e:.0f} (required |Err_Ref_LO| ≤ {lim.get('err_ref_lo', 3):g} before a "
                           f"mode command): the BOS internal oscillator has not settled on the 10 MHz reference.",
                           {"REF": 0.7, "CLK": 0.3}, ["Wait longer after power-up (Reference wait, Unit control page)",
                                                      "Check the reference oscillator frequency / tuning"],
                           "telemetry"))
    return out


def check_config(c: dict | None, cfg: dict, lim: dict) -> list[Finding]:
    """Stored BOS configuration (ИОК_КОНФИГ, bos_protocol.decode_config) vs. what the test expects."""
    out = []
    if not c:
        return out
    if not c.get("crc_ok"):
        out.append(Finding("warn", "ИОК_КОНФИГ checksum does not match",
                           f"Stored 0x{c['Checksum']:04X}, calculated 0x{c['crc_calc']:04X}: the configuration in "
                           f"flash may be corrupt - the BOS would then start with its default settings.",
                           {"FPGA": 0.3}, ["Re-send МД_КОНФИГ and save it (SAVE_CONFIG_NEW)"], "config"))
    if bool(c["Sour_10MHz"]) != bool(cfg.get("ext_ref", False)):
        out.append(Finding("warn", f"10 MHz source in КОНФИГ is {'external' if c['Sour_10MHz'] else 'internal'}",
                           f"The test expects the {'external' if cfg.get('ext_ref') else 'internal'} reference "
                           f"('External 10 MHz expected' setting). A wrong stored setting is not a hardware fault.",
                           {"REF": 0.2}, ["Set the source with SET_Sour_10MHz (0x02) and save the КОНФИГ"], "config"))
    if c["Ttlm_dop_FB"] == 0:
        out.append(Finding("info", "ИОК_ДОП is disabled in the stored КОНФИГ",
                           "Ttlm_dop_FB = 0 (factory default): PLL / JESD / rail telemetry only arrives after "
                           "SET_Period_TM. The station enables it for the test.", {}, [], "config"))
    if c["Time_SNR"] and c["Time_SNR"] < 60:
        out.append(Finding("info", f"Session limit Time_SNR = {c['Time_SNR']} s",
                           "The BOS ends every session after this time; the station re-starts the session before "
                           "each capture.", {}, [], "config"))
    for k in ("Temp_Max_DT1", "Temp_Max_DT2", "Temp_Max_DT3"):
        if c[k] > lim.get("temp_max", 70) + 10:
            out.append(Finding("info", f"{k} = {c[k]} °C", "Over-temperature shutdown threshold is higher than the "
                               "test limit.", {}, [], "config"))
    return out


def diagnose(res: dict, tlm: dict, cfg: dict, lim: dict) -> list[Finding]:
    """res: {"cw": .., "mod_BPSK": .., "mod_OQPSK": .., "att": .., "harm": .., "supply": .., "drift": ..}."""
    F = list(check_telemetry(tlm, cfg, lim))
    F += check_config(res.get("config"), cfg, lim)
    tlm_ok = bool(tlm) and tlm.get("age_dop") is not None and \
        all(tlm.get(k) != 0 for k in ("pll_lmk", "pll_lmx (DAC)", "pll_lmx (mod)", "pll_jesd", "pll_all", "jesd_err"))
    lo_unlocked = bool(tlm) and tlm.get("pll_lmx (mod)") == 0
    caps = [(k, r) for k, r in res.items() if isinstance(r, dict) and r.get("kind") in ("mod", "cw")]

    # ---- no output at all
    absent = [k for k, r in caps if not r.get("present", True)]
    if caps and len(absent) == len(caps):
        sus = {"PA": 0.35, "MIX": 0.2, "OUT": 0.25, "ATT": 0.1, "FPGA": 0.1}
        if not tlm_ok and tlm:
            sus = {"CLK": 0.3, "LMX": 0.3, "FPGA": 0.2, "DAC": 0.2}
        F.append(Finding("fail", "No RF output detected",
                         "None of the captures shows a signal above the FSW noise floor"
                         + (" although all PLLs are locked and JESD is OK." if tlm_ok else "."),
                         sus, ["Check Mode / Mode_SNR telemetry: is the BOS really transmitting?",
                               "Protocol control: the session ends without a valid target designation (3 s) "
                               "or after Time_SNR - check the keep-alive option and the acknowledgements in the log",
                               "Check the RF cable and the FSW centre frequency",
                               "Check the PA supply current (drain current rises with RF on)",
                               "Check ADMV1013 enable and LO drive", "Check that the attenuator is not at 31.5 dB"],
                         "signal"))
        return F

    # ---- frequency / clock
    mods = {k: r for k, r in caps if r.get("kind") == "mod" and "mer" in r}
    good = [r for r in mods.values() if _finite(r.get("mer")) and r["mer"] >= 12]   # estimates are reliable
    cw = res.get("cw") if isinstance(res.get("cw"), dict) and res["cw"].get("present", True) else None
    if cw and _finite(cw.get("pn_deg")) and cw["pn_deg"] > 20:
        cw = None                                         # carrier not stable enough to measure
    cfe_r = cw or (good[0] if good else None)
    sr_r = good[0] if good else None
    if cfe_r and _finite(cfe_r.get("cfe")) and cfg.get("carrier"):
        ppm = cfe_r["cfe"] / cfg["carrier"] * 1e6
        sr_ppm = sr_r.get("srerr") if sr_r else None
        lim_hz = lim.get("cfe_max_hz", 75.5e3)
        if abs(cfe_r["cfe"]) > lim_hz:
            if _finite(sr_ppm) and abs(ppm) > 1 and abs(sr_ppm - ppm) < max(0.7, 0.3 * abs(ppm)):
                F.append(Finding("fail", "Reference frequency offset",
                                 f"Carrier error {cfe_r['cfe']:+.0f} Hz = {ppm:+.2f} ppm and symbol-rate error "
                                 f"{sr_ppm:+.2f} ppm agree: RF and symbol clock are off by the same ratio → the BOS's "
                                 f"internal 10 MHz oscillator is off-frequency (TZ: ≤ 2·10⁻⁶ without external reference).",
                                 {"REF": 0.85, "CLK": 0.1}, ["Measure the internal oscillator (Err_Ref_LO, counter)",
                                                             "Trim / replace the oscillator, or run on the external 10 MHz"],
                                 "frequency"))
            elif tlm and tlm.get("Sour_10MHz") == 1 and tlm.get("Stat_10MHz") != 0:
                F.append(Finding("fail", "Carrier frequency error (external reference or LO)",
                                 f"Carrier error {cfe_r['cfe']:+.0f} Hz ({ppm:+.3f} ppm). The RF is locked to the external "
                                 f"10 MHz (TFG6806) while the digital part (symbol clock) runs on the BOS's own "
                                 f"oscillator, so an external reference offset moves only the carrier.",
                                 {"REF": 0.5, "LMX": 0.45, "FPGA": 0.05},
                                 ["Check the TFG6806 frequency (10 MHz exactly; its own reference / warm-up)",
                                  "Repeat with SET_Sour_10MHz = internal: if the error disappears, it is the external "
                                  "reference; if it stays, it is the LMX2594 frequency plan"], "frequency"))
            else:
                F.append(Finding("fail", "Carrier frequency error (LO)",
                                 f"Carrier error {cfe_r['cfe']:+.0f} Hz ({ppm:+.3f} ppm) while the symbol clock is "
                                 f"{'correct' if _finite(sr_ppm) and abs(sr_ppm) < 2 else 'not conclusive'} → the LO "
                                 f"path is off: LMX2594 frequency word / fractional-N setup or LO multiplier.",
                                 {"LMX": 0.7 if not lo_unlocked else 0.9, "FPGA": 0.15, "DAC": 0.1, "REF": 0.05},
                                 [f"Check LMX2594 programming for {cfg['carrier'] / cfg.get('lo_mult', 4) / 1e9:.6f} GHz "
                                  f"(×{cfg.get('lo_mult', 4)} = {cfg['carrier'] / 1e9:.6f} GHz)",
                                  "Check the DAC NCO (if used) and the FPGA frequency settings",
                                  "Check the LMX2594 reference divider / PFD"], "frequency"))
        elif abs(cfe_r["cfe"]) > lim_hz / 2:
            F.append(Finding("warn", "Carrier frequency error near the limit", f"{cfe_r['cfe']:+.0f} Hz ({ppm:+.3f} ppm).",
                             {"REF": 0.4, "LMX": 0.4}, ["Check the reference oscillator accuracy"], "frequency"))
        if _finite(sr_ppm) and abs(sr_ppm) > lim.get("srerr_max_ppm", 5) and abs(cfe_r["cfe"]) <= lim_hz:
            F.append(Finding("fail", "Symbol clock error",
                             f"Symbol rate error {sr_ppm:+.2f} ppm while the carrier is on frequency → DAC / FPGA "
                             f"sample clock is wrong (not the common reference).",
                             {"CLK": 0.6, "FPGA": 0.3, "DAC": 0.1},
                             ["Check the DAC clock PLL (pll_lmx (DAC)) settings and the FPGA symbol rate configuration",
                              "Check the interpolation setting of the DAC38J82"], "frequency"))

    # ---- per modulation quality
    bpsk, oq = mods.get("mod_BPSK"), mods.get("mod_OQPSK")
    for key, r in mods.items():
        mod = r["expected"]
        det = r.get("detected")
        mdet = r["mer_by_mod"].get(det) if det else None
        if det and det != mod and _finite(mdet) and mdet >= 15 and mdet > (r.get("mer") or 0) + 3:
            if mod in ("OQPSK", "QPSK") and det == "BPSK":
                F.append(Finding("fail", f"Only the I rail carries data ({mod} expected, BPSK-like signal)",
                                 f"MER as {mod} {r.get('mer', float('nan')):.1f} dB, as BPSK {mdet:.1f} dB: the Q rail "
                                 f"is silent - either the FPGA sends BPSK, or the Q path (DAC channel B / ADMV1013 Q) is dead.",
                                 {"DAC": 0.4, "FPGA": 0.35, "MIX": 0.25},
                                 ["Check the modulation command / CONFIG", "Check DAC38J82 IOUTB± with a scope",
                                  "Check the Q baseband path to the ADMV1013"], mod))
            else:
                F.append(Finding("fail", f"Wrong modulation: {mod} expected, {det} received",
                                 f"MER as {mod} {r.get('mer', float('nan')):.1f} dB, as {det} {mdet:.1f} dB.",
                                 {"FPGA": 0.8, "OUT": 0.05},
                                 ["Check the CAN command / CONFIG of the modulation mode",
                                  "Check the FPGA firmware version (Soft_MORE_Ver)"], mod))
            continue
        mer = r.get("mer", float("nan"))
        mer_min = lim.get("mer_min", 20.0)
        if _finite(mer) and mer < mer_min:
            # explain the MER loss by the impairments present
            causes = []
            if _finite(r.get("phase_err_res_deg")) and r["phase_err_res_deg"] > lim.get("phase_err_max", 3):
                causes.append("phase noise")
            if _finite(r.get("compression_db")) and r["compression_db"] > lim.get("compression_max_db", 0.8):
                causes.append("compression")
            if _finite(r.get("offset_dbc")) and r["offset_dbc"] > lim.get("iq_offset_max_dbc", -30):
                causes.append("carrier leakage")
            if _finite(r.get("gain_imb")) and (abs(r["gain_imb"]) > lim.get("gain_imb_max", 0.5) or
                                               abs(r.get("quad_err", 0)) > lim.get("quad_err_max", 3)):
                causes.append("I/Q imbalance")
            if _finite(r.get("snr_floor")) and r["snr_floor"] < mer + 6:
                causes.append("low signal-to-noise at the FSW")
            F.append(Finding("fail", f"{mod}: MER {mer:.1f} dB below {mer_min:g} dB",
                             ("Main contributors: " + ", ".join(causes) + ".") if causes else
                             "No single impairment dominates: broadband noise / distortion in the chain.",
                             {} if causes else {"DAC": 0.25, "MIX": 0.25, "PA": 0.25, "CLK": 0.25},
                             ["See the findings below for each contributor"] if causes else
                             ["Check the DAC full-scale current and the ADMV1013 drive level",
                              "Check the FSW level (ref level / attenuation) - overload or noise"], mod))
        # phase noise
        pe, ps = r.get("phase_err_res_deg"), r.get("phase_slow_deg")
        if _finite(pe) and pe > lim.get("phase_err_max", 3) and _finite(r.get("mer")) and r["mer"] >= 10:
            slow = _finite(ps) and ps > 0.6 * pe
            F.append(Finding("fail" if pe > 2 * lim.get("phase_err_max", 3) else "warn",
                             f"{mod}: phase noise / jitter ({pe:.1f}° RMS)",
                             ("Mostly slow phase wander → close-in noise of the LO loop (LMX2594 loop filter, charge "
                              "pump, reference)." if slow else
                              "Fast phase jitter → VCO noise / spurs of the LMX2594 or clock jitter on the DAC.")
                             + f" Magnitude error only {r.get('mag_err_pct', 0):.1f} %.",
                             {"LMX": 0.6, "REF": 0.2 if slow else 0.05, "CLK": 0.1 if slow else 0.3},
                             ["Check LMX2594 VCO calibration / lock (pll_lmx (mod))", "Check the LO loop filter components",
                              "Run the 'Carrier (CW)' test: integrated phase noise without modulation"], mod))
        if _finite(r.get("mer_corr")) and _finite(r.get("mer")) and r["mer_corr"] - r["mer"] > 3:
            F.append(Finding("info", f"{mod}: MER would be {r['mer_corr']:.1f} dB after I/Q correction",
                             f"Now {r['mer']:.1f} dB: {r['mer_corr'] - r['mer']:.1f} dB are lost to I/Q imbalance and "
                             f"carrier leakage, which the DAC38J82 QMC can correct.", {"DAC": 0.2, "MIX": 0.2},
                             ["See the Corrections tab"], mod))
        # carrier leakage
        off = r.get("offset_dbc")
        if _finite(off) and off > lim.get("iq_offset_max_dbc", -30):
            F.append(Finding("fail" if off > lim.get("iq_offset_max_dbc", -30) + 6 else "warn",
                             f"{mod}: carrier (LO) leakage {off:.1f} dBc",
                             "DC offset at the modulator input or LO feed-through in the ADMV1013 → a line at the "
                             "carrier frequency.", {"MIX": 0.5, "DAC": 0.45},
                             ["Null it with the DAC38J82 QMC offset registers (see Corrections)",
                              "If it does not null: ADMV1013 LO feed-through / damaged device",
                              "Check the DC coupling / bias network between DAC and ADMV1013"], mod))
        # I/Q imbalance
        gi, qe = r.get("gain_imb"), r.get("quad_err")
        if _finite(gi) and _finite(qe) and (abs(gi) > lim.get("gain_imb_max", 0.5) or abs(qe) > lim.get("quad_err_max", 3)):
            big = abs(gi) > 2 * lim.get("gain_imb_max", 0.5) or abs(qe) > 2 * lim.get("quad_err_max", 3)
            F.append(Finding("fail" if big else "warn", f"{mod}: I/Q imbalance (gain {gi:+.2f} dB, quadrature {qe:+.1f}°)",
                             f"Image rejection {r.get('irr', float('nan')):.1f} dB. Unequal I and Q paths: DAC channel "
                             f"gain, filter / balun mismatch or ADMV1013 quadrature.",
                             {"MIX": 0.45, "DAC": 0.35, "FPGA": 0.2},
                             ["Apply the QMC gain / phase corrections (see Corrections)",
                              "If |gain| > 1 dB: check the Q baseband path (DAC IOUTB, filter, resistors)"], mod))
        # compression
        c = r.get("compression_db")
        sp = r.get("spectrum", {})
        if _finite(c) and c > lim.get("compression_max_db", 0.8):
            F.append(Finding("fail" if c > 2 * lim.get("compression_max_db", 0.8) else "warn",
                             f"{mod}: PA compression ({c:.1f} dB PAPR loss)",
                             f"Peak-to-average {r['papr']:.2f} dB vs {r['papr_ref']:.2f} dB for an ideal signal"
                             + (f"; ACLR {sp.get('aclr', float('nan')):.1f} dB" if _finite(sp.get("aclr")) else "") + ".",
                             {"PA": 0.65, "ATT": 0.15, "MIX": 0.15, "PWR": 0.05},
                             ["Increase the attenuation by 3…6 dB and re-measure: compression must disappear",
                              "Check the CHA3397 drain voltage / gate bias", "Check that the output is matched (VSWR)"], mod))
        if not (_finite(mer) and mer >= 10):
            continue                                      # garbage signal: the rest is not meaningful
        # spectrum
        if _finite(sp.get("aclr")) and sp["aclr"] < lim.get("aclr_min", 30):
            F.append(Finding("warn", f"{mod}: ACLR {sp['aclr']:.1f} dB below {lim.get('aclr_min', 30):g} dB",
                             "Spectral regrowth in the adjacent channels: non-linearity or missing pulse shaping.",
                             {"PA": 0.45, "FPGA": 0.25, "MIX": 0.2, "DAC": 0.1},
                             ["Compare at a higher attenuation (PA non-linearity disappears, filter problems stay)"], mod))
        if _finite(sp.get("obw_ratio")) and not 0.85 < sp["obw_ratio"] < 1.15:
            F.append(Finding("warn", f"{mod}: occupied bandwidth {sp['obw'] / 1e6:.3f} MHz "
                                     f"({sp['obw_ratio'] * 100:.0f} % of nominal)",
                             "Wider: pulse shaping (RRC) off / wrong roll-off or strong distortion. Narrower: wrong "
                             "symbol rate configured.", {"FPGA": 0.6, "PA": 0.2, "DAC": 0.2},
                             ["Check the FPGA RRC filter / roll-off and the symbol rate in CONFIG"], mod))
        if _finite(sp.get("asym_db")) and abs(sp["asym_db"]) > lim.get("asym_max_db", 1.0):
            F.append(Finding("warn", f"{mod}: spectrum asymmetry {sp['asym_db']:+.1f} dB",
                             "Upper and lower half of the spectrum differ: frequency-dependent I/Q imbalance "
                             "(I/Q timing skew, baseband filter mismatch) or a tilted RF response.",
                             {"MIX": 0.35, "DAC": 0.3, "FPGA": 0.2, "OUT": 0.15},
                             ["Check the I/Q baseband filters and trace lengths", "Check the DAC lane / channel delay"], mod))
        if _finite(sp.get("spur_max_dbc")) and sp["spur_max_dbc"] > lim.get("spur_max_dbc", -40):
            s0 = sp["spurs"][0]
            F.append(Finding("warn", f"{mod}: spur {s0[1]:.1f} dBc at {s0[0] / 1e6:+.3f} MHz",
                             "Discrete line next to the signal: LMX2594 fractional / integer-boundary spur, DAC "
                             "clock coupling or switching regulator.", {"LMX": 0.4, "CLK": 0.25, "PWR": 0.2, "DAC": 0.15},
                             ["Note the offset: if it moves with the carrier setting it is a synthesizer spur",
                              "Compare with the DC/DC switching frequency"], mod))

    # ---- carrier (CW) purity
    if cw:
        if _finite(cw.get("spur_max_dbc")) and cw["spur_max_dbc"] > lim.get("spur_max_dbc", -40):
            f0, lv = cw["spurs"][0]
            F.append(Finding("warn", f"Carrier: spur {lv:.1f} dBc at {f0 / 1e3:+.1f} kHz",
                             "Discrete line next to the unmodulated carrier: synthesizer (LMX2594 fractional / "
                             "integer-boundary) spur, reference or clock coupling, or a switching regulator.",
                             {"LMX": 0.45, "CLK": 0.2, "PWR": 0.2, "REF": 0.15},
                             ["Note the offset: if it moves with the frequency code N it is a synthesizer spur",
                              "Compare with the DC/DC switching frequency"], "CW"))
        if _finite(cw.get("pn_deg")) and cw["pn_deg"] > lim.get("pn_max_deg", 1.5):
            F.append(Finding("warn", f"Carrier: integrated phase noise {cw['pn_deg']:.2f}° RMS",
                             f"Limit {lim.get('pn_max_deg', 1.5):g}°, band {cw['pn_band'][0] / 1e3:g}…"
                             f"{cw['pn_band'][1] / 1e3:g} kHz: noisy LO.", {"LMX": 0.6, "REF": 0.25, "CLK": 0.15},
                             ["Check the LMX2594 loop filter and VCO calibration", "Check the 10 MHz reference"], "CW"))

    for key, r in mods.items():
        fw = r.get("freq_wander") or {}
        if fw and (fw["pp_hz"] > lim.get("wander_pp_hz", 500) or fw["max_rate_hz_s"] > lim.get("wander_rate_hz_s", 100)):
            F.append(Finding("warn", f"{r['expected']}: carrier frequency wanders {fw['pp_hz']:.0f} Hz p-p "
                                     f"(up to {fw['max_rate_hz_s']:.0f} Hz/s)",
                             f"Measured over {fw['span_s']:.1f} s in 10 ms windows (the receiver tracks it, so decoding "
                             f"works). The protocol allows ≤ 100 Hz/s for the reference adjustment (dF, table 7.1). "
                             f"Either the BOS reference / LO drifts (Err_Ref_LO tuning, temperature, LMX2594), or "
                             f"the FSW runs on a different 10 MHz than the BOS.",
                             {"REF": 0.5, "LMX": 0.3, "OUT": 0.2},
                             ["Lock the FSW to the same 10 MHz as the BOS (TFG6806 -> FSW REF IN) and repeat",
                              "If it stays: watch Err_Ref_LO / Sour_10MHz in the telemetry during the capture",
                              "Then check the LMX2594 loop and the reference oscillator"], r["expected"]))
        if _finite(r.get("low_sps")):
            F.append(Finding("info", f"{r['expected']}: only {r['low_sps']:.1f} samples per symbol in the capture",
                             "The FSW bandwidth (0.8 × sample rate) cuts the rectangular SP-L pulses: even a perfect "
                             "signal then measures only ~28 dB MER (2 samples/symbol). Decoding is not affected. "
                             "Capture with ≥ 8 samples per symbol (УР: sample rate ≥ 16 × data rate).", {}, [],
                             r["expected"]))

    # ---- УР data path (decoded КБД)
    for key in ("ber", "mod_BPSK"):
        d = res.get(key) if key == "ber" else (res.get(key) or {}).get("decode")
        if not d or "frames" not in d:
            continue
        mer_ok = bpsk is not None and _finite(bpsk.get("mer")) and bpsk["mer"] >= lim.get("mer_min", 20)
        if d["n_frames"] == 0 and mer_ok:
            F.append(Finding("fail", "УР: no frames found although the BPSK signal is clean",
                             f"MER {bpsk['mer']:.1f} dB but the synchromarker 0x1ACFFC1D is not in the bit stream "
                             f"(SP-L violations {100 * d['manchester_violations']:.0f} %): data format, rate or framing "
                             f"in the FPGA is wrong, or the buffer sends no КБД.",
                             {"FPGA": 0.8, "OUT": 0.05}, ["Check the УР data rate (symbol rate = 2 × rate)",
                                                         "Check the SP-L coding and the КБД framing in the FPGA"], "data"))
        elif d["n_frames"]:
            if d["rs_fail"] or d["crc_fail"] or (_finite(d["ber"]) and d["ber"] > 1e-7 and mer_ok):
                F.append(Finding("fail", f"УР data errors with a clean RF signal ({key})",
                                 f"BER {d['ber']:.2e}, RS corrected {d['rs_corrected']} bytes, uncorrectable "
                                 f"{d['rs_fail']}, CRC errors {d['crc_fail']}"
                                 + (f", MER {bpsk['mer']:.1f} dB: the bits are wrong before modulation - digital "
                                    f"data path (FPGA coding / buffer / interface)." if mer_ok else "."),
                                 {"FPGA": 0.7, "DAC": 0.1, "PWR": 0.1} if mer_ok else {"FPGA": 0.3},
                                 ["Check the IPPD / MLVDS data input and the КБД buffer",
                                  "Check the RS encoder setting (K7/8 mode) in the FPGA"], "data"))
            if d["idle_bad"]:
                F.append(Finding("warn", "Idle frame content differs from table 4.4",
                                 f"{d['idle_bad']} idle frame(s) with unexpected bytes.", {"FPGA": 0.6},
                                 ["Check the idle frame generator in the FPGA"], "data"))
            if d["vcfc_gaps"]:
                F.append(Finding("warn", f"Frame counter jumps ({d['vcfc_gaps']})",
                                 "The virtual channel frame counter is not continuous: frames lost in the buffer.",
                                 {"FPGA": 0.6}, ["Check the КБД buffer (≥ 37 КБД) and the IPPD interface"], "data"))
            if not d["scid_ok"]:
                F.append(Finding("warn", f"Spacecraft ID {d['scid']} (expected {cfg.get('scid', 0)})",
                                 "SCID in the transport frames differs from the setting.", {"FPGA": 0.3},
                                 ["Check the SCID stored in the BOS non-volatile memory"], "data"))
        break

    # ---- ШР packet (despread / turbo decoded)
    s = res.get("shr")
    if s and "error" not in s:
        oq_ok = oq is not None and _finite(oq.get("mer")) and oq["mer"] >= lim.get("mer_min", 20)
        for key, what, prs in (("despread_eff", "preamble", "PRS 3 (preamble, 0x00008805)"),
                               ("cbd_eff", "КБД", "PRS 1 ⊕ PRS 2 (КБД, 0x00800043 / 0x00400010)")):
            eff = s.get(key)
            if _finite(eff) and eff < 0.7:
                F.append(Finding("fail", f"ШР: spreading sequence of the {what} does not match",
                                 f"Despreading recovers only {100 * eff:.0f} % of the chip energy: the {prs} in the BOS "
                                 f"differs from the expected one (polynomial, initial state, chip order or chip rate). "
                                 f"With a strong signal the bits may still decode, but a ground station would lose "
                                 f"{-20 * math.log10(max(eff, 1e-3)):.0f} dB of processing gain.",
                                 {"FPGA": 0.75, "CLK": 0.1}, ["Check the PRS polynomial / register settings (ИОК_КОНФИГ "
                                                             "Polyn / Registr) and the FPGA PRS generator"], "ШР"))
        if not s.get("preamble_found"):
            F.append(Finding("fail" if oq_ok else "warn", "ШР: preamble not found by despreading",
                             "No preamble (PRS 3, 0x00008805) in the capture"
                             + (" although the OQPSK signal itself is clean: wrong spreading sequence, preamble "
                                "value, chip rate or rate setting in the FPGA (or the capture starts after the "
                                "preambles)." if oq_ok else "."),
                             {"FPGA": 0.7, "CLK": 0.1} if oq_ok else {"FPGA": 0.3},
                             ["Check PRS polynomial / initial state and the preamble value (ИОК_КОНФИГ)",
                              "Use the ШР test (it triggers on the RF onset) or the preamble-only mode"], "ШР"))
        elif s.get("preamble_found") and s.get("marker") is False:
            F.append(Finding("warn", "ШР: end-of-preamble marker 0xDA2B1E5E not found", "Preambles are present but "
                             "no marker followed within the capture.", {"FPGA": 0.5},
                             ["Make the capture longer / check the packet type"], "ШР"))
        if s.get("frames"):
            if s.get("crc_fail") or (_finite(s.get("ber")) and s["ber"] > 1e-4):
                F.append(Finding("fail", "ШР: data errors after despreading / turbo decoding",
                                 f"{s['crc_fail']} of {s['n_frames']} frame(s) with CRC errors, code-bit BER "
                                 f"{s.get('ber', float('nan')):.2e}.",
                                 {"FPGA": 0.6, "CLK": 0.2, "DAC": 0.2},
                                 ["Check the turbo encoder / randomizer / PRS 1, 2 settings in the FPGA"], "ШР"))
            if s.get("idle_bad"):
                F.append(Finding("warn", "ШР: idle frame content differs from table 4.4", "", {"FPGA": 0.6}, [], "ШР"))
        elif s.get("marker") and s.get("cbd_bits", 0) >= 64 + 17848:
            F.append(Finding("fail", "ШР: КБД not decodable after the marker", "The marker was found but no КБД "
                             "followed in the expected form.", {"FPGA": 0.6}, ["Check PRS 1/2 and the КБД format"], "ШР"))

    # ---- BPSK vs OQPSK: which rail is bad
    if bpsk and oq and _finite(bpsk.get("mer")) and _finite(oq.get("mer")):
        pn_oq = _finite(oq.get("phase_err_res_deg")) and oq["phase_err_res_deg"] > lim.get("phase_err_max", 3)
        if bpsk["mer"] - oq["mer"] > 6 and bpsk["mer"] >= lim.get("mer_min", 20) and \
                oq["mer"] < lim.get("mer_min", 20) and not pn_oq:
            F.append(Finding("fail", "Q path fault (BPSK good, OQPSK bad)",
                             f"BPSK (I rail only) {bpsk['mer']:.1f} dB but OQPSK {oq['mer']:.1f} dB → the Q rail "
                             f"(DAC channel B, its JESD lanes or the ADMV1013 Q input) is faulty.",
                             {"DAC": 0.4, "MIX": 0.3, "FPGA": 0.3},
                             ["Check DAC38J82 channel B output (IOUTB±) with a scope",
                              "Check the Q baseband filter / balun and the ADMV1013 Q inputs",
                              "Check FPGA lane mapping for Q"], "comparison"))

    # ---- output power
    pw_r = cw or bpsk or oq
    if pw_r and _finite(pw_r.get("power")) and _finite(cfg.get("power_nom")):
        dp = pw_r["power"] - cfg["power_nom"]
        tol = lim.get("power_tol_db", 1.5)
        a = res.get("att")
        att_ok = a and "bits" in a and all(b["state"] == "ok" for b in a["bits"])
        if dp < -tol:
            F.append(Finding("fail" if dp < -3 * tol else "warn", f"Output power low by {-dp:.1f} dB",
                             f"{pw_r['power']:.2f} dBm vs nominal {cfg['power_nom']:.2f} dBm (incl. {cfg.get('loss_db', 0):g} dB "
                             f"cable loss).", {"PA": 0.4, "ATT": 0.05 if att_ok else 0.25, "OUT": 0.25, "MIX": 0.1},
                             ["Check the cable loss setting and the connectors", "Check the PA drain current / bias",
                              "Run the attenuator test" if not att_ok else "Attenuator verified OK - look at the PA"],
                             "power"))
        elif dp > tol:
            F.append(Finding("warn", f"Output power high by {dp:.1f} dB",
                             f"{pw_r['power']:.2f} dBm vs nominal {cfg['power_nom']:.2f} dBm.",
                             {"ATT": 0.5, "OUT": 0.3, "PA": 0.2},
                             ["Check the attenuator code sent by the FPGA (bit stuck at 0?)", "Check the cable loss setting"],
                             "power"))

    # ---- attenuator
    a = res.get("att")
    if a and "bits" in a and lo_unlocked:
        F.append(Finding("info", "Attenuator results not evaluated",
                         "The LO is unlocked, so the carrier level is not stable enough to judge the attenuator bits.",
                         {}, ["Repeat the attenuator test once the LMX2594 locks"], "attenuator"))
    elif a and "bits" in a:
        for b in a["bits"]:
            if b["state"] == "no effect (stuck / not driven)":
                F.append(Finding("fail", f"Attenuator {b['nominal']:g} dB step has no effect",
                                 f"Measured weight {b['measured']:.2f} dB.",
                                 {"ATT": 0.75, "FPGA": 0.25},
                                 [f"Check the ADRF5730 control line(s) for the {b['nominal']:g} dB step from the FPGA",
                                  "Check the ADRF5730 soldering / replace"], "attenuator"))
            elif b["state"] == "weight error":
                F.append(Finding("warn", f"Attenuator {b['nominal']:g} dB step measures {b['measured']:.2f} dB",
                                 f"Error {b['error']:+.2f} dB.", {"ATT": 0.6, "OUT": 0.2, "PA": 0.2},
                                 ["Use a calibration table, or check for compression at low attenuation"], "attenuator"))

    # ---- LO harmonics / sub-harmonics
    for h in (res.get("harm") or {}).get("lines", []):
        if _finite(h.get("dbc")) and h["dbc"] > lim.get("lo_harm_max_dbc", -40):
            F.append(Finding("warn", f"LO-related line at {h['f'] / 1e9:.4f} GHz: {h['dbc']:.1f} dBc ({h['name']})",
                             "Leakage of the LO or its multiples through the ADMV1013 ×4 multiplier and output.",
                             {"MIX": 0.55, "PA": 0.15, "OUT": 0.15, "LMX": 0.15},
                             ["Check the ADMV1013 multiplier filter / LO drive level", "Check the output filter"],
                             "spurious"))

    # ---- supply current
    sup = res.get("supply")
    if sup and _finite(sup.get("i_off")) and _finite(sup.get("i_on")):
        di = sup["i_on"] - sup["i_off"]
        if di < lim.get("pa_delta_i_min", 0.05):
            F.append(Finding("fail" if not any(not r.get("present", True) for _, r in caps) else "warn",
                             "PA current does not rise with RF on",
                             f"Supply current {sup['i_off']:.3f} A (RF off) → {sup['i_on']:.3f} A (RF on).",
                             {"PA": 0.6, "PWR": 0.25, "FPGA": 0.15},
                             ["Check the PA gate bias / drain switch and its enable from the FPGA"], "supply"))
        if sup["i_on"] > lim.get("i_max", 5.0):
            F.append(Finding("fail", "Supply current too high", f"{sup['i_on']:.3f} A (limit {lim.get('i_max', 5.0):g} A).",
                             {"PA": 0.5, "PWR": 0.4, "FPGA": 0.1},
                             ["Switch off and check for shorts / PA oscillation"], "supply"))

    # ---- drift
    dr = res.get("drift")
    if dr and len(dr.get("t", [])) >= 3:
        dpw = float(np.ptp(dr["power"])) if len(dr["power"]) else 0.0
        dcf = float(np.ptp(dr["cfe"])) if len(dr["cfe"]) else 0.0
        if dpw > lim.get("drift_power_db", 1.0):
            temp = dr.get("temp") or []
            F.append(Finding("warn", f"Output power drifts {dpw:.2f} dB during the run",
                             (f"Temperature changed {np.ptp(temp):.0f} °C. " if len(temp) > 1 else "")
                             + "Thermal gain drift of the PA or a marginal connection.",
                             {"PA": 0.55, "OUT": 0.3, "PWR": 0.15}, ["Check PA heat sinking", "Re-seat the RF cable"],
                             "drift"))
        if dcf > lim.get("drift_cfe_hz", 2e3):
            F.append(Finding("warn", f"Carrier frequency drifts {dcf:.0f} Hz during the run",
                             "Reference / LO not stable over time or temperature.", {"REF": 0.6, "LMX": 0.4},
                             ["Check the reference oscillator warm-up / external 10 MHz"], "drift"))
    if not any(f.sev in ("fail", "warn") for f in F):
        F.append(Finding("ok", "No faults found", "All measured parameters are within limits.", {}, [], ""))
    return F


# --------------------------------------------------------------------------- #
#  Simulated BOS (demo mode, operator training and tests)                      #
# --------------------------------------------------------------------------- #
FAULTS = [  # key, label, default magnitude, unit
    ("ref_ppm", "Internal 10 MHz oscillator offset", 3.0, "ppm"),
    ("tfg_ppm", "External 10 MHz (TFG6806) offset", 3.0, "ppm"),
    ("ref_lost", "External 10 MHz missing", 1, ""),
    ("lo_hz", "LMX2594 frequency error", 150e3, "Hz"),
    ("lo_unlock", "LMX2594 unlocked", 1, ""),
    ("clk_ppm", "DAC / FPGA clock error", 120.0, "ppm"),
    ("jesd", "JESD204B link error", 1, ""),
    ("gain_imb", "Q path gain error", 2.0, "dB"),
    ("quad", "Quadrature error", 8.0, "deg"),
    ("leak", "LO leakage", -22.0, "dBc"),
    ("pn", "Excess phase noise", 6.0, "deg"),
    ("comp", "PA compression (backoff)", 0.5, "dB"),
    ("gain_low", "Low PA gain", 6.0, "dB"),
    ("att_bit", "Attenuator bit stuck (Atten_EIRP)", 3, "bit"),
    ("no_rf", "No RF output", 1, ""),
    ("wrong_mod", "Wrong modulation (QPSK sent)", 1, ""),
    ("spur", "Synthesizer spur", -32.0, "dBc"),
    ("rail", "1.8 V rail low", 1.70, "V"),
    ("hot", "Over-temperature", 85.0, "°C"),
    ("q_dead", "Q rail dead (DAC ch B)", 1, ""),
    ("data_err", "Data path bit errors (FPGA / buffer)", 1e-3, "BER"),
    ("no_rs", "Reed-Solomon check symbols missing", 1, ""),
    ("shr_prs", "ШР spreading sequence wrong (FPGA)", 1, ""),
]


class BosSimulator:
    """Simulated BOS output + telemetry with injectable faults. mode: off / cw / BPSK / OQPSK."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.mode = "off"
        self.att_code = int(cfg.get("att_code", 0))
        self.faults: dict = {}
        self.rng = np.random.default_rng()
        self.t0 = 0.0
        self.freq = None                  # carrier set by the last session command (protocol mode)
        self.angle_t = None               # last target designation (protocol mode)
        self.sr_t = 0.0
        self.time_snr = 30
        self.protocol = False             # True once driven by protocol commands (designation required)

    def att_db(self):
        code = min(ATT_MAX, self.att_code)
        if "att_bit" in self.faults:
            code &= ~(1 << int(self.faults["att_bit"]))
        return sum(w for b, w in enumerate(ATT_BITS) if code >> b & 1)

    # ---- protocol (bos_protocol): the simulated unit executes the real command frames
    def handle(self, code, data, mapping):
        """Execute a command; returns True (КВ+) / False (КВ-)."""
        import bos_protocol as bp
        if code not in bp.COMMANDS:
            return False
        if code == 0x01:
            self.mode = "off"
        elif code == 0x04:
            self.time_snr = data[0]
        elif code == 0x0C:
            self.angle_t = time.monotonic()
        elif code in bp.SR_COMMANDS:
            p = bp.parse_session(data)
            self.att_code = p["atten_db"]
            self.freq = bp.freq_hz(p["freq_n"])
            self.sr_t = time.monotonic()
            self.protocol = True
            self.mode = "cw" if code == 0xA0 else mapping.get(code, "OQPSK")
        return True

    def _session_alive(self):
        """The real BOS ends the session without valid target designation (3 s) or after Time_SNR."""
        if self.mode == "off" or not self.protocol:
            return True
        now = time.monotonic()
        if self.angle_t is None or now - self.angle_t > 3.0 or (self.time_snr and now - self.sr_t > self.time_snr):
            self.mode = "off"
            return False
        return True

    def config(self):
        """136-byte КОНФИГ as the simulated unit would report it (with CRC)."""
        import bos_protocol as bp
        c = bytearray(136)
        c[2] = 0x80 | 0x40 | 0x20                     # user config, external 10 MHz, randomisation
        c[3] = 0xE0
        c[4] = self.time_snr or 30
        c[5], c[6] = 0x4B, 0x04
        c[7:9] = (0x0300).to_bytes(2, "little")
        c[9:13] = (0xC6DFA611).to_bytes(4, "little")
        c[13:17] = (1).to_bytes(4, "little")
        c[17:21] = (0x8805).to_bytes(4, "little")
        c[49:53] = c[53:57] = (1).to_bytes(4, "little")
        c[57:61], c[61:65] = (0x800043).to_bytes(4, "little"), (0x400004).to_bytes(4, "little")
        c[121], c[122], c[123], c[124] = 1, 1, 1, 0
        c[125] = c[126] = c[127] = 70
        c[134:136] = bp.crc16(bytes(c[:134])).to_bytes(2, "little")
        return bytes(c)

    def power_dbm(self):
        self._session_alive()
        if self.mode == "off" or "no_rf" in self.faults:
            return -200.0
        att = self.att_db()
        p = self.cfg.get("power_nom", 10.0) + self.cfg.get("att_code", 0) - att
        p -= self.faults.get("gain_low", 0.0)
        return p - self.cfg.get("loss_db", 0.0)

    def capture(self, center, fs, n, sr=None, filt=None):
        cfg = self.cfg
        filt = filt or "RRC"
        sr = sr or cfg.get("symbol_rate") or cfg.get("sr_oqpsk", 1e6)
        rng = self.rng
        noise_dbm = -95.0 + 10 * math.log10(fs / 1e6)          # FSW noise in the capture bandwidth
        nv = math.sqrt(10 ** (noise_dbm / 10) * 1e-3 * iq_demod.R_LOAD / 2)
        noise = nv * (rng.normal(size=n) + 1j * rng.normal(size=n))
        p = self.power_dbm()
        if p < -150:
            return noise
        ppm_int = self.faults.get("ref_ppm", 0.0) + self.rng.normal(0, 0.02)     # BOS oscillator (digital part)
        ext = "ref_ppm" not in self.faults and "ref_lost" not in self.faults       # RF locked to the TFG6806
        ppm = (self.faults.get("tfg_ppm", 0.0) + self.rng.normal(0, 0.005)) if ext else ppm_int
        f_set = self.freq or cfg["carrier"]
        cfe = f_set * ppm * 1e-6 + self.faults.get("lo_hz", 0.0) + (f_set - center)
        if "lo_unlock" in self.faults:
            cfe += rng.uniform(-3e5, 3e5)
        clk = ppm_int + self.faults.get("clk_ppm", 0.0)
        if abs(cfe) > fs / 2:
            return noise
        if self.mode == "cw":
            s = np.ones(n, complex)
        else:
            mod = "QPSK" if "wrong_mod" in self.faults else self.mode
            sps = int(round(fs / sr))
            if abs(fs / sr - sps) > 1e-6 or sps < 2:      # FSW rate not a multiple: generate at 4 sps, resample
                n4 = int(math.ceil(n * 4 * sr / fs)) + 64
                s4 = iq_demod.simulate(mod, sr, 4, n4 // 4 + 1, snr_db=80, cfo=0.0, alpha=cfg["alpha"], filt=filt,
                                       clock_ppm=clk, delay=rng.random(), phase=0.0, rng=rng)
                s = resample(s4, 4 * sr, fs)[0][:n]
            elif mod == "BPSK" and sps >= 2:
                s = iq_demod.simulate(mod, sr, sps, n // sps + 1, snr_db=80, cfo=0.0, alpha=cfg["alpha"], filt=filt,
                                      clock_ppm=clk, delay=rng.random(), phase=0.0, rng=rng,
                                      symbols=self.ur_symbols(n // sps + 61))[:n]
            else:
                s = iq_demod.simulate(mod, sr, sps, n // sps + 1, snr_db=80, cfo=0.0, alpha=cfg["alpha"], filt=filt,
                                      clock_ppm=clk, delay=rng.random(), phase=0.0, rng=rng)[:n]
            if len(s) < n:
                s = np.r_[s, np.zeros(n - len(s))]
            if "jesd" in self.faults:                       # garbage samples: noise-like spectrum
                s = rng.normal(size=n) + 1j * rng.normal(size=n)
            s = s / math.sqrt(np.mean(np.abs(s) ** 2))
            if "q_dead" in self.faults:
                s = s.real + 0j
            g = 10 ** (self.faults.get("gain_imb", 0.0) / 20)
            phi = math.radians(self.faults.get("quad", 0.0))
            i, q = s.real, s.imag
            s = i + 1j * g * (q * math.cos(phi) + i * math.sin(phi))
        if "leak" in self.faults:
            s = s + 10 ** (self.faults["leak"] / 20) * math.sqrt(np.mean(np.abs(s) ** 2))
        if "comp" in self.faults:                           # Rapp PA model, smoothness 2
            a = np.abs(s)
            backoff = self.faults["comp"] + self.att_db() - self.cfg.get("att_code", 0)   # ATT is before the PA
            sat = 10 ** (backoff / 20) * math.sqrt(np.mean(a ** 2))
            p0 = np.mean(np.abs(s) ** 2)
            s = s / (1 + (a / sat) ** 4) ** 0.25
            p += 10 * math.log10(np.mean(np.abs(s) ** 2) / p0)          # gain compression
        t = np.arange(n) / fs
        ph = 2 * np.pi * cfe * t
        pn_deg = 0.3 + self.faults.get("pn", 0.0) + (40.0 if "lo_unlock" in self.faults else 0.0)
        walk = np.cumsum(rng.normal(size=n))
        walk = np.convolve(walk - np.mean(walk), np.ones(64) / 64, mode="same")
        walk = walk / (np.std(walk) or 1) * math.radians(pn_deg)
        s = s * np.exp(1j * (ph + walk + rng.random() * 6.28))
        if "spur" in self.faults:
            s = s + 10 ** (self.faults["spur"] / 20) * np.exp(2j * np.pi * (cfe + 1.37 * sr) * t)
        s = s / math.sqrt(np.mean(np.abs(s) ** 2)) * math.sqrt(10 ** (p / 10) * 1e-3 * iq_demod.R_LOAD)
        bw = cfg.get("fsw_bw")
        if bw and bw < fs:                                 # the FSW I/Q bandwidth
            s = band_limit(s, fs, bw)
        return s + noise

    def tone(self, f):
        """Level of an LO-related line at f (for the harmonics test), dBm at the FSW."""
        p = self.power_dbm()
        if p < -150:
            return -120.0
        base = -55.0 if "leak" not in self.faults else -35.0
        return p + base + self.rng.normal(0, 0.5)

    def ur_symbols(self, nsym):
        """УР data: idle КБД (document 0202 table 4.4), SP-L coded; faults corrupt the data path."""
        import bos_decode as bd
        nfr = nsym // (2 * bd.CBD_UR * 8) + 2
        start = int(self.rng.integers(0, 2 * bd.CBD_UR * 8))
        self._vcfc = getattr(self, "_vcfc", 0)
        stream = []
        for k in range(nfr):
            stream.append(bd.cbd_ur(bd.idle_frame(self.cfg.get("scid", 0), (self._vcfc + k) & 0xFF)))
        self._vcfc = (self._vcfc + nfr) & 0xFF
        bits = bd.bytes_to_bits(b"".join(stream))
        if "data_err" in self.faults:                       # digital data path corrupts bits
            flip = self.rng.random(len(bits)) < self.faults["data_err"]
            bits = bits ^ flip.astype(np.uint8)
        if "no_rs" in self.faults:                          # check symbols not generated (zeros)
            b = bits.reshape(-1, bd.CBD_UR * 8).copy()
            b[:, (4 + bd.TF_LEN) * 8:] = 0
            bits = b.ravel()
        sym = bd.manchester_encode(bits)
        return np.roll(sym, -start)[:nsym]

    def capture_shr(self, fs, n, rate_kbps, kind="kbd", pre=None, n_pre=5):
        """ШР packet as the FSW would capture it: kind 'pre' = endless preambles (any phase),
        'kbd' = preambles + marker + 2 КБД starting at the RF onset (trigger)."""
        import bos_decode as bd
        import bos_shr
        sr = bos_shr.SYMBOL_RATE
        p = self.power_dbm()
        nv = math.sqrt(10 ** ((-95.0 + 10 * math.log10(fs / 1e6)) / 10) * 1e-3 * iq_demod.R_LOAD / 2)
        noise = (nv * (self.rng.normal(size=n) + 1j * self.rng.normal(size=n))).astype(np.complex64)
        if p < -150:
            return noise
        pre = pre or bos_shr.PREAMBLE
        if "shr_prs" in self.faults:                      # wrong spreading polynomial in the FPGA
            poly3 = bos_shr.POLY3
            bos_shr.POLY3 = 0x0000B400
        try:
            if kind == "pre":
                sfp = bos_shr.chips_per_bit(rate_kbps, True)
                nb = int(n / (fs / sr / 2) / sfp / 32) + 2
                chips = bos_shr.packet_chips(rate_kbps, nb, (), pre)[:-32 * sfp]   # no marker
                skip = int(self.rng.integers(0, 32 * sfp))
                chips = chips[skip:]
            else:
                self._vcfc = getattr(self, "_vcfc", 0)
                tfs = [bd.idle_frame(self.cfg.get("scid", 0), (self._vcfc + k) & 0xFF) for k in range(2)]
                chips = bos_shr.packet_chips(rate_kbps, n_pre, tfs, pre)
        finally:
            if "shr_prs" in self.faults:
                bos_shr.POLY3 = poly3
        if "data_err" in self.faults and kind != "pre":
            sf = bos_shr.chips_per_bit(rate_kbps)
            nbits = len(chips) // sf
            flip = np.repeat(self.rng.random(nbits) < self.faults["data_err"] * 20, sf)
            chips[:len(flip)][flip] *= -1
        w = bos_shr.oqpsk_waveform(chips, 4, self.cfg.get("alpha", 0.25))
        if abs(fs - 4 * sr) > 1:
            w = resample(w, 4 * sr, fs)[0].astype(np.complex64)
        if "q_dead" in self.faults:
            w = w.real.astype(np.complex64)
        lead = 2000 if kind == "kbd" else 0
        s = np.zeros(n, np.complex64)
        m = min(n - lead, len(w))
        s[lead:lead + m] = w[:m]
        ppm = self.faults.get("tfg_ppm", 0.0) + self.faults.get("ref_ppm", 0.0)
        cfe = (self.freq or self.cfg["carrier"]) * ppm * 1e-6 + self.faults.get("lo_hz", 0.0) + 3e3
        ph = (2 * np.pi * cfe / fs * np.arange(n) + self.rng.random() * 6.28).astype(np.float32)
        s = s * np.exp(1j * ph).astype(np.complex64) * np.float32(math.sqrt(10 ** (p / 10) * 1e-3 * iq_demod.R_LOAD))
        bw = self.cfg.get("fsw_bw")
        if bw and bw < fs:
            s = band_limit(s, fs, bw).astype(np.complex64)
        return s + noise

    def telemetry(self):
        f = self.faults
        tl = {"pll_lmk": 1, "pll_jesd": 1, "pll_all": 1, "pll_lmx (DAC)": 1,
              "pll_lmx (mod)": 0 if "lo_unlock" in f else 1, "jesd_err": 0 if "jesd" in f else 1,
              "vdd_1v": 1.0 + self.rng.normal(0, 0.003), "vdd_1_8v": f.get("rail", 1.8) + self.rng.normal(0, 0.004),
              "vdd_2_5v": 2.5 + self.rng.normal(0, 0.005),
              "Temp_DT1": f.get("hot", 41.0) + self.rng.normal(0, 0.3), "Temp_DT2": 38.0, "Temp_DT3": 36.0,
              "Stat_10MHz": 0 if "ref_lost" in f else 1,
              "Sour_10MHz": 0 if ("ref_lost" in f or "ref_ppm" in f) else 1,
              "Err_Ref_LO": int(f.get("ref_ppm", 0) * 300) + int(self.rng.integers(-2, 3)),
              "PulsNumb_10MHz": 0 if "ref_lost" in f else
              int(round(1e7 * (1 + f.get("tfg_ppm", 0.0) * 1e-6))) + int(self.rng.integers(-1, 2)),
              "Mode": 0 if self.mode == "off" else 1, "Mode_SNR": 0x40 if self.mode == "cw" else 0xA4}
        return tl

    def supply_current(self):
        i = 0.38
        if self.mode != "off" and "no_rf" not in self.faults:
            i += 0.62
        return i + self.rng.normal(0, 0.005)


# --------------------------------------------------------------------------- #
#  Offline analysis of an I/Q recording                                        #
# --------------------------------------------------------------------------- #
UR_RATES_KBPS = (8, 16, 32, 64, 128, 256, 512)
SHR_RATES_KBPS = (16, 32, 64, 128, 256, 512, 1024)


def sample_blocks(x, n_blocks=64, blk=65536):
    """Up to n_blocks blocks of blk samples spread over x (complex64), for statistics of long records
    without touching (or copying) all of it."""
    n = len(x)
    if n <= n_blocks * blk:
        return np.asarray(x, np.complex64)
    starts = np.linspace(0, n - blk, n_blocks).astype(np.int64)
    return np.concatenate([np.asarray(x[s:s + blk], np.complex64) for s in starts])


def identify(x, fs) -> dict:
    """Rough signal type from the spectrum: off / cw / narrow (УР BPSK) / wide (ШР OQPSK).
    Long records: spectrum of blocks spread over the record (no full copy)."""
    xs = sample_blocks(x)
    pw = np.abs(xs) ** 2
    if len(xs) > 4096:                                   # only where the transmitter is on
        k = 1024
        m = len(xs) // k * k
        bp = pw[:m].reshape(-1, k).mean(axis=1)
        on = np.repeat(bp > 0.25 * bp.max(), k)
        if on.sum() >= 4096:
            xs = xs[:m][on]
    f, p = psd(xs, fs, 4096 if len(xs) >= 65536 else 1024)
    tot = float(np.sum(p))
    if tot <= 0:
        return {"type": "off"}
    pk = int(np.argmax(p))
    tone = float(np.sum(p[max(0, pk - 3):pk + 4])) / tot
    # noise floor: the quietest 10 % of the bins (a wideband signal may fill most of the band,
    # then the median is the signal, not the noise)
    nf = float(np.percentile(p, 10))
    peak_db = 10 * math.log10(float(np.max(p)) / nf) if nf > 0 else 99
    if peak_db < 10:
        return {"type": "off", "obw": float("nan")}
    ps = np.clip(p - nf, 0, None)                        # occupied bandwidth above the floor
    c = np.cumsum(ps) / max(float(np.sum(ps)), 1e-300)
    obw = float(f[min(len(f) - 1, np.searchsorted(c, 0.995))] - f[np.searchsorted(c, 0.005)])
    fc = float(np.sum(f * ps) / max(float(np.sum(ps)), 1e-300))
    if tone > 0.7:
        return {"type": "cw", "obw": obw, "tone_frac": tone, "f_center": float(f[pk])}
    return {"type": "wide" if obw > 20e6 else "narrow", "obw": obw, "tone_frac": tone, "f_center": fc}


def block_power(x, blk=4096, step=1 << 22):
    """Mean |x|² per block of blk samples over the whole record, computed in pieces (memory-mapped
    multi-GB files: no full-size temporaries)."""
    n = len(x) // blk * blk
    out = np.empty(n // blk, np.float32)
    step = step // blk * blk
    for s in range(0, n, step):
        e = min(n, s + step)
        v = np.asarray(x[s:e], np.complex64)
        out[s // blk:e // blk] = (v.real ** 2 + v.imag ** 2).reshape(-1, blk).mean(axis=1)
    return out


def rf_end(x, frac=0.25, blk=4096) -> int:
    """Sample just after the transmission ends (block power below frac × the strong level), or len(x)."""
    bp = block_power(x, blk)
    if not bp.size:
        return len(x)
    idx = np.where(bp > frac * float(np.percentile(bp, 99.5)))[0]
    return len(x) if not len(idx) else int(min(len(x), (idx[-1] + 3) * blk))


def rf_onset(x, frac=0.25) -> int:
    """First sample where the (smoothed) power exceeds frac × its maximum (+ a short settling margin).
    Long records: block powers over the whole record locate the onset, then it is refined."""
    k = 256
    a0 = 0
    if len(x) > 4_000_000:
        blk = 4096
        bp = block_power(x, blk)
        if bp.size:
            ref = float(np.percentile(bp, 99.5))
            idx = np.where(bp > frac * ref)[0]
            if not len(idx) or idx[0] == 0:
                return 0
            a0 = max(0, (int(idx[0]) - 2) * blk)
    p = np.abs(np.asarray(x[a0:a0 + min(len(x) - a0, 4_000_000)], np.complex64)) ** 2
    sm = np.convolve(p, np.ones(k) / k, mode="same")
    idx = np.where(sm > frac * sm.max())[0]
    on = int(idx[0] + 2 * k) if len(idx) and (idx[0] > k or a0) else 0
    return int(min(len(x) - 1, a0 + on))


def analyze_file(x, fs, center, cfg, mode="auto", ur_rate=None, shr_rate=None, log=lambda s: None,
                 progress=None) -> dict:
    """Analyse a recording like a station capture. Returns {"cw"| "mod_BPSK"| "mod_OQPSK"| "shr": …,
    "_ident": …}. cfg: station settings (carrier, power_nom, sr_oqpsk, alpha, …)."""
    res = {}
    x = x if isinstance(x, np.ndarray) and x.dtype == np.complex64 else np.asarray(x, np.complex64)
    idt = identify(x, fs)
    res["_ident"] = idt
    kind = mode if mode != "auto" else {"cw": "cw", "narrow": "bpsk", "wide": "oqpsk"}.get(idt["type"], "off")
    res["_nominal"] = cfg.get("carrier")

    def fix_cfe(r):
        if r and _finite(r.get("cfe")) and center:
            f_meas = center + r["cfe"]
            if cfg.get("snap_grid", True):                 # nominal = nearest channel 37650 + 0.125·n MHz
                n = int(round((f_meas / 1e6 - 37650.0) / 0.125))
                nom = (37650.0 + 0.125 * n) * 1e6
                r["freq_code_n"] = n
            else:
                nom = cfg["carrier"]
            res["_nominal"] = nom
            r["carrier_measured"] = f_meas
            r["cfe_file"] = r["cfe"]
            r["cfe"] = f_meas - nom
            r["cfe_ppm"] = r["cfe"] / nom * 1e6
        return r
    if kind == "off":
        res["cw"] = {"kind": "cw", "present": False, "power": float("nan")}
        return res
    # narrowband signal in a wide recording: shift to the carrier and decimate first (fast, small)
    if kind in ("cw", "bpsk") and _finite(idt.get("obw")):
        fs_t = max(8 * idt["obw"], 4 * 2e3 * (ur_rate or 0),
                   4 * cfg.get("pn_hi", 100e3) if kind == "cw" else 0.0, 200e3)
        D = int(fs // fs_t)
        if D >= 2:
            f0 = float(idt.get("f_center") or 0.0)
            log(f"narrowband ({idt['obw'] / 1e3:.0f} kHz): shift {f0 / 1e3:+.1f} kHz, decimate ×{D} -> "
                f"{fs / D / 1e6:.3f} MS/s")
            x, fs = decimate(x, fs, f0, D, progress)
            center = (center + f0) if center else center
            res["_decimated"] = D
    if kind == "cw":
        res["cw"] = fix_cfe(analyze_cw(x, fs, cfg, (cfg.get("pn_lo", 1e3), cfg.get("pn_hi", 100e3))))
        return res
    if kind == "bpsk":
        rates = [ur_rate] if ur_rate else list(UR_RATES_KBPS)
        tried = []
        errs = []
        # the rate is the one at which КБД decode (synchromarker + RS); MER alone does not tell
        # (a BPSK signal also demodulates at 2×, 4× its symbol rate, and phase tracking helps every rate).
        # Candidate rates are tried in parallel threads, BPSK only (the modulation check comes after).
        def trial(rk):
            sr = 2e3 * rk
            c = dict(cfg, symbol_rate=sr, filter="NONE", decode_ur=True, try_mods=("BPSK",))
            n = min(len(x), int(3.2 * 2 * 8 * 1279 * fs / sr))
            return analyze_modulated(x[:n], fs, c, "BPSK")
        cand = [rk for rk in rates if fs >= 2 * 2e3 * rk and len(x) >= 200 * fs / (2e3 * rk)]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max(1, min(len(cand), 4))) as ex:
            futs = {rk: ex.submit(trial, rk) for rk in cand}
        for rk in cand:
            try:
                r = futs[rk].result()
            except Exception as e:
                errs.append(f"{rk} kbit/s: {type(e).__name__}: {e}")
                log(f"BPSK {rk} kbit/s: {type(e).__name__}: {e}")
                continue
            dd = r.get("decode") or {}
            ok = sum(1 for f in dd.get("frames", []) if f.get("rs_ok"))
            log(f"BPSK {rk} kbit/s: MER {r.get('mer', float('nan')):.1f} dB, frames {dd.get('n_frames', 0)} "
                f"(RS ok {ok})")
            if _finite(r.get("mer")):
                tried.append((rk, ok, dd.get("n_frames", 0), r["mer"]))
        if not tried:
            raise ValueError("no УР rate fits this recording (sample rate too low or too short)"
                             + (f"; {errs[-1]}" if errs else ""))
        best = max(tried, key=lambda t: (t[1], t[2], -t[0]))
        if best[2] == 0:                                   # nothing decodes: lowest rate that demodulates
            top = max(t[3] for t in tried)
            best = min((t for t in tried if t[3] >= top - 3), key=lambda t: t[0])
        rk = best[0]
        c = dict(cfg, symbol_rate=2e3 * rk, filter="NONE", decode_ur=True)
        r = fix_cfe(analyze_modulated(x, fs, c, "BPSK"))
        r["ur_rate_kbps"] = rk
        res["mod_BPSK"] = r
        return res
    # OQPSK (ШР): modulation quality on the first part, then the packet decoder
    sr = cfg.get("sr_oqpsk", 131.072e6)
    on = rf_onset(x)
    # ≥ 6000 symbols, and a whole number of resampling blocks (34375 samples at 275 MS/s)
    blk = resample_block(fs, sr * DEMOD_SPS) if abs(fs / sr - round(fs / sr)) > 1e-6 or round(fs / sr) < DEMOD_SPS else 1
    n = -(-max(int(6000 * fs / sr), blk) // blk) * blk
    if n > len(x) - on:
        on = max(0, len(x) - n)
    n = min(len(x) - on, n)
    c = dict(cfg, symbol_rate=sr, filter="RRC")
    res["mod_OQPSK"] = fix_cfe(analyze_modulated(x[on:on + n], fs, c, "OQPSK"))
    if on:
        res["mod_OQPSK"]["rf_onset_s"] = on / fs
    if len(x) > 200000:
        import bos_shr
        try:
            # chips from just before the RF onset: the preamble search looks at the start of the stream
            st = max(0, on - int(20e-6 * fs))
            en = rf_end(x) if len(x) > 4_000_000 else len(x)     # not the noise after the packet
            if en - st < 200000:
                en = len(x)
            log(f"ШР: chip receiver on {(en - st) / fs:.3f} s ({(en - st) / 1e6:.0f} M samples, "
                f"{bos_shr._workers()} threads) …")
            t0 = time.time()
            chips, info = bos_shr.chips_from_iq(x[st:en], fs, sr, cfg.get("alpha", 0.25), progress=progress)
            log(f"ШР: {len(chips) / 2e6:.1f} M symbols in {time.time() - t0:.1f} s")
            tries = [shr_rate] if shr_rate else list(SHR_RATES_KBPS)[::-1]
            for rk in tries:
                d = bos_shr.decode_shr(chips, rk, cfg.get("preamble", bos_shr.PREAMBLE), max_cbd=4)
                log(f"ШР {rk} kbit/s: preambles {d.get('preamble_found', 0)}, marker {d.get('marker')}")
                if d.get("preamble_found"):
                    res["shr"] = d
                    break
            else:
                res["shr"] = {"preamble_found": 0, "marker": False, "frames": [], "searched": tries}
        except Exception as e:
            res["shr"] = {"error": f"{type(e).__name__}: {e}"}
    return res
