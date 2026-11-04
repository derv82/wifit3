from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from tests.verify_pcap.capture_replay import CaptureDivergence, CaptureReplay
from tests.verify_pcap.test_waivers import REALTEK_OTHER_REGISTER, realtek_write
from tests.verify_pcap.virtual_clock_loop import KERNEL_HZ, MAIN_CALLER, VirtualClockEventLoop, current_caller_name, timer_wheel_expiry

WATCHDOG_JIFFIES = 62


def replay_watchdog_sleeping(sleep_jiffies: int) -> str | None:
    """A driver task makes the first captured op, sleeps, then makes the second, captured WATCHDOG_JIFFIES later."""
    return replay_driver_task_sleeping(sleep_jiffies / KERNEL_HZ, (WATCHDOG_JIFFIES + 1) / KERNEL_HZ, is_hrtimer=False)


def replay_driver_task_sleeping(sleep: float, second_op_time: float, is_hrtimer: bool) -> str | None:
    ops = [replace(realtek_write(REALTEK_OTHER_REGISTER, 0x1), submit_time=0.0, completion_time=0.0),
           replace(realtek_write(REALTEK_OTHER_REGISTER, 0x2), submit_time=second_op_time,
                   completion_time=second_op_time)]

    async def watchdog() -> None:
        replay.match_request(ops[0], current_caller_name())
        await asyncio.sleep(sleep)
        replay.match_request(ops[1], current_caller_name())

    hrtimer_callers = frozenset({watchdog.__qualname__}) if is_hrtimer else frozenset()
    loop = VirtualClockEventLoop(0.0, lambda: True, hrtimer_callers)
    replay = CaptureReplay(ops, lambda captured, request: captured.same_request(request),
                           lambda request, cursor_differs: False, lambda ops, position: None,
                           lambda caller_ops, request: False, loop)

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


def test_hrtimer_is_not_rounded_to_a_jiffy() -> None:
    async def hrtimer_task() -> float:
        await asyncio.sleep(0.001)
        return asyncio.get_running_loop().time()

    loop = VirtualClockEventLoop(0.0, lambda: True, frozenset({hrtimer_task.__qualname__}))
    try:
        assert loop.run_until_complete(loop.create_task(hrtimer_task())) == pytest.approx(0.001)
    finally:
        loop.close()


def test_hrtimer_at_the_captured_time_matches() -> None:
    assert replay_driver_task_sleeping(0.001, 0.0011, is_hrtimer=True) is None


def test_hrtimer_firing_early_diverges() -> None:
    reason = replay_driver_task_sleeping(0.001, 0.0025, is_hrtimer=True)
    assert reason is not None and "timer fired" in reason


def test_timer_wheel_level_0_rounds_the_expiry_up_to_the_next_bucket() -> None:
    assert timer_wheel_expiry(0.0, 0.0, WATCHDOG_JIFFIES / KERNEL_HZ) == pytest.approx((WATCHDOG_JIFFIES + 1) / KERNEL_HZ)


def test_timer_wheel_level_1_rounds_the_expiry_up_to_the_next_8_jiffy_bucket() -> None:
    assert timer_wheel_expiry(0.0, 3 / KERNEL_HZ, 503 / KERNEL_HZ) == pytest.approx(504 / KERNEL_HZ)
    assert timer_wheel_expiry(0.0, 4 / KERNEL_HZ, 504 / KERNEL_HZ) == pytest.approx(512 / KERNEL_HZ)


def test_woken_driver_task_passes_over_a_main_op_captured_within_its_timer_tolerance() -> None:
    """The kernel timer fired a millisecond before the modeled one; main's op came between the task's two ops."""
    fire_time = WATCHDOG_JIFFIES / KERNEL_HZ - 0.001
    ops = [replace(realtek_write(REALTEK_OTHER_REGISTER, value), submit_time=submit_time, completion_time=submit_time)
           for value, submit_time in [(0x1, 0.0), (0x2, fire_time), (0x3, fire_time + 0.0001),
                                      (0x4, fire_time + 0.0002)]]
    loop = VirtualClockEventLoop(0.0, lambda: True)
    replay = CaptureReplay(ops, lambda captured, request: captured.same_request(request),
                           lambda request, cursor_differs: False, lambda ops, position: None,
                           lambda caller_ops, request: False, loop)

    async def watchdog() -> None:
        replay.match_request(ops[0], current_caller_name())
        await asyncio.sleep(WATCHDOG_JIFFIES / KERNEL_HZ)
        replay.match_request(ops[1], current_caller_name())
        replay.match_request(ops[3], current_caller_name())

    try:
        loop.run_until_complete(loop.create_task(watchdog()))
    finally:
        loop.close()
    assert replay.divergence is None
    assert replay.unreproduced_positions() == [2]


def test_hrtimer_read_does_not_wait_on_another_callers_op() -> None:
    """The capture's read comes 2.7 ms after the hrtimer, behind main's op: the kernel made it late, not waiting."""
    ops = [replace(realtek_write(REALTEK_OTHER_REGISTER, value), submit_time=submit_time, completion_time=submit_time)
           for value, submit_time in [(0x1, 0.0), (0x2, 0.0015), (0x3, 0.0038)]]

    async def hrtimer_task() -> None:
        replay.match_request(ops[0], current_caller_name())
        await asyncio.sleep(0.001)
        replay.match_request(ops[2], current_caller_name())

    loop = VirtualClockEventLoop(0.0, lambda: True, frozenset({hrtimer_task.__qualname__}))
    replay = CaptureReplay(ops, lambda captured, request: captured.same_request(request),
                           lambda request, cursor_differs: False, lambda ops, position: None,
                           lambda caller_ops, request: False, loop)

    async def main() -> None:
        try:
            await loop.create_task(hrtimer_task())
        except CaptureDivergence:
            pass

    try:
        loop.run_until_complete(loop.create_task(main(), name=MAIN_CALLER))
    finally:
        loop.close()
    assert replay.divergence is not None and "timer fired" in replay.divergence.reason


def test_new_driver_task_starts_at_its_creators_time() -> None:
    async def driver_task() -> float:
        return asyncio.get_running_loop().time()

    loop = VirtualClockEventLoop(0.0, lambda: True)
    loop.completion_time_by_caller[driver_task.__qualname__] = 0.5
    loop.virtual_time = 1.0
    try:
        assert loop.run_until_complete(loop.create_task(driver_task())) == 1.0
    finally:
        loop.close()
