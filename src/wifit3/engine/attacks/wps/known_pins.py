"""BSSID → candidate WPS default PINs (the campaign's pre-sweep seed).

Many consumer routers ship a factory WPS PIN that is fixed per hardware family or
computed from the BSSID, so a target's BSSID predicts a small set of PINs worth trying
*before* the generic COMMON list and the 11k brute sweep.

The candidates are **computed at runtime** from published default-PIN *algorithms*
(see :mod:`wps_algos`); no PIN table is bundled.
"""

from __future__ import annotations

from typing import List

from . import wps_algos

_HEX = "0123456789abcdefABCDEF"


def known_pins_for(bssid: str) -> List[str]:
    """Ranked, deduped candidate PINs for a BSSID (any separators/case), or [] when the
    BSSID isn't a full 6-octet MAC."""
    hexstr = "".join(c for c in bssid if c in _HEX)[:12]
    if len(hexstr) < 12:
        return []
    return wps_algos.pins_for(bytes.fromhex(hexstr))
