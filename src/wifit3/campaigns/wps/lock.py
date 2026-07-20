"""WPS lockout detection + adaptive backoff.

Two lock signals, mirroring bully:
  * the AP-Setup-Locked IE in beacons (already decoded into
    ``AccessPoint.wps_locked`` by the frame parser), and
  * a heuristic: N consecutive setup rejections / NACKs before the AP ever
    judges a PIN half (for APs that lock silently and never set the IE).

Backoff is *measured*, not a blind constant: we time how long the AP stays
locked and bias the next wait toward that, so a fleet of differently-behaving
routers each settle to their own real lockout period instead of reaver's flat
60s / bully's 43s.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List


@dataclass
class LockTracker:
    # Treat this many consecutive rejects (before the AP judges a PIN half) as a silent lock.
    strike_threshold: int = 3
    # Backoff bounds (seconds). Start conservative; learn from there.
    min_wait: float = 30.0
    max_wait: float = 360.0
    initial_wait: float = 60.0

    strikes: int = 0
    locked_since: float = 0.0
    _observed_durations: List[float] = field(default_factory=list)

    def note_progress(self) -> None:
        """A PIN half got decided (M5 or NACK): real progress, not a lock."""
        self.strikes = 0

    def note_reject_before_pin_answer(self) -> None:
        self.strikes += 1

    def note_setup_locked(self) -> None:
        """AP explicitly signalled WPS Setup-Locked (WSC config_error 15): lock at once,
        rather than waiting for the silent-lock strike heuristic to reach threshold."""
        self.strikes = self.strike_threshold

    def is_locked(self, beacon_locked: bool) -> bool:
        return beacon_locked or self.strikes >= self.strike_threshold

    def begin_lock(self) -> None:
        if not self.locked_since:
            self.locked_since = time.monotonic()

    def end_lock(self) -> None:
        if self.locked_since:
            self._observed_durations.append(time.monotonic() - self.locked_since)
            self.locked_since = 0.0
        self.strikes = 0

    def backoff(self) -> float:
        """Seconds to wait before re-checking. Biased toward the longest lock
        we've actually observed on this AP, clamped to [min, max]."""
        if self._observed_durations:
            learned = max(self._observed_durations) * 1.1
        else:
            learned = self.initial_wait
        return max(self.min_wait, min(self.max_wait, learned))
