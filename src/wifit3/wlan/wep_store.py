"""Passive WEP capture store (always-on, RX-only).

The single sink for everything we passively learn from WEP-encrypted Data
frames on the RX/parse path. One ``observe(bssid, parsed)`` call routes a frame to all the
right buckets:

  - unique 3-byte IVs (deduped) → the count that drives "ETA to 10k"
  - broadcast ARP-sized frames → replay seeds for the ARP-replay attack
  - those same frames' (IV, ciphertext) → PTW crack samples (known plaintext)

Keeping the routing here (not in WlanInterface) means callers never need to
know ARP sizes or cipher offsets. No TX, no association — it works equally
from the Scanner (channel hopping) and Focus (parked), the only difference
being how fast frames arrive.

The light, UI-facing counters live on ``AccessPoint.wep`` (a ``WepStats``);
the heavy buffers live here so the ``WepStats`` model stays cheap to poll.
"""
from __future__ import annotations

import time
from collections import deque
from typing import TYPE_CHECKING, Deque, Dict, List, Optional, Set

from wifit3.engine.models import WepStats

if TYPE_CHECKING:
    from wifit3.dot11.packet import WepDataPacket

# Unique-IV count at which a 40-bit (5-byte) WEP key typically becomes
# recoverable with PTW. Drives the Focus "ETA to Nk" line.
WEP_CRACK_IV_THRESHOLD = 10_000

# WEP-encrypted ARP REQUEST recognition (passive, no decrypt). ARP requests
# are broadcast; the on-air 802.11 length is the tell:
#   24 (MAC hdr) + 4 (IV+KeyID) + 8 (LLC/SNAP) + 28 (ARP) + 4 (ICV) = 68
#   +2 for a QoS header (26-byte MAC hdr)                            = 70
# Some stacks pad to 86/88. VERIFY against the hardware debug dump
# (wifit3-wep-arp.log) before trusting these — driver framing varies.
ARP_REQUEST_LENGTHS = {68, 70, 86, 88}

# Fragmentation seeds (iv, leading-ciphertext) from ANY WEP data frame — the
# LLC/SNAP known-plaintext makes the seed protocol-independent, so frag does
# NOT need a replayable ARP (that's the whole point of frag vs replay). Deduped
# by IV; a small ring is plenty (we only need one good seed).
SEED_SAMPLE_MAXLEN = 64

# We store EVERY ARP-sized broadcast WEP frame — BOTH directions. The replay
# engine re-addresses each into a ToDS frame sourced from our associated MAC
# (the 802.11 header is cleartext; only the IV+body is encrypted), so a FromDS
# relay is just as replayable as a client's ToDS request. Recognition isn't
# foolproof either, so breadth + the engine's yield-test pruning beats a clever
# filter. Ring is per-BSSID, newest-last; cap keeps memory bounded.
ARP_RING_MAXLEN = 256

# ChopChop can chop ANY broadcast WEP data frame, not just ARP-sized ones (it
# re-frames the cipher, so the original addressing is irrelevant, and the
# LLC/SNAP head is always-known plaintext) — so we keep a broader ring of
# broadcast data frames as chop seeds. This is what lets ChopChop fall back to
# IP broadcasts (DHCP / mDNS / …) when no ARP is in the air. Min length gates
# out runts too short to yield the ~40 keystream bytes a forged ARP needs:
#   24 (MAC hdr) + 4 (IV+KeyID) + >=40 cipher (8 SNAP + >=28 + 4 ICV) = 68.
CHOP_RING_MAXLEN = 64
CHOP_MIN_LEN = 68


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


