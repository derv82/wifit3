"""802.11 channel helpers (band / DFS classification).

Device-agnostic facts shared by the scanner default-hop logic and the Channel Filter
dialog, so "what counts as DFS" lives in exactly one place.
"""
from __future__ import annotations


def is_dfs(channel: int) -> bool:
    """True for 5 GHz DFS channels — UNII-2 (52-64) + UNII-2e (100-144).

    These bands are shared with radar, so a device that *transmits* there must do radar
    detection; most APs avoid them, leaving them usually empty. Passive monitoring (RX) on
    them is fine, so they stay tunable — they are just excluded from the default scan hop
    (opt back in via the Channel Filter) so a fixed scan budget isn't diluted on empty air.
    """
    return 52 <= channel <= 144
