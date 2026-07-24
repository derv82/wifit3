"""Always-on USB arrival/departure watch. Polls the cheap enumeration and reports what changed; the
app drives the timer and decides what to do (refresh the Splash list, or prompt to bring up a new
card). A dumb detector: no UI, no bring-up, no persistence (a replug is a fresh arrival)."""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from typing import Callable, List, Optional, Tuple

from wifit3.chips.driver import DeviceID
from wifit3.errors import WifiteFatalError
from wifit3.wlan.discovery import find_devices

logger = logging.getLogger(__name__)

OnChange = Callable[[List[DeviceID], List[DeviceID], List[DeviceID]], None]
OnFatal = Callable[[WifiteFatalError], None]


def _diff(current: List[DeviceID], seen: List[DeviceID]) -> Tuple[List[DeviceID], List[DeviceID]]:
    """(arrived, departed) as multisets keyed by (vid, pid), so two identical cards count twice and a
    replug of the same model reads as a genuine arrival. Order is not significant."""
    cur = Counter((d.vid, d.pid) for d in current)
    old = Counter((d.vid, d.pid) for d in seen)
    rep_cur = {(d.vid, d.pid): d for d in current}
    rep_old = {(d.vid, d.pid): d for d in seen}
    arrived: List[DeviceID] = []
    departed: List[DeviceID] = []
    for key in set(cur) | set(old):
        delta = cur[key] - old[key]
        if delta > 0:
            arrived += [rep_cur[key]] * delta
        elif delta < 0:
            departed += [rep_old[key]] * (-delta)
    return arrived, departed


class DeviceListener:
    """Owns the last-seen device set; the app calls ``poll_once`` on a timer."""

    def __init__(self, on_change: OnChange, on_fatal: Optional[OnFatal] = None) -> None:
        self._on_change = on_change      # (current, arrived, departed)
        self._on_fatal = on_fatal
        self._seen: List[DeviceID] = []
        self._paused = False
        self._stopped = False            # latched after a fatal so it isn't re-reported every tick

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def present(self) -> List[DeviceID]:
        return list(self._seen)

    async def poll_once(self) -> None:
        """One bus scan. When the present set changed, fire ``on_change(current, arrived, departed)``.
        A no-op while paused (during a bring-up, so the list can't churn and no second prompt stacks)
        or after a fatal backend error."""
        if self._paused or self._stopped:
            return
        try:
            current = await asyncio.to_thread(find_devices)
        except WifiteFatalError as err:
            self._stopped = True
            if self._on_fatal is not None:
                self._on_fatal(err)
            return
        arrived, departed = _diff(current, self._seen)
        if arrived or departed:
            self._seen = current
            self._on_change(current, arrived, departed)
