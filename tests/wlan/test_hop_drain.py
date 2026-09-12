"""stop_hopping() must drain an in-flight channel tune before returning.

Reproduces the cross-family "bad Focus" bug (wifit3.log @ 03:49:53): a channel-hop
set_channel offloads to a run_in_executor thread that cancellation can't stop. When Focus
entry cancels the hop loop mid-tune, the orphaned tune keeps running and finishes *after*
stop_hopping returns, moving the chip onto a stale hop channel right as Focus pins its
target, so Focus shows 0 beacons. The fix shields the tune as a task and has stop_hopping
await it, so the chip is on a known channel (and current_channel is truthful) before the
caller's next set_channel runs.
"""
from __future__ import annotations

import asyncio
import threading

from wifit3.wlan.interface import WlanInterface


class _OrphanProneDriver:
    """A driver whose set_channel runs its tune on an executor thread: the real,
    uncancellable shape that orphans on a mid-tune cancel."""

    SUPPORTED_CHANNELS = [1, 6, 11]

    def __init__(self):
        self.current = None
        self.events: list[tuple[str, int]] = []
        self._lk = threading.Lock()
        self.in_flight = threading.Event()
        self.release_tune = threading.Event()

    def register_rx_callback(self, cb):
        pass

    def register_disconnect_callback(self, cb):
        pass

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        loop = asyncio.get_running_loop()

        def _sync():
            with self._lk:
                self.events.append(("start", channel))
            self.in_flight.set()
            self.release_tune.wait()
            self.current = channel
            with self._lk:
                self.events.append(("end", channel))

        await loop.run_in_executor(None, _sync)
        return True


async def test_stop_hopping_drains_inflight_tune():
    drv = _OrphanProneDriver()
    iface = WlanInterface(driver_instance=drv, name="wlan0", description="t")

    await iface.start_hopping([1, 6, 11], interval=0.001)
    await asyncio.get_running_loop().run_in_executor(None, drv.in_flight.wait)
    stop_task = asyncio.create_task(iface.stop_hopping())
    await asyncio.sleep(0)
    drv.release_tune.set()
    await stop_task

    starts = [c for (e, c) in drv.events if e == "start"]
    ends = [c for (e, c) in drv.events if e == "end"]
    assert starts == ends, f"in-flight tune not drained before stop_hopping returned: {drv.events}"


async def test_focus_pin_lands_after_stop_hopping():
    """The user-visible symptom: after stop_hopping, the Focus set_channel must stick:
    no orphan tune comes along afterward to move the chip off the focused channel."""
    drv = _OrphanProneDriver()
    iface = WlanInterface(driver_instance=drv, name="wlan0", description="t")

    await iface.start_hopping([1, 6, 11], interval=0.001)
    await asyncio.get_running_loop().run_in_executor(None, drv.in_flight.wait)
    stop_task = asyncio.create_task(iface.stop_hopping())
    await asyncio.sleep(0)
    drv.release_tune.set()
    await stop_task

    await iface.set_channel(99, scan=False)   # Focus pins its target
    assert drv.current == 99, f"chip moved off the Focus channel after the pin: current={drv.current}"
