"""Seeds hashline capture dates in an SSID's .meta, as record_hashlines_captured stores them."""
from __future__ import annotations

from typing import Optional

from wifit3.crack.hc22000_format import hashline_id
from wifit3.persist.meta import update_meta


def write_hashline_dates(ssid: Optional[str], bssid: str, epochs: dict[str, int]) -> None:
    """``epochs``: raw hashline -> its capture date. Replaces the SSID's stored dates."""
    update_meta(ssid, bssid, hashlines={hashline_id(raw): {"captured": [epoch]} for raw, epoch in epochs.items()})
