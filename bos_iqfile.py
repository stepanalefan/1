# -*- coding: utf-8 -*-
"""
bos_iqfile.py
=============
Read / write I/Q recordings so that BOS signals can be analysed offline.

Read:
* .iq.tar  R&S I/Q TAR (FSW "I/Q Export"): XML with Clock (sample rate), Format, DataType,
           ScalingFactor, NumberOfChannels, CenterFrequency (UserData) + the data file
* .iqw     FSW raw float32: I,Q pairs (default) or the whole I block followed by the Q block
* .csv     two numeric columns I,Q (header lines with "Sample Rate" / "Center" are used if present)
* .npy / .npz   complex array (npz: keys x, fs, center)
* .bin / .dat / .raw   raw interleaved I,Q, float32 / int16 (sample rate given by the user)
Write: .iq.tar (float32), so station captures can be archived and re-analysed.
"""

from __future__ import annotations

import io
import os
import re
import tarfile
import time
import xml.etree.ElementTree as ET

import numpy as np

DTYPES = {"int8": np.int8, "int16": np.int16, "int32": np.int32, "float32": np.float32, "float64": np.float64}


class IQData:
    def __init__(self, x, fs=None, center=None, name="", meta=None):
        self.x = x if isinstance(x, np.ndarray) and x.dtype == np.complex64 else np.asarray(x, np.complex64)
        self.fs, self.center, self.name = fs, center, name
        self.meta = meta or {}

    def __repr__(self):
        return f"IQData({len(self.x)} samples, fs={self.fs}, center={self.center}, {self.name})"


def _num(s):
    try:
        return float(str(s).strip().split()[0].replace(",", "."))
    except Exception:
        return None


