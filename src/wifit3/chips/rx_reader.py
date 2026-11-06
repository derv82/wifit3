"""Shared RX reader thread for USB bulk-IN drivers."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable, Iterable, Optional

from wifit3.errors import is_device_gone

logger = logging.getLogger(__name__)

DROP_LOG_PERIOD = 2.0  # Log drop errors once every (seconds)
PAUSE_POLL = 0.003     # wait while paused (seconds)
MAX_BATCH_SIZE = 64    # frame buffers per batch
MAX_BACKLOG = 256      # backlog = produced - consumed
MAX_SPLIT_BACKLOG = 8192
MAX_BACKLOG_BYTES = 4 * 1024 * 1024
EMPTY_SPIN_LIMIT = 8   # consecutive zero-length reads before backing off
EMPTY_BACKOFF = 0.001  # wait after a zero-length read run (seconds)


class RxReaderThread:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        read_once: Callable[[], Optional[bytes]],
        dispatch: Callable[[bytes], None],
        *,
        name: str = "rx",
        max_errors: int = 5,
        on_fatal: Optional[Callable[[Exception], None]] = None,
        split: Optional[Callable[[bytes], Iterable[bytes]]] = None,
    ) -> None:
        self._loop = loop
        self._read_once = read_once
        self._dispatch = dispatch
        self._name = name
        self._max_errors = max_errors
        self._on_fatal = on_fatal  # fires on unplug, wedged errors
        self._split = split
        self._backlog_lock = threading.Lock()
        self._backlog_count = 0
        self._backlog_bytes = 0

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._pause_req = threading.Event()
        self._paused = threading.Event()
        self._fatal_fired = False
        self._bufs_produced = 0  # total buffers sent to be dispatched
        self._bufs_consumed = 0  # total buffers the loop has finished dispatching
        self._dropped = 0
        self._next_drop_log = 0.0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        logger.debug(f"[{self._name}] RX reader thread started")

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
        """Loops over blocking reads, batches & submits buffers."""
        if self._split is not None:
            self._run_split()
            return
        consec_errors = 0
        consec_empty = 0
        batch: list[bytes] = []
        while self._running:
            if self._pause_req.is_set():
                self._paused.set()
                time.sleep(PAUSE_POLL)
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
            if buf:
                batch.append(buf)
                consec_empty = 0
            else:
                consec_empty += 1
                if consec_empty >= EMPTY_SPIN_LIMIT:
                    time.sleep(EMPTY_BACKOFF)
                if not batch:
                    continue
            loop_busy = self._bufs_produced != self._bufs_consumed
            if loop_busy and len(batch) < MAX_BATCH_SIZE:
                continue  # the loop is still on the previous batch; keep collecting
            now = time.monotonic()

            # Check for backlogged consumer
            if self._bufs_produced - self._bufs_consumed >= MAX_BACKLOG:
                # Backlog full, report & drop
                self._dropped += len(batch)
                if now >= self._next_drop_log:
                    self._next_drop_log = now + DROP_LOG_PERIOD
                    logger.error(f"[{self._name}] RX dropped, {self._dropped} total (backlog full)")
                batch = []
                continue

            # Submit the batch
            self._bufs_produced += len(batch)
            self._loop.call_soon_threadsafe(self._dispatch_batch, batch)
            batch = []
        self._running = False
        if self._dropped:
            logger.error(f"[{self._name}] RX reader stopped: {self._dropped} buffers dropped total")
        logger.debug(f"[{self._name}] RX reader thread stopped")

    def _run_split(self) -> None:
        consec_errors = 0
        consec_empty = 0
        while self._running:
            if self._pause_req.is_set():
                self._paused.set()
                time.sleep(PAUSE_POLL)
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
            if not buf:
                consec_empty += 1
                if consec_empty >= EMPTY_SPIN_LIMIT:
                    time.sleep(EMPTY_BACKOFF)
                continue
            consec_empty = 0
            batch: list[bytes] = []
            try:
                for record in self._split(buf):
                    if self._pause_req.is_set() and batch:
                        self._submit_split_batch(batch)
                        batch = []
                    while self._running and self._pause_req.is_set():
                        self._paused.set()
                        time.sleep(PAUSE_POLL)
                    if not self._running:
                        break
                    with self._backlog_lock:
                        if (self._backlog_count >= MAX_SPLIT_BACKLOG or
                                self._backlog_bytes + len(record) > MAX_BACKLOG_BYTES):
                            accepted = False
                        else:
                            self._backlog_count += 1
                            self._backlog_bytes += len(record)
                            accepted = True
                    if accepted:
                        batch.append(record)
                        if len(batch) >= MAX_BATCH_SIZE:
                            self._submit_split_batch(batch)
                            batch = []
                    else:
                        self._dropped += 1
                        now = time.monotonic()
                        if now >= self._next_drop_log:
                            self._next_drop_log = now + DROP_LOG_PERIOD
                            logger.error(f"[{self._name}] RX dropped, {self._dropped} total (backlog full)")
            except Exception:
                logger.exception(f"[{self._name}] RX split failed")
            finally:
                if batch:
                    self._submit_split_batch(batch)
        self._running = False
        if self._dropped:
            logger.error(f"[{self._name}] RX reader stopped: {self._dropped} records dropped total")
        logger.debug(f"[{self._name}] RX reader thread stopped")

    def _submit_split_batch(self, batch: list[bytes]) -> None:
        try:
            self._loop.call_soon_threadsafe(self._dispatch_split_batch, batch)
        except RuntimeError:
            self._release_split_batch(batch)
            logger.exception(f"[{self._name}] RX batch submission failed")

    def _release_split_batch(self, batch: list[bytes]) -> None:
        with self._backlog_lock:
            self._backlog_count -= len(batch)
            self._backlog_bytes -= sum(map(len, batch))

    def _dispatch_split_batch(self, batch: list[bytes]) -> None:
        try:
            for record in batch:
                try:
                    self._dispatch(record)
                except Exception:
                    logger.exception(f"[{self._name}] dispatch raised")
        finally:
            self._release_split_batch(batch)

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

    # -- loop side -----------------------------------------------------------

    def _dispatch_batch(self, batch: list[bytes]) -> None:
        for buf in batch:
            try:
                self._dispatch(buf)
            except Exception:
                logger.exception(f"[{self._name}] dispatch raised")
        self._bufs_consumed += len(batch)
