# -*- coding: utf-8 -*-
"""
device_addresses.py
===================
Remembers the connection settings of the instruments (FSW, HMP4040, N6701C, IT6832, TFG6806) in
device_addresses.json next to the program - like can_lines.json for the CAN lines. Saved after every successful
connection (demo connections are not saved), restored into the connection fields at start-up. Each device also
keeps its last addresses ("recent") for the editable address lists.
"""

from __future__ import annotations

import json
import os
import sys

RECENT_MAX = 6


def _app_dir() -> str:
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else \
        os.path.dirname(os.path.abspath(__file__))


STORE_FILE = os.path.join(_app_dir(), "device_addresses.json")


def _read() -> dict:
    try:
        with open(STORE_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def load(device: str) -> dict:
    """Last connection settings of `device` ({} when never connected)."""
    return dict(_read().get(device, {}))


def recent(device: str) -> list[str]:
    return list(_read().get(device, {}).get("recent", []))


def save(device: str, settings: dict, addr_key: str = "addr"):
    """Store the settings of a successful connection; the address goes to the front of the recent list."""
    d = _read()
    old = d.get(device, {})
    new = dict(settings)
    addr = str(new.get(addr_key) or "").strip()
    rec = [a for a in old.get("recent", []) if a != addr]
    if addr:
        rec.insert(0, addr)
    new["recent"] = rec[:RECENT_MAX]
    d[device] = new
    try:
        tmp = STORE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, STORE_FILE)
    except OSError:
        pass


def option(name: str, default=None):
    """Program option kept in the same file (key "_options")."""
    return _read().get("_options", {}).get(name, default)


def set_option(name: str, value):
    d = _read()
    d.setdefault("_options", {})[name] = value
    try:
        tmp = STORE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, STORE_FILE)
    except OSError:
        pass


def fill_combo(combo, device: str, current: str = ""):
    """Put the recent addresses into an editable QComboBox (current text kept / set)."""
    have = {combo.itemText(i) for i in range(combo.count())}
    for a in recent(device):
        if a not in have:
            combo.addItem(a)
    if current:
        combo.setEditText(current)
