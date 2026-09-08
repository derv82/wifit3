from __future__ import annotations

import re

from .vendors import VENDOR_BY_OUI
from wifit3.models.identity import clean_text

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
