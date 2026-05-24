"""Passive WEP IV collection (always-on, RX-only).

This is the passive half of the WEP attack suite: it hooks the existing
RX/parse path (via ``WlanInterface._on_frame_parsed``) and tallies the
unique 3-byte Initialization Vectors leaking from WEP-encrypted Data
frames. No TX, no association — it works equally from the Scanner (channel
hopping) and Focus (parked on one channel), the only difference being how
fast IVs arrive.

The light, UI-facing counters live on ``AccessPoint.wep`` (a ``WepStats``).
The heavy state — the per-BSSID set used to dedup IVs, and (later
milestones) the captured ARP / validation packets — lives here so the
pydantic model stays cheap to poll and serialize.

Cracking needs ~10k unique IVs for a 40-bit key, more for 104-bit; PTW
(native, future M7) consumes the same per-IV stream. For now the threshold
just drives the Focus "ETA to 10k" readout.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Deque, Dict, List, Optional, Set

from wifit3.engine.models import WepStats

# Unique-IV count at which a 40-bit (5-byte) WEP key typically becomes
# recoverable with PTW. Drives the Focus "ETA to Nk" line; the actual
# cracker (future M7) sets its own readiness threshold.
WEP_CRACK_IV_THRESHOLD = 10_000

# WEP-encrypted ARP REQUEST recognition (passive, no decrypt). ARP requests
# are broadcast; the on-air 802.11 length is the tell:
#   24 (MAC hdr) + 4 (IV+KeyID) + 8 (LLC/SNAP) + 28 (ARP) + 4 (ICV) = 68
#   +2 for a QoS header (26-byte MAC hdr)                            = 70
# Some stacks pad to 86/88. VERIFY against the hardware debug dump
# (wifit3-wep-arp.log) before trusting these — driver framing varies.
ARP_REQUEST_LENGTHS = {68, 70, 86, 88}

# We store EVERY ARP-sized broadcast WEP frame — BOTH directions. The replay
# engine re-addresses each into a ToDS frame sourced from our associated MAC
# (the 802.11 header is cleartext; only the IV+body is encrypted), so a FromDS
# relay is just as replayable as a client's ToDS request. Recognition isn't
# foolproof either, so breadth + the engine's yield-test pruning beats a clever
# filter. Ring is per-BSSID, newest-last; cap keeps memory bounded.
ARP_RING_MAXLEN = 256


class RateTracker:
    """Sliding-window event-rate estimator (events/second).

    Records event timestamps and reports the rate over a trailing window.
    ``rate()`` prunes to *now* on every call, so the rate decays correctly
    toward zero when events stop arriving — important for the ETA readout,
    which must not freeze on the last-seen rate when a target goes quiet.
    """

    def __init__(self, window_s: float = 10.0):
        self._window_s = window_s
        self._events: Deque[float] = deque()

    def mark(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self._events.append(now)
        self._prune(now)

    def rate(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        self._prune(now)
        if not self._events:
            return 0.0
        span = now - self._events[0]
        if span <= 0:
            # All events landed this same instant — not enough spread to
            # divide by; report the raw count as a 1-second rate floor.
            return float(len(self._events))
        return len(self._events) / span

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_s
        while self._events and self._events[0] < cutoff:
            self._events.popleft()


class WepIvCollector:
    """Per-interface registry of WEP IV state, keyed by BSSID.

    One instance lives on each ``WlanInterface``. ``record()`` is called
    from the RX path for every WEP-encrypted Data frame; everything else is
    read-only accessors for the UI (counts, rate, ETA).
    """

    def __init__(self, rate_window_s: float = 10.0):
        self._rate_window_s = rate_window_s
        self._unique_ivs: Dict[str, Set[bytes]] = {}
        self._stats: Dict[str, WepStats] = {}
        self._rates: Dict[str, RateTracker] = {}
        # Per-BSSID ring of replayable (ToDS) ARP-like frames (raw bytes).
        self._arp_candidates: Dict[str, Deque[bytes]] = {}
        # Per-BSSID count of ALL broadcast WEP frames seen (both directions,
        # any size) — for visibility ("N seen / M usable seeds").
        self._arp_seen: Dict[str, int] = {}

    def record(self, bssid: str, iv: bytes, now: Optional[float] = None) -> WepStats:
        """Tally one WEP Data frame's IV for ``bssid``.

        Always bumps ``total_frames``; bumps ``unique_ivs`` and marks the
        rate tracker only when ``iv`` is one we haven't seen for this BSSID
        (unique IVs are what cracking actually needs — replayed frames reuse
        a fixed IV and must not inflate the count or the rate). Returns the
        BSSID's ``WepStats`` so the caller can attach it to the AP on first
        sight.
        """
        stats = self._stats.setdefault(bssid, WepStats())
        seen = self._unique_ivs.setdefault(bssid, set())
        stats.total_frames += 1
        if iv not in seen:
            seen.add(iv)
            stats.unique_ivs += 1
            self._rates.setdefault(
                bssid, RateTracker(self._rate_window_s)
            ).mark(now)
        return stats

    def stats(self, bssid: str) -> Optional[WepStats]:
        return self._stats.get(bssid)

    def unique_count(self, bssid: str) -> int:
        stats = self._stats.get(bssid)
        return stats.unique_ivs if stats else 0

    def rate(self, bssid: str, now: Optional[float] = None) -> float:
        """Unique-IV acquisition rate (IVs/second) over the trailing window."""
        rt = self._rates.get(bssid)
        return rt.rate(now) if rt else 0.0

    def eta_seconds(
        self,
        bssid: str,
        target: int = WEP_CRACK_IV_THRESHOLD,
        now: Optional[float] = None,
    ) -> Optional[float]:
        """Seconds until ``target`` unique IVs at the current rate.

        Returns 0.0 if already at/past the target, and None when there's no
        usable rate yet (can't estimate — caller renders a placeholder).
        """
        remaining = target - self.unique_count(bssid)
        if remaining <= 0:
            return 0.0
        r = self.rate(bssid, now)
        if r <= 0:
            return None
        return remaining / r

    # ---- ARP replay candidates ----------------------------------------------

    def record_arp_candidate(
        self, bssid: str, raw: bytes, source: Optional[str] = None
    ) -> bool:
        """Stash a broadcast WEP Data frame as an ARP-replay candidate.

        Counts every broadcast WEP frame seen (for the UI's 'N seen / M usable'
        visibility) and retains any that match the ARP-request length
        heuristic, regardless of DS direction — the replay engine re-addresses
        it into a replayable ToDS frame. Returns True if retained.
        """
        self._arp_seen[bssid] = self._arp_seen.get(bssid, 0) + 1
        if len(raw) not in ARP_REQUEST_LENGTHS:
            return False
        ring = self._arp_candidates.setdefault(bssid, deque(maxlen=ARP_RING_MAXLEN))
        ring.append(bytes(raw))
        return True

    def arp_candidates(self, bssid: str) -> List[bytes]:
        """Snapshot of stored ARP-replay candidates (raw captured frames)."""
        return list(self._arp_candidates.get(bssid, ()))

    def arp_candidate_count(self, bssid: str) -> int:
        return len(self._arp_candidates.get(bssid, ()))

    def arp_seen_count(self, bssid: str) -> int:
        """All broadcast WEP frames seen (any size/direction) — for the UI's
        'N seen / M usable' visibility."""
        return self._arp_seen.get(bssid, 0)