class WepCaptureStore:
    """Per-interface registry of WEP capture state, keyed by BSSID.

    One instance lives on each ``WlanInterface``. ``observe()`` is the single
    RX entry point; the ``record_*`` methods are the low-level buckets it
    routes to (and are unit-tested directly). Everything else is read-only
    accessors for the UI / cracker (counts, rate, ETA, seeds, samples).
    """

    # Frames whose destination is broadcast are ARP-replay / crack candidates.
    _BROADCAST = "ff:ff:ff:ff:ff:ff"

    def __init__(self, rate_window_s: float = 10.0):
        self._rate_window_s = rate_window_s
        self._unique_ivs: Dict[str, Set[bytes]] = {}
        self._stats: Dict[str, WepStats] = {}
        self._rates: Dict[str, RateTracker] = {}
        # Per-BSSID ring of replayable (ToDS) ARP-like frames (raw bytes).
        self._arp_candidates: Dict[str, Deque[bytes]] = {}
        # Broadcast WEP data frames of any size — ChopChop chop seeds (superset
        # of the ARP-sized ring above; includes IP broadcasts).
        self._chop_candidates: Dict[str, Deque[bytes]] = {}
        # Per-BSSID count of ALL broadcast WEP frames seen (both directions,
        # any size) — for visibility ("N seen / M usable seeds").
        self._broadcast_seen: Dict[str, int] = {}
        # Per-BSSID PTW crack samples: (iv, cipher16) for ARP-sized broadcast
        # frames (known plaintext), deduped by IV. This is the cracker's input
        # — kept separate from the small replay-seed ring because the cracker
        # needs tens of thousands of distinct IVs.
        self._crack_samples: Dict[str, List[tuple]] = {}
        self._crack_ivs: Dict[str, Set[bytes]] = {}
        # Sample acquisition rate (samples/s), for the crack ETA.
        self._sample_rates: Dict[str, RateTracker] = {}
        # Per-BSSID ring of (iv, leading-cipher) from ANY WEP data frame, for
        # the fragmentation seed. Deduped by IV.
        self._seed_samples: Dict[str, Deque[tuple]] = {}
        self._seed_ivs: Dict[str, Set[bytes]] = {}

    def observe(self, bssid: str, pkt: "WepDataPacket") -> Optional[WepStats]:
        """Route one parsed WEP Data frame into every relevant bucket.

        The single RX entry point — the caller (WlanInterface) only has to
        know "this is a confirmed-WEP data frame on a known AP"; all the ARP
        size / cipher-offset knowledge lives here. Returns the BSSID's
        ``WepStats`` (so the caller can attach it to the AP on first sight),
        or None if the frame had no IV.
        """
        iv = pkt.iv
        if not iv:
            return None
        stats = self.record(bssid, iv)
        cipher = pkt.cipher
        # ANY data frame is a fragmentation seed (LLC/SNAP known-plaintext),
        # not just broadcast ARPs.
        if cipher and len(cipher) >= 8:
            self.record_seed_sample(bssid, iv, cipher)
        raw = pkt.raw
        if raw and pkt.dest == self._BROADCAST:
            # record_broadcast_frame returns True iff it was ARP-sized — reuse
            # that as the single size gate for the crack sample too (the
            # ciphertext's plaintext is only known for ARP-sized frames).
            arp_sized = self.record_broadcast_frame(bssid, raw, source=pkt.source)
            if arp_sized and cipher and len(cipher) == 16:
                self.record_crack_sample(bssid, iv, cipher)
        return stats

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

    # ---- Broadcast-frame candidates (ARP replay + ChopChop seeds) -----------

    def record_broadcast_frame(
        self, bssid: str, raw: bytes, source: Optional[str] = None
    ) -> bool:
        """File one broadcast WEP Data frame into the candidate rings.

        Counts every broadcast frame seen (for the UI's 'N seen / M usable'
        visibility), keeps any of usable size as a ChopChop seed, and — for
        frames matching the ARP-request length heuristic, regardless of DS
        direction — also keeps it in the ARP-replay ring (the replay engine
        re-addresses it into a replayable ToDS frame). Returns True if it was
        ARP-sized (i.e. landed in the ARP ring).
        """
        self._broadcast_seen[bssid] = self._broadcast_seen.get(bssid, 0) + 1
        # Any broadcast WEP data frame of usable size is a ChopChop seed (ARP or
        # IP alike) — keep a broad ring so ChopChop isn't limited to ARP frames.
        if len(raw) >= CHOP_MIN_LEN:
            chop_ring = self._chop_candidates.setdefault(
                bssid, deque(maxlen=CHOP_RING_MAXLEN)
            )
            chop_ring.append(bytes(raw))
        if len(raw) not in ARP_REQUEST_LENGTHS:
            return False
        arp_ring = self._arp_candidates.setdefault(bssid, deque(maxlen=ARP_RING_MAXLEN))
        arp_ring.append(bytes(raw))
        return True

    def arp_candidates(self, bssid: str) -> List[bytes]:
        """Snapshot of stored ARP-replay candidates (raw captured frames)."""
        return list(self._arp_candidates.get(bssid, ()))

    def chop_candidates(self, bssid: str) -> List[bytes]:
        """Snapshot of broadcast WEP data frames usable as ChopChop seeds (raw
        captured frames, any size — ARP and IP alike)."""
        return list(self._chop_candidates.get(bssid, ()))

    def arp_candidate_count(self, bssid: str) -> int:
        return len(self._arp_candidates.get(bssid, ()))

    def broadcast_seen_count(self, bssid: str) -> int:
        """All broadcast WEP frames seen (any size/direction) — for the UI's
        'N seen / M usable' visibility."""
        return self._broadcast_seen.get(bssid, 0)

    # ---- PTW crack samples --------------------------------------------------

    def record_crack_sample(
        self, bssid: str, iv: bytes, cipher: bytes, now: Optional[float] = None
    ) -> bool:
        """Store one (IV, cipher) PTW sample, deduped by IV. Caller passes
        these only for ARP-sized broadcast frames, where the plaintext (hence
        keystream) is known. Returns True if it was a new IV."""
        seen = self._crack_ivs.setdefault(bssid, set())
        if iv in seen:
            return False
        seen.add(iv)
        self._crack_samples.setdefault(bssid, []).append((bytes(iv), bytes(cipher)))
        # Track the SAMPLE acquisition rate separately from unique IVs — samples
        # (ARP-sized broadcast, known-plaintext) are a subset, and they're what
        # actually gates cracking, so the crack ETA must use this rate, not the
        # unique-IV rate (which races ahead via the client's organic traffic).
        self._sample_rates.setdefault(
            bssid, RateTracker(self._rate_window_s)
        ).mark(now)
        return True

    def crack_samples(self, bssid: str) -> List[tuple]:
        """Append-only list of (iv, cipher16) PTW samples for ``bssid``."""
        return self._crack_samples.get(bssid, [])

    def crack_sample_count(self, bssid: str) -> int:
        return len(self._crack_samples.get(bssid, ()))

    def crack_rate(self, bssid: str, now: Optional[float] = None) -> float:
        """Usable-IV (crack-sample) acquisition rate, samples/second."""
        rt = self._sample_rates.get(bssid)
        return rt.rate(now) if rt else 0.0

    def crack_eta_seconds(
        self, bssid: str, target: int, now: Optional[float] = None
    ) -> Optional[float]:
        """Seconds until ``target`` crack samples at the current sample rate.
        This is the REAL ETA-to-crackable (the cracker gates on samples, not
        unique IVs). 0.0 if already there; None when there's no usable rate."""
        remaining = target - self.crack_sample_count(bssid)
        if remaining <= 0:
            return 0.0
        r = self.crack_rate(bssid, now)
        if r <= 0:
            return None
        return remaining / r

    # ---- Fragmentation seed samples -----------------------------------------

    def record_seed_sample(self, bssid: str, iv: bytes, cipher: bytes) -> bool:
        """Store one (IV, leading-cipher) sample from ANY WEP data frame for the
        fragmentation seed, deduped by IV. Returns True if it was a new IV."""
        seen = self._seed_ivs.setdefault(bssid, set())
        if iv in seen:
            return False
        seen.add(iv)
        ring = self._seed_samples.setdefault(
            bssid, deque(maxlen=SEED_SAMPLE_MAXLEN)
        )
        # Evicting the oldest sample also frees its IV from the dedup set so the
        # ring can't pin memory via _seed_ivs growing unbounded.
        if len(ring) == ring.maxlen:
            old_iv, _ = ring[0]
            seen.discard(old_iv)
        ring.append((bytes(iv), bytes(cipher)))
        return True

    def seed_samples(self, bssid: str) -> List[tuple]:
        """Snapshot of (iv, cipher) fragmentation-seed samples for ``bssid``."""
        return list(self._seed_samples.get(bssid, ()))
