"""Ctrl+C handling for the rtl8821au_dkms live-HW scripts.

``asyncio.run()`` (Python 3.11+) already installs a SIGINT handler that cancels the
running task on Ctrl+C — the correct mechanism, and it works on Windows. The trap is
that on the Windows proactor loop a long ``await asyncio.sleep(N)`` blocks the IOCP wait
for the whole N seconds, so the pending SIGINT isn't processed until the sleep ends (a
2 s listen window = Ctrl+C ignored for up to 2 s). ``interruptible_sleep`` sleeps in
short steps so the loop returns to Python often enough for the cancel to land within a
fraction of a second; when the task is cancelled, the inner sleep raises CancelledError,
which the caller catches to shut down gracefully.

Do NOT install a custom signal.signal/add_signal_handler here: an earlier version did,
which OVERRODE asyncio.run's own SIGINT handler — that is exactly what stopped Ctrl+C
from working.
"""
from __future__ import annotations

import asyncio


async def interruptible_sleep(total: float, step: float = 0.1) -> None:
    """Sleep ``total`` seconds in <= ``step`` increments (Ctrl+C-responsive on Windows).

    Raises CancelledError immediately if the task is cancelled (Ctrl+C) mid-sleep.
    """
    slept = 0.0
    while slept < total:
        await asyncio.sleep(min(step, total - slept))
        slept += step
