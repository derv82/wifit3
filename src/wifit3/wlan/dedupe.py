"""Merge N per-card 802.11 RX streams into one deduplicated stream.

Two (or more) monitor-mode radios tuned to the same air hear mostly the same frames. ``submit``
returns True the first time a given on-air transmission is seen and False for every cross-card copy
that lands within ``window`` seconds, so each transmission surfaces exactly once no matter how many
cards caught it. What one card's antenna misses another often catches, so the merged stream is
strictly richer than any single card.

The key is FCS- and driver-independent: frame-control + addr1/2/3 + seq_ctrl (the MPDU header,
bytes ``[0:2] + [4:24]``). The same transmission heard by two cards carries identical addresses and
sequence number, so it collapses to one. A retransmission flips the Retry bit inside frame-control
and a fresh frame steps seq_ctrl, so neither is wrongly merged. Keys live only for ``window``
seconds, so a much-later frame that reuses a sequence number is never suppressed.

Promoted from ``scripts/cross_streams.py`` and generalized from two fixed int-indexed sources to a
dynamic set of string-keyed sources (a card id per source), so cards can be added and dropped at
runtime (hotplug / unplug). ``scripts/cross_streams.py`` keeps its own two-source copy.
"""


class StreamMerger:
    """Joins per-card parsed-frame sources into one deduplicated stream.

    ``submit(src, raw, now)`` returns True when a frame is novel (emit it) and False when it is a
    copy already delivered by another source within ``window``. Along the way it tallies the
    coverage payoff: how many unique transmissions were heard by more than one card versus only
    one, which is the coverage a single card would have lost.
    """

    def __init__(self, sources: list[str] | None = None, window: float = 0.3):
        self.window = window
        self._seen: dict[bytes, list] = {}          # key -> [first_ts, {src}]
        self._last_evict = 0.0
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
        """Drop a source (unplug): forget its counters and discard it from every in-window key so a
        later size check does not miscount a departed card as still present."""
        self.rx.pop(src, None)
        self.first.pop(src, None)
        self.only.pop(src, None)
        for ent in self._seen.values():
            ent[1].discard(src)

    @staticmethod
    def key(raw: bytes) -> bytes:
        """FC + addr1/2/3 + seq_ctrl. A control frame short of a full header (should not reach here,
        the parser drops them) falls back to its whole buffer so it still dedups sanely."""
        raw = bytes(raw)
        if len(raw) >= 24:
            return raw[0:2] + raw[4:24]
        return raw

    def submit(self, src: str, raw: bytes, now: float) -> bool:
        self.rx[src] = self.rx.get(src, 0) + 1
        k = self.key(raw)
        ent = self._seen.get(k)
        if ent is not None and now - ent[0] <= self.window:
            self.dup += 1
            if src not in ent[1]:
                ent[1].add(src)
                if len(ent[1]) == 2:
                    self.both += 1
            return False
        self._seen[k] = [now, {src}]
        self.novel += 1
        self.first[src] = self.first.get(src, 0) + 1
        return True

    def evict(self, now: float) -> None:
        """Retire keys older than the window, tallying the ones only a single source ever heard.
        Rate-limited to once per window so a busy channel does not rescan the dict every frame."""
        if now - self._last_evict < self.window:
            return
        self._last_evict = now
        dead = [k for k, ent in self._seen.items() if now - ent[0] > self.window]
        for k in dead:
            _, srcs = self._seen.pop(k)
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
