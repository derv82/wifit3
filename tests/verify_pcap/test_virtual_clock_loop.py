from __future__ import annotations

import asyncio
from dataclasses import replace

from tests.verify_pcap.capture_replay import CaptureDivergence, CaptureReplay
from tests.verify_pcap.test_waivers import REALTEK_OTHER_REGISTER, realtek_write
from tests.verify_pcap.virtual_clock_loop import KERNEL_HZ, MAIN_CALLER, VirtualClockEventLoop, current_caller_name

WATCHDOG_JIFFIES = 62


def replay_watchdog_sleeping(sleep_jiffies: int) -> str | None:
    """A driver task makes the first captured op, sleeps, then makes the second, captured WATCHDOG_JIFFIES later."""
    ops = [replace(realtek_write(REALTEK_OTHER_REGISTER, 0x1), submit_time=0.0, completion_time=0.0),
           replace(realtek_write(REALTEK_OTHER_REGISTER, 0x2), submit_time=(WATCHDOG_JIFFIES + 1) / KERNEL_HZ,
                   completion_time=(WATCHDOG_JIFFIES + 1) / KERNEL_HZ)]
    loop = VirtualClockEventLoop(0.0, lambda: True)
    replay = CaptureReplay(ops, lambda captured, request: captured.same_request(request),
                           lambda request, cursor_differs: False, lambda ops, position: None, loop)

    async def watchdog() -> None:
        replay.match_request(ops[0], current_caller_name())
        await asyncio.sleep(sleep_jiffies / KERNEL_HZ)
        replay.match_request(ops[1], current_caller_name())

    async def main() -> None:
        try:
            await loop.create_task(watchdog())
        except CaptureDivergence:
            pass

    try:
        loop.run_until_complete(loop.create_task(main(), name=MAIN_CALLER))
    finally:
        loop.close()
    if replay.divergence is None:
        return None
    return replay.divergence.reason


def test_driver_task_timer_at_the_captured_period_matches() -> None:
    assert replay_watchdog_sleeping(WATCHDOG_JIFFIES) is None


def test_driver_task_timer_firing_early_diverges() -> None:
    reason = replay_watchdog_sleeping(50)
    assert reason is not None and "timer fired" in reason
