"""Shared Ctrl+C handling for the rtl8821au_dkms live-HW scripts.

On Windows the default asyncio proactor loop does NOT reliably raise KeyboardInterrupt
while the loop is awaiting (the IOCP wait isn't interrupted by SIGINT), so a plain
``try: while ...: await asyncio.sleep(N) except KeyboardInterrupt`` can ignore Ctrl+C
entirely until the sleep happens to end. This routes SIGINT to an ``asyncio.Event`` and
the loops poll it with short, interruptible sleeps so Ctrl+C is honoured within a
fraction of a second on every platform.
"""
from __future__ import annotations

import asyncio
import signal


def install_stop(loop: asyncio.AbstractEventLoop) -> asyncio.Event:
    """Return an Event that gets set on the first Ctrl+C (SIGINT / SIGTERM)."""
    stop = asyncio.Event()

    def _set() -> None:
        if not stop.is_set():
            print("\n[stopping — Ctrl+C, finishing the current step ...]")
        stop.set()

    try:
        loop.add_signal_handler(signal.SIGINT, _set)        # POSIX
        loop.add_signal_handler(signal.SIGTERM, _set)
    except (NotImplementedError, AttributeError):
        # Windows proactor loop: add_signal_handler is unsupported. The C-level SIGINT
        # handler runs on the main thread; bounce it onto the loop thread-safely (which
        # also wakes the loop) instead of raising KeyboardInterrupt.
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(_set))
    return stop


async def sleep_or_stop(stop: asyncio.Event, total: float, step: float = 0.15) -> bool:
    """Sleep up to ``total`` s, returning early (True) as soon as ``stop`` is set.

    Sleeps in <= ``step`` increments so the proactor loop returns to Python frequently
    and a pending Ctrl+C is processed within ``step`` s even on Windows, where it can't
    interrupt the wait directly. Returns whether ``stop`` is set on exit.
    """
    remaining = total
    while remaining > 0:
        if stop.is_set():
            return True
        await asyncio.sleep(min(step, remaining))
        remaining -= step
    return stop.is_set()
