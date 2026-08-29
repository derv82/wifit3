"""Shared RX reader thread for USB bulk-IN drivers."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable, Optional

from wifit3.errors import is_device_gone

logger = logging.getLogger(__name__)

_DROP_LOG_PERIOD = 2.0  # Log drop errors once every (seconds)
_PAUSE_POLL = 0.003   # wait while paused (seconds)


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
        stats: bool = False,
        on_fatal: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        self._loop = loop
        self._read_once = read_once
        self._dispatch = dispatch
        self._name = name
        self._cap = pending_cap
        self._max_errors = max_errors
        self._stats = stats  # log stats about BULK-in bytes
        self._on_fatal = on_fatal  # fires on unplug, wedged errors
    
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._pause_req = threading.Event()
        self._paused = threading.Event()
        self._fatal_fired = False
        self._pending = 0  # buffers not yet dispatched
        self._n_bytes = 0
        self._dropped = 0  # Lost buffers, discarded due to cap limit
        self._next_drop_log = 0.0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        logger.info(f"[{self._name}] RX reader thread started")

    async def stop(self, join_timeout: float = 1.5) -> None:
        """waits up to one ``read_once`` timeout once ``_running`` clears."""
        self._running = False
        t, self._thread = self._thread, None
        if t is not None:
            await self._loop.run_in_executor(None, t.join, join_timeout)

    @property
    def running(self) -> bool:
        return self._running

    def pause(self, wait_timeout: float = 0.25) -> bool:
        """Stop issuing bulk-IN reads and block until the reader is idle."""
        self._pause_req.set()
        if not self._running:
            return True
        return self._paused.wait(wait_timeout)

    def resume(self) -> None:
        """The reader resumes bulk-IN reads on its next loop."""
        self._paused.clear()
        self._pause_req.clear()

    # -- thread side ---------------------------------------------------------

    def _run(self) -> None:
        consec_errors = 0
        next_report = time.monotonic() + 2.0 if self._stats else float("inf")
        while self._running:
            if self._sleep_if_paused():
                continue
            try:
                buf = self._read_once()
            except Exception as e:
                consec_errors += 1
                if self._is_fatal(e, consec_errors):
                    break
                time.sleep(0.01)
                continue
            consec_errors = 0
            self._emit_buffer(buf)
            if self._stats and time.monotonic() >= next_report:
                logger.info(f"[{self._name}] RX 2s: produced {self._n_bytes} bytes")
                self._n_bytes = 0
                next_report = time.monotonic() + 2.0
        if self._dropped:
            logger.error(f"[{self._name}] RX reader stopped: {self._dropped} buffers dropped total")
        logger.info(f"[{self._name}] RX reader thread stopped")

    def _sleep_if_paused(self) -> bool:
        if not self._pause_req.is_set():
            return False
        self._paused.set()
        time.sleep(_PAUSE_POLL)
        return True

    def _is_fatal(self, e: Exception, consec_errors: int) -> bool:
        logger.warning(f"[{self._name}] read failed ({consec_errors}/{self._max_errors}): {e}")
        if is_device_gone(e):
            logger.error(f"[{self._name}] device gone: {e}")
            self._fire_fatal(e)  # unplugged
            return True
        if consec_errors >= self._max_errors:
            logger.error(f"[{self._name}] giving up after {consec_errors} consecutive errors")
            self._fire_fatal(e)  # error streak
            return True
        return False

    def _fire_fatal(self, exc: Exception) -> None:
        if self._fatal_fired or self._on_fatal is None:
            return
        self._fatal_fired = True
        self._loop.call_soon_threadsafe(self._on_fatal, exc)

    def _emit_buffer(self, buf: bytes) -> None:
        if not buf:
            return
        if self._pending < self._cap:
            self._pending += 1
            self._n_bytes += len(buf)
            self._loop.call_soon_threadsafe(self._on_buffer, buf)
            return
        self._dropped += 1
        now = time.monotonic()
        if now >= self._next_drop_log:
            logger.error(f"[{self._name}] RX DROPPED: cap ({self._cap}), total {self._dropped} dropped")
            self._next_drop_log = now + _DROP_LOG_PERIOD

    # -- loop side -----------------------------------------------------------

    def _on_buffer(self, buf: bytes) -> None:
        self._pending -= 1
        try:
            self._dispatch(buf)
        except Exception:
            logger.exception(f"[{self._name}] dispatch raised")
