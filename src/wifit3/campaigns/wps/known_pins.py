"""Backwards-compatibility wrapper for wps_algos.pins_for."""
from __future__ import annotations

from typing import List

from . import wps_algos


def known_pins_for(bssid: str) -> List[str]:
    """Ranked candidate PINs for a BSSID."""
    return wps_algos.pins_for(bssid)
