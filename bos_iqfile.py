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
        self.x = np.asarray(x, np.complex64)
        self.fs, self.center, self.name = fs, center, name
        self.meta = meta or {}

    def __repr__(self):
        return f"IQData({len(self.x)} samples, fs={self.fs}, center={self.center}, {self.name})"


def _num(s):
    try:
        return float(str(s).strip().split()[0].replace(",", "."))
    except Exception:
        return None


def read_iq_tar(path) -> IQData:
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
        raw = np.frombuffer(tf.extractfile(dm).read(), DTYPES.get(dtype, np.float32))
    if fmt != "complex":
        raise ValueError(f"I/Q format '{fmt}' not supported (complex only)")
    raw = raw.reshape(-1, 2 * nch)[:, :2].astype(np.float64)       # first channel
    if dtype.startswith("int"):
        raw = raw / float(np.iinfo(DTYPES[dtype]).max)
    x = (raw[:, 0] + 1j * raw[:, 1]) * scale
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


def read_raw(path, dtype="float32", order="pairs") -> IQData:
    raw = np.fromfile(path, DTYPES[dtype]).astype(np.float64)
    if dtype.startswith("int"):
        raw /= float(np.iinfo(DTYPES[dtype]).max)
    n = raw.size // 2
    if order == "blocks":                                  # I block then Q block
        x = raw[:n] + 1j * raw[n:2 * n]
    else:
        x = raw[0:2 * n:2] + 1j * raw[1:2 * n:2]
    return IQData(x, None, None, os.path.basename(path), {"format": f"raw {dtype} {order}"})


def load(path, dtype="float32", order="pairs") -> IQData:
    p = path.lower()
    if p.endswith(".iq.tar") or p.endswith(".tar"):
        return read_iq_tar(path)
    if p.endswith(".csv") or p.endswith(".txt"):
        return read_csv(path)
    if p.endswith(".npy"):
        return IQData(np.load(path), None, None, os.path.basename(path), {"format": "npy"})
    if p.endswith(".npz"):
        d = np.load(path)
        return IQData(d["x"], float(d["fs"]) if "fs" in d else None, float(d["center"]) if "center" in d else None,
                      os.path.basename(path), {"format": "npz"})
    if p.endswith(".iqw"):
        return read_raw(path, "float32", order)
    return read_raw(path, dtype, order)


FILE_FILTER = ("I/Q recordings (*.iq.tar *.tar *.iqw *.csv *.txt *.npy *.npz *.bin *.dat *.raw);;All files (*)")
