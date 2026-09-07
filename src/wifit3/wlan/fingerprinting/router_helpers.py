from __future__ import annotations

import re
from typing import Iterable

from wifit3.wlan.fingerprinting.vendors import VENDOR_BY_OUI

_PREFIX_LENGTHS = (9, 7, 6)
_CANONICAL_VENDOR_PATTERNS = (
    (re.compile(r"\btp[-\s]?link\b", re.I), "TP-Link"),
    (re.compile(r"\bavm\b|audiovisuelles marketing", re.I), "AVM"),
    (re.compile(r"\bamv\b|amv audio", re.I), "AMV"),
    (re.compile(r"\bkaon\b", re.I), "Kaon"),
    (re.compile(r"\b(?:mikrotik|routerboard(?:\.com)?)\b", re.I), "MikroTik"),
    (re.compile(r"\bepson\b", re.I), "Epson"),
    (re.compile(r"\bapple\b", re.I), "Apple"),
)


def hex_mac(mac: str) -> str:
    return mac.replace(":", "").replace("-", "").upper()


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().strip("\x00")
    return cleaned or None


def canonical_vendor(name: str | None) -> str | None:
    cleaned = clean_text(name)
    if cleaned is None:
        return None
    for pattern, canonical in _CANONICAL_VENDOR_PATTERNS:
        if pattern.search(cleaned):
            return canonical
    return cleaned


def vendor_for_mac(mac: str) -> str | None:
    oui = hex_mac(mac)
    vendor = next((VENDOR_BY_OUI[oui[:n]] for n in _PREFIX_LENGTHS if oui[:n] in VENDOR_BY_OUI), None)
    return canonical_vendor(vendor)


def combine_confidences(confidences: Iterable[float], cap: float = 0.99) -> float:
    miss = 1.0
    for confidence in confidences:
        miss *= 1.0 - max(0.0, min(confidence, 1.0))
    return min(cap, 1.0 - miss)
