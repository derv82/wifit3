"""Transmit queue management for MT7601U.

Ported from driver_sources/mt7601u-source-v7.2/mt7601u/dma.c: mt7601u_dma_submit_tx,
mt7601u_complete_tx, mt7601u_alloc_tx_queue and mt7601u_free_tx_queue.

This is not a device-memory ring. dma.c fills a pre-allocated USB bulk URB with the
wrapped frame, submits it, and advances q->end / q->used; the completion callback
decrements q->used. There is no MT_TX_DONE kick and no host-pointer programming in this
driver. The queue exists to bound how many frames may be in flight and to tell the
caller when that bound is reached.
"""
from __future__ import annotations

from .mcu import MT7601UMcu
from .transport import MT7601UTransport
from .tx_status import DRAIN_LIMIT, TxStatus, TxStatusFifo
from .tx import (
    TX_NO_STATION,
    TX_QUEUE_INJECT,
    TX_QUEUE_COUNT,
    TX_RING_ENTRIES,
    build_tx_dma,
)

TX_QUEUE_ENTRIES = TX_RING_ENTRIES
"""mt7601u.h:81 N_TX_ENTRIES, matched rather than trimmed so a replay divergence
means a porting error rather than a ring-shape mismatch."""

TX_TIMEOUT_MS = 300
"""usb.c: the same 300 ms window the other bulk transfers use."""

TX_STATUS_HISTORY = 64
"""Statuses kept for the caller. Bounds memory on a long-running scanner; the
kernel hands each one to mac80211 and forgets it, so nothing reads them twice."""


class TxQueue:
    """One OUT endpoint's in-flight frame pool."""

    def __init__(self, transport: MT7601UTransport, mcu: MT7601UMcu, *,
                 queue: int = TX_QUEUE_INJECT, entries: int = TX_QUEUE_ENTRIES,
                 wcid: int = TX_NO_STATION) -> None:
        if not 0 <= queue < TX_QUEUE_COUNT:
            raise ValueError(f"queue {queue} outside the {TX_QUEUE_COUNT} OUT endpoints")
        if entries < 1:
            raise ValueError(f"a queue needs at least one entry, got {entries}")
        self.transport = transport
        self.mcu = mcu
        self.queue = queue
        self.wcid = wcid
        self.entries = entries
        self.end = 0
        self.used = 0
        self.closed = False
        self.statuses: list[TxStatus] = []

    def free_entries(self) -> int:
        """dma.c's ``entries - used``: how many frames may still be submitted."""
        return self.entries - self.used

    def submit(self, frame: bytes, *, ack: bool = True) -> bool:
        """Send one frame. False means it was not sent -- the caller must not
        treat that as success.

        dma.c returns -ENOSPC when the queue is full and propagates a URB
        submission failure; neither may look like a completed transmit.
        """
        if self.closed:
            return False
        # Build before touching the queue so a rejected frame changes nothing.
        payload = build_tx_dma(frame, ack=ack, wcid=self.wcid, queue=self.queue)
        if self.used >= self.entries:
            return False
        try:
            self.transport.bulk_out_tx(payload, self.queue, TX_TIMEOUT_MS)
        except (TimeoutError, OSError):
            return False
        self.end = (self.end + 1) % self.entries
        self.used += 1
        # dma.c:278 arms a 10ms delayed work on every completion to pop
        # MT_TX_STAT_FIFO. The FIFO is read-pop, so leaving it undrained is what
        # makes a TX result untrustworthy. Draining inline is the tightest
        # approximation and needs no timer.
        self.collect_status()
        # bulk_out_tx is synchronous, so this frame is already on the air: release its
        # slot here. The kernel releases q->used in the async URB completion callback,
        # which has no counterpart in this port. Retiring on a popped status instead
        # ratcheted used to entries after ~65 injects and refused every frame after
        # that, measured on hardware while the endpoint still accepted 96-byte writes.
        self.pump(1)
        return True

    def collect_status(self, limit: int = DRAIN_LIMIT) -> list[TxStatus]:
        """Pop pending TX status entries into the recent-status window.

        Popping does not retire a ring slot: MT_TX_STAT_FIFO is a free-running counter
        that yields a valid entry on a wrap rather than per frame, so slots retired
        against it leak. ``submit`` releases the slot instead.
        """
        statuses = TxStatusFifo(self.transport).drain(limit)
        self.statuses.extend(statuses)
        del self.statuses[:-TX_STATUS_HISTORY]
        return statuses

    def pump(self, completed: int = 1) -> int:
        """Release ``completed`` finished frames. Returns how many were released."""
        if completed <= 0:
            return 0
        released = min(completed, self.used)
        self.used -= released
        return released

    def close(self) -> None:
        """mt7601u_free_tx_queue. In-flight frames are dropped; the chip is reset
        by the caller's chip_offoff(False) immediately after."""
        self.closed = True
        self.used = 0
        self.end = 0
        self.statuses.clear()


class TxQueues:
    """All six OUT queues, indexed the way usb.h numbers the endpoints."""

    def __init__(self, transport: MT7601UTransport, mcu: MT7601UMcu, *,
                 entries: int = TX_QUEUE_ENTRIES) -> None:
        self.queues = [TxQueue(transport, mcu, queue=q, entries=entries)
                      for q in range(TX_QUEUE_COUNT)]

    def __getitem__(self, queue: int) -> TxQueue:
        return self.queues[queue]

    def free_entries(self) -> int:
        return sum(q.free_entries() for q in self.queues)

    def close(self) -> None:
        for q in self.queues:
            q.close()
