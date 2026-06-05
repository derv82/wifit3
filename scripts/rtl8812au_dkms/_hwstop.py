"""Ctrl+C handling for the rtl8812au_dkms live-HW scripts.

``asyncio.run()`` (Python 3.11+) installs a SIGINT handler that cancels the running task
on Ctrl+C — the correct mechanism, and it works on Windows. The trap is that on the
Windows proactor loop a long ``await asyncio.sleep(N)`` blocks the IOCP wait for the
whole N seconds, so a pending SIGINT isn't processed until the sleep ends.
``interruptible_sleep`` sleeps in short steps so the loop returns to Python often enough
for the cancel to land quickly. Do NOT install a custom signal handler here — that
overrides asyncio.run's own and breaks Ctrl+C.
"""
from __future__ import annotations

import asyncio


async def interruptible_sleep(total: float, step: float = 0.1) -> None:
    """Sleep ``total`` seconds in <= ``step`` increments (Ctrl+C-responsive on Windows)."""
    slept = 0.0
    while slept < total:
        await asyncio.sleep(min(step, total - slept))
        slept += step
