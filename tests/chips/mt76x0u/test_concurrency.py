"""Reproduces the Focus-entry channel-tune race on MT7610U (mt76x0u): two tunes running
concurrently on the USB device.

Root cause (proven from wifit3.log @ 02:54:29.9): entering Focus stops channel hopping, but
``stop_hopping``'s ``await task`` returns while the cancelled hop tune's ``run_in_executor``
thread is still draining ``set_channel_20mhz`` on the control endpoint. Focus's ``set_channel``
then spawns a second thread; the two interleave their RF/BBP register batches and the chip
lands on a corrupt channel ("0 beacons until you re-enter Focus"). Same class as the rt3070
RX-DMA wedge — fixed the same way, with a ``threading.Lock`` held by the executor work.

No hardware: ``set_channel_20mhz`` is stubbed with a thread-concurrency counter. Run from two
threads it overlaps (>=2); through ``driver._set_channel_sync`` (which holds ``_hw_lock``) it
serializes (==1).
"""
from __future__ import annotations

import threading
import time

from wifit3.chips.mt76x0u import driver as driver_mod
from wifit3.chips.mt76x0u.driver import MT76x0UDriver


class _ConcurrencyCounter:
    """Stands in for set_channel_20mhz; records the peak number of threads inside it."""

    def __init__(self):
        self._inside = 0
        self.max_concurrent = 0
        self._lk = threading.Lock()

    def __call__(self, transport, mcu, channel, efuse_full=None):
        with self._lk:
            self._inside += 1
            self.max_concurrent = max(self.max_concurrent, self._inside)
        try:
            time.sleep(0.01)               # widen the window so a real race is caught
            return {"ch": channel}
        finally:
            with self._lk:
                self._inside -= 1


def _make_driver() -> MT76x0UDriver:
    """A driver with just the state _set_channel_sync touches — no real USB."""
    d = MT76x0UDriver.__new__(MT76x0UDriver)
    d._hw_lock = threading.Lock()
    d.current_channel = None
    d.efuse_full = None
    d.transport = None
    d.mcu = None
    d.last_set_channel_state = None
    return d


def _run_two(fn) -> None:
    t1 = threading.Thread(target=lambda: fn(1))
    t2 = threading.Thread(target=lambda: fn(6))
    t1.start()
    t2.start()
    t1.join()
    t2.join()


def test_raw_tune_from_two_threads_collides():
    """The bug, proven: two un-serialized tunes DO run on the device concurrently."""
    counter = _ConcurrencyCounter()
    _run_two(lambda ch: counter(None, None, ch))
    assert counter.max_concurrent >= 2, "expected the race (>=2 threads in the tune at once)"


def test_set_channel_sync_is_serialized_by_hw_lock(monkeypatch):
    """The fix: _set_channel_sync holds _hw_lock, so two concurrent tunes never overlap —
    a cancelled hop tune's still-draining thread blocks the Focus tune instead of colliding."""
    counter = _ConcurrencyCounter()
    monkeypatch.setattr(driver_mod, "set_channel_20mhz", counter)
    d = _make_driver()
    _run_two(d._set_channel_sync)
    assert counter.max_concurrent == 1, (
        f"_hw_lock failed: {counter.max_concurrent} tunes overlapped on the device"
    )
