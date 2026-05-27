"""Shared RX reader thread for USB bulk-IN drivers.

Why this exists: reading + parsing RX on the asyncio event loop lets the TUI
starve RX — while the UI is busy, no `dev.read` is posted and the dongle's RX
FIFO overflows, dropping frames (~30% beacon loss / flaky handshakes; first
diagnosed + fixed per-chip on rtl8821au, commit 2e3a7a7). This class moves the
bulk reads onto a dedicated thread that keeps a read posted at all times,
independent of loop/UI load.

Composition, NOT a base class. Driver classes already have their own shape;
they differ only in *how they read* and *how they decode*, so each supplies
two callables:

  * ``read_once() -> bytes | None`` — one blocking bulk read; returns the raw
    buffer, or None on a benign timeout (no traffic). Runs ON THE READER
    THREAD. May raise on a real USB error (the reader counts consecutive
    errors and gives up after ``max_errors``).
  * ``dispatch(buf) -> None`` — decode the raw buffer into 802.11 frames and
    fan them to the driver's rx callback. Runs ON THE EVENT LOOP THREAD (via
    ``call_soon_threadsafe``), so it never races the UI's reads of the AP
    registry. The driver owns per-frame error handling.

The thread, the loop hand-off, a soft backpressure cap, and start/stop
lifecycle live here — once — so a fix or tweak (cap size, metrics, …) happens
in one place instead of N copy-pasted driver loops.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class RxReaderThread:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        read_once: Callable[[], Optional[bytes]],
        dispatch: Callable[[bytes], None],
        *,
        name: str = "rx",
        pending_cap: int = 256,
        max_errors: int = 5,
    ) -> None:
        self._loop = loop
        self._read_once = read_once
        self._dispatch = dispatch
        self._name = name
        self._cap = pending_cap
        self._max_errors = max_errors
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # Buffers handed to the loop but not yet dispatched. Advisory soft cap
        # so a momentarily-swamped loop drops here rather than ballooning
        # memory; the ±1 race between the two threads is harmless for a cap.
        self._pending = 0

    def start(self) -> None:
        """Spawn the reader thread. Idempotent."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name=self._name, daemon=True
        )
        self._thread.start()
        logger.info("[%s] RX reader thread started", self._name)

    async def stop(self, join_timeout: float = 1.5) -> None:
        """Signal the thread to exit and join it off the loop. The thread
        leaves within one ``read_once`` timeout once ``_running`` clears.
        Call this BEFORE releasing the USB handle it reads from."""
        self._running = False
        t, self._thread = self._thread, None
        if t is not None:
            await self._loop.run_in_executor(None, t.join, join_timeout)

    @property
    def running(self) -> bool:
        return self._running

    # -- thread side ---------------------------------------------------------

    def _run(self) -> None:
        consec_errors = 0
        while self._running:
            try:
                buf = self._read_once()
            except Exception as e:  # noqa: BLE001 — count + bail, don't crash the thread
                consec_errors += 1
                logger.warning("[%s] read failed (%d/%d): %s",
                               self._name, consec_errors, self._max_errors, e)
                if consec_errors >= self._max_errors:
                    logger.error("[%s] giving up after %d consecutive errors",
                                 self._name, consec_errors)
                    break
                time.sleep(0.01)
                continue
            consec_errors = 0
            if not buf:
                continue
            # Loop swamped and not draining — drop rather than balloon memory.
            if self._pending >= self._cap:
                continue
            self._pending += 1
            self._loop.call_soon_threadsafe(self._on_buffer, buf)
        logger.info("[%s] RX reader thread stopped", self._name)

    # -- loop side -----------------------------------------------------------

    def _on_buffer(self, buf: bytes) -> None:
        self._pending -= 1
        try:
            self._dispatch(buf)
        except Exception:
            logger.exception("[%s] dispatch raised", self._name)
