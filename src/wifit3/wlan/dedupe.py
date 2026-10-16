"""Merge N per-card 802.11 RX streams into one deduplicated stream.

Two (or more) monitor-mode radios tuned to the same air hear mostly the same frames. ``submit``
returns True for the first copy of an on-air transmission and again only when a later cross-card
copy is more useful. What one card's antenna misses or clips another can recover, so the merged
stream is at least as useful as any single card.

The key is FCS- and driver-independent: frame-control + addr1/2/3 + seq_ctrl + QoS TID for the
three-address frames accepted by the parser. Another read from one source starts the next
transmission even when a Retry reuses the header. Keys live only for ``window`` seconds.

Promoted from ``scripts/cross_streams.py`` and generalized from two fixed int-indexed sources to a
dynamic set of string-keyed sources (a card id per source), so cards can be added and dropped at
runtime (hotplug / unplug). ``scripts/cross_streams.py`` keeps its own two-source copy.
"""

from dataclasses import dataclass


@dataclass
class _Transmission:
    first_seen: float
    sources: set[str]
    best_quality: tuple[int, ...]
    shared: bool = False


class StreamMerger:
    """Joins per-card parsed frames, emitting first copies and useful quality upgrades."""

    def __init__(self, sources: list[str] | None = None, window: float = 0.3):
        self.window = window
        self._seen: dict[bytes, _Transmission] = {}
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
        for transmission in self._seen.values():
            transmission.sources.discard(src)

    @staticmethod
    def key(raw: bytes) -> bytes:
        """FC + addr1/2/3 + seq_ctrl + QoS TID when present."""
        raw = bytes(raw)
        if len(raw) >= 24:
            identity = raw[0:2] + raw[4:24]
            is_qos_data = raw[0] & 0x8C == 0x88
            if is_qos_data and len(raw) >= 25:
                identity += bytes((raw[24] & 0x0F,))
            return identity
        return raw

    def submit_detailed(self, src: str, raw: bytes, now: float,
                        quality: tuple[int, ...] = (0,)) -> tuple[bool, bool]:
        """Return whether to emit this copy and whether its source is new to the transmission."""
        self._evict(now)
        self.rx[src] = self.rx.get(src, 0) + 1
        k = self.key(raw)
        transmission = self._seen.get(k)
        if transmission is not None and now - transmission.first_seen <= self.window:
            if src in transmission.sources:
                self._retire(transmission)
            else:
                transmission.sources.add(src)
                if not transmission.shared:
                    transmission.shared = True
                    self.both += 1
                if quality > transmission.best_quality:
                    transmission.best_quality = quality
                    return True, True
                self.dup += 1
                return False, True
        elif transmission is not None:
            self._retire(transmission)
        self._seen[k] = _Transmission(now, {src}, quality)
        self.novel += 1
        self.first[src] = self.first.get(src, 0) + 1
        return True, True

    def submit(self, src: str, raw: bytes, now: float,
               quality: tuple[int, ...] = (0,)) -> bool:
        return self.submit_detailed(src, raw, now, quality)[0]

    def _retire(self, transmission: _Transmission) -> None:
        if not transmission.shared and len(transmission.sources) == 1:
            source = next(iter(transmission.sources))
            self.only[source] = self.only.get(source, 0) + 1

    def _evict(self, now: float) -> None:
        """Retire keys older than the window, tallying the ones only a single source ever heard.
        Rate-limited to once per window so a busy channel does not rescan the dict every frame."""
        if now - self._last_evict < self.window:
            return
        self._last_evict = now
        dead = [
            key for key, transmission in self._seen.items()
            if now - transmission.first_seen > self.window
        ]
        for k in dead:
            self._retire(self._seen.pop(k))

    def flush(self) -> None:
        """Evict everything so ``only`` is final for a summary."""
        for transmission in self._seen.values():
            self._retire(transmission)
        self._seen.clear()