def _to_complex64(raw, dtype, nch=1, scale=1.0, order="pairs", progress=None):
    """Interleaved (or block) I/Q numbers -> complex64. float32 single-channel pairs without scaling is
    returned as a zero-copy view (of a memory map: nothing is read until used); everything else is
    converted in chunks, so a multi-GB file never needs more than its complex64 size in RAM."""
    raw = raw[:raw.size // (2 * nch) * 2 * nch]
    if order == "pairs" and nch == 1 and raw.dtype == np.float32 and scale == 1.0:
        return raw.view(np.complex64)
    n = raw.size // (2 * nch)
    x = np.empty(n, np.complex64)
    k = 1.0 / float(np.iinfo(raw.dtype).max) if np.issubdtype(raw.dtype, np.integer) else 1.0
    k *= scale
    step = 1 << 22
    for s in range(0, n, step):
        e = min(n, s + step)
        if order == "blocks":
            x.real[s:e] = raw[s:e] * k
            x.imag[s:e] = raw[n + s:n + e] * k
        else:
            r = raw[s * 2 * nch:e * 2 * nch].reshape(-1, 2 * nch)
            x.real[s:e] = r[:, 0] * k
            x.imag[s:e] = r[:, 1] * k
        if progress:
            progress(e / n)
    return x


def read_iq_tar(path, progress=None) -> IQData:
    with tarfile.open(path) as tf:
        xml_m = next((m for m in tf.getmembers() if m.name.lower().endswith(".xml")), None)
        if xml_m is None:
            raise ValueError("no XML description in the .iq.tar")
        root = ET.fromstring(tf.extractfile(xml_m).read())
        txt = {el.tag.split("}")[-1]: (el.text or "").strip() for el in root.iter()}
        fs = _num(txt.get("Clock"))
        dtype = txt.get("DataType", "float32").lower()
        fmt = txt.get("Format", "complex").lower()
        nch = int(_num(txt.get("NumberOfChannels")) or 1)
        scale = _num(txt.get("ScalingFactor")) or 1.0
        center = None
        for el in root.iter():
            if el.tag.split("}")[-1].lower() in ("centerfrequency", "centerfreq") and _num(el.text) is not None:
                center = _num(el.text)
                break
        dname = txt.get("DataFilename")
        dm = next((m for m in tf.getmembers() if dname and m.name.endswith(dname)), None) or \
            next(m for m in tf.getmembers() if not m.name.lower().endswith(".xml") and m.isfile())
        if fmt != "complex":
            raise ValueError(f"I/Q format '{fmt}' not supported (complex only)")
        dt = np.dtype(DTYPES.get(dtype, np.float32))
        if isinstance(tf.fileobj, (io.BufferedReader, io.FileIO)):
            # uncompressed tar (as the FSW writes it): map the data member in place, no copy
            raw = np.memmap(path, dt, "r", offset=dm.offset_data, shape=(dm.size // dt.itemsize,))
        else:
            raw = np.frombuffer(tf.extractfile(dm).read(), dt)
    x = _to_complex64(raw, dt, nch, scale, progress=progress)
    return IQData(x, fs, center, os.path.basename(path), {"format": "R&S iq.tar", "datatype": dtype})


def write_iq_tar(path, x, fs, center=None, comment="KTR TEST BOS station"):
    x = np.asarray(x, np.complex64)
    base = os.path.basename(path).replace(".iq.tar", "")
    dname = f"{base}.complex.1ch.float32"
    xml = (f'<?xml version="1.0" encoding="UTF-8"?>\n<RS_IQ_TAR_FileFormat fileFormatVersion="1">\n'
           f"<Name>KTR TEST</Name><Comment>{comment}</Comment>"
           f"<DateTime>{time.strftime('%Y-%m-%dT%H:%M:%S')}</DateTime>"
           f"<Samples>{len(x)}</Samples><Clock unit=\"Hz\">{fs:.10g}</Clock><Format>complex</Format>"
           f"<DataType>float32</DataType><ScalingFactor unit=\"V\">1</ScalingFactor>"
           f"<NumberOfChannels>1</NumberOfChannels><DataFilename>{dname}</DataFilename>"
           + (f"<UserData><CenterFrequency unit=\"Hz\">{center:.12g}</CenterFrequency></UserData>" if center else "")
           + "</RS_IQ_TAR_FileFormat>\n").encode()
    data = np.column_stack([x.real, x.imag]).astype("<f4").tobytes()
    with tarfile.open(path, "w") as tf:
        for name, blob in ((f"{base}.xml", xml), (dname, data)):
            ti = tarfile.TarInfo(name)
            ti.size = len(blob)
            ti.mtime = time.time()
            tf.addfile(ti, io.BytesIO(blob))


def read_csv(path) -> IQData:
    fs = center = None
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = [p for p in re.split(r"[;,\t ]+", line.strip()) if p]
            if not parts:
                continue
            low = line.lower()
            if ("sample rate" in low or "clock" in low or "srate" in low) and fs is None:
                v = [_num(p) for p in parts if _num(p) is not None]
                fs = v[0] if v else None
                continue
            if "center" in low and center is None:
                v = [_num(p) for p in parts if _num(p) is not None]
                center = v[0] if v else None
                continue
            nums = [_num(p) for p in parts]
            nums = [n for n in nums if n is not None]
            if len(nums) >= 2 and len(nums) == len(parts):
                rows.append(nums[-2:])
    if not rows:
        raise ValueError("no I,Q number columns found in the CSV")
    a = np.array(rows, float)
    return IQData(a[:, 0] + 1j * a[:, 1], fs, center, os.path.basename(path), {"format": "CSV"})


def read_raw(path, dtype="float32", order="pairs", progress=None) -> IQData:
    dt = np.dtype(DTYPES[dtype])
    raw = np.memmap(path, dt, "r") if os.path.getsize(path) >= dt.itemsize * 2 else np.zeros(0, dt)
    x = _to_complex64(raw, dt, order=order, progress=progress)
    return IQData(x, None, None, os.path.basename(path), {"format": f"raw {dtype} {order}"})


def load(path, dtype="float32", order="pairs", progress=None) -> IQData:
    """Large files are memory-mapped (float32) or converted in chunks to complex64."""
    p = path.lower()
    if p.endswith(".iq.tar") or p.endswith(".tar"):
        return read_iq_tar(path, progress)
    if p.endswith(".csv") or p.endswith(".txt"):
        return read_csv(path)
    if p.endswith(".npy"):
        return IQData(np.load(path), None, None, os.path.basename(path), {"format": "npy"})
    if p.endswith(".npz"):
        d = np.load(path)
        return IQData(d["x"], float(d["fs"]) if "fs" in d else None, float(d["center"]) if "center" in d else None,
                      os.path.basename(path), {"format": "npz"})
    if p.endswith(".iqw"):
        return read_raw(path, "float32", order, progress)
    return read_raw(path, dtype, order, progress)


FILE_FILTER = ("I/Q recordings (*.iq.tar *.tar *.iqw *.csv *.txt *.npy *.npz *.bin *.dat *.raw);;All files (*)")
