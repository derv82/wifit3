"""Always-on USB arrival/departure watch. Polls the cheap enumeration and reports what changed; the
app drives the timer and decides what to do (refresh the Splash list, or prompt to bring up a new
card). A dumb detector: no UI, no bring-up, no persistence (a replug is a fresh arrival)."""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, List, Optional, Tuple

from wifit3.chips.driver import DeviceID
from wifit3.errors import WifiteFatalError
from wifit3.wlan.discovery import find_devices

logger = logging.getLogger(__name__)

OnChange = Callable[[List[DeviceID], List[DeviceID], List[DeviceID]], None]
OnFatal = Callable[[WifiteFatalError], None]


def _diff(current: List[DeviceID], seen: List[DeviceID]) -> Tuple[List[DeviceID], List[DeviceID]]:
    """(arrived, departed) keyed by each card's instance_key (vid, pid, bus, address), so two identical
    models on different ports are distinct arrivals and a replug (new address) reads as a departure
    plus a fresh arrival. Order is not significant."""
    cur = {d.instance_key: d for d in current}
    old = {d.instance_key: d for d in seen}
    arrived = [d for k, d in cur.items() if k not in old]
    departed = [d for k, d in old.items() if k not in cur]
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
