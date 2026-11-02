"""Merge N per-card 802.11 RX streams into one deduplicated stream.

The dedupe key is FCS- and driver-independent:
  - frame-control + addr1/2/3 + seq_ctrl (the MPDU header, bytes ``[0:2] + [4:24]``),
  - plus the QoS Control byte for QoS data (whose seq_ctrl counts per TID).

The same transmission heard by two cards carries identical addresses and sequence number, and
collapses to one. A retransmission flips the Retry bit inside frame-control
and a fresh frame steps seq_ctrl, so neither is wrongly merged. Keys live only for ``window``
seconds, so a much-later frame that reuses a sequence number is never suppressed.
"""

from collections import deque


class StreamMerger:
    """Joins per-card parsed-frame sources into one deduplicated stream.

    ``submit(src, raw, now)`` returns True when a frame is novel (emit it) and False when it is a
    copy already delivered by another source within ``window``.
    """

    def __init__(self, sources: list[str] | None = None, window: float = 0.3):
        self.window = window
        self._seen: dict[bytes, list] = {}          # key -> [first_ts, {src}]
        self._expiry = deque()                      # insertion-ordered (first_ts, key)
        self.novel = 0                              # unique on-air transmissions emitted
        self.dup = 0                                # cross-card copies suppressed
        self.both = 0                               # unique frames heard by >= 2 sources
        self.rx: dict[str, int] = {}                # frames each source delivered
        self.first: dict[str, int] = {}             # frames each source was first to deliver
        self.only: dict[str, int] = {}              # unique frames only this source heard (at evict)
        for src in sources or []:
            self.add_source(src)

    def add_source(self, src: str) -> None:
        """Register a source so its tallies read zero from the start (idempotent)."""
        self.rx.setdefault(src, 0)
        self.first.setdefault(src, 0)
        self.only.setdefault(src, 0)

    def remove_source(self, src: str) -> None:
        """Drop a source (unplug): forget its counters and remove it from every in-window key."""
        self.rx.pop(src, None)
        self.first.pop(src, None)
        self.only.pop(src, None)
        for ent in self._seen.values():
            ent[1].discard(src)

    @staticmethod
    def key(frame: bytes) -> bytes:
        """Identifies one on-air transmission of a whole 802.11 frame: FC, addr1-3, seq_ctrl, QoS Control."""
        if not isinstance(frame, bytes):
            frame = bytes(frame)
        if len(frame) < 24:
            return frame  # Control frames (ACK/CTS/RTS): whole frame is key
        # first 2b: frame ctrl, next 20b: addr1-3 + seq_ctrl (no Duration)
        key = frame[0:2] + frame[4:24]
        if frame[0] & 0x8C == 0x88:
            # Data, QoS subtype
            if frame[1] & 0x03 == 0x03:
                qos_offset = 30  # skip addr4
            else:
                qos_offset = 24
            key += frame[qos_offset:qos_offset + 1]
        return key

    def submit_detailed(self, src: str, raw: bytes, now: float) -> tuple[bool, bool]:
        """Submit a frame. Returns (novel_globally, is_first_for_src)."""
        self._evict(now)
        self.rx[src] = self.rx.get(src, 0) + 1
        k = self.key(raw)
        ent = self._seen.get(k)
        if ent is not None and now - ent[0] <= self.window:
            self.dup += 1
            is_first_for_src = src not in ent[1]
            if is_first_for_src:
                ent[1].add(src)
                if len(ent[1]) == 2:
                    self.both += 1
            return False, is_first_for_src
        self._seen[k] = [now, {src}]
        self._expiry.append((now, k))
        self.novel += 1
        self.first[src] = self.first.get(src, 0) + 1
        return True, True

    def submit(self, src: str, raw: bytes, now: float) -> bool:
        return self.submit_detailed(src, raw, now)[0]

    def _evict(self, now: float) -> None:
        """Retire expired keys in insertion order, tallying single-source frames."""
        while self._expiry and now - self._expiry[0][0] > self.window:
            timestamp, key = self._expiry.popleft()
            entry = self._seen.get(key)
            if entry is None or entry[0] != timestamp:
                continue
            _, srcs = self._seen.pop(key)
            if len(srcs) == 1:
                only = next(iter(srcs))
                self.only[only] = self.only.get(only, 0) + 1

    def flush(self) -> None:
        """Evict everything so ``only`` is final for a summary."""
        for _, srcs in self._seen.values():
            if len(srcs) == 1:
                only = next(iter(srcs))
                self.only[only] = self.only.get(only, 0) + 1
        self._seen.clear()
        self._expiry.clear()
