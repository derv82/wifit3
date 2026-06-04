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


# The 2.4 GHz channels that carry the bulk of APs: the non-overlapping trio
# nearly every router parks on (FCC 1/6/11). The channels between them overlap
# and stay mostly empty, so visiting 1/6/11 first front-loads most 2.4 GHz
# targets into the first three hops.
_PRIORITY_2G = (1, 6, 11)


def scan_hop_order(channels: list[int]) -> list[int]:
    """Reorder a channel set into scan-priority order: the busy 2.4 GHz trio
    (1/6/11) first, then the rest of 2.4 GHz, then 5 GHz.

    Front-loading the popular channels means the AP table is mostly populated
    within the first second, before the first sort tick — instead of new APs
    dribbling in channel-by-channel so that every later sort reshuffles the list
    as a fresh burst lands. Pure reordering: the result holds exactly the input
    channels (de-duped, first occurrence wins); the non-priority 2.4 GHz and the
    5 GHz channels keep the caller's original relative order.
    """
    seen: set[int] = set()
    uniq = [c for c in channels if not (c in seen or seen.add(c))]
    priority = [c for c in _PRIORITY_2G if c in uniq]
    rest_2g = [c for c in uniq if c <= 14 and c not in _PRIORITY_2G]
    band_5g = [c for c in uniq if c > 14]
    return priority + rest_2g + band_5g
