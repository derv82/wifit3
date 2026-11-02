"""TX status FIFO draining for MT7601U.

Ported from driver_sources/mt7601u-source-v7.2/mt7601u/mac.c:157
(mt7601u_mac_fetch_tx_status) and dma.c:278 (mt7601u_tx_tasklet), which queues
stat_work 10 ms after every completed transmit.

The FIFO is read-pop: every read of MT_TX_STAT_FIFO yields one status word and
consumes it, so an undrained FIFO eventually stops reporting. On this silicon the
register behaves as a free-running counter and a VALID word appears on a wrap rather
than per frame -- 74 injected frames yielded 10 statuses -- so an entry here is not
evidence that a frame was transmitted. See MT7601U.md.

Upstream does this in a delayed work re-armed while entries remain, because the
entries arrive asynchronously. Nothing here does: submit() drains inline on the
same call that transmitted, which is the tightest possible approximation and
needs no timer.
"""
from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    MT_TX_STAT_FIFO,
    MT_TX_STAT_FIFO_ACKREQ,
    MT_TX_STAT_FIFO_AGGR,
    MT_TX_STAT_FIFO_PID_TYPE,
    MT_TX_STAT_FIFO_RATE,
    MT_TX_STAT_FIFO_SUCCESS,
    MT_TX_STAT_FIFO_VALID,
    MT_TX_STAT_FIFO_WCID,
)
from .transport import MT7601UTransport

DRAIN_LIMIT = 64
"""Upper bound on entries per drain.

The kernel's work loop stops when a fetch comes back invalid, so it cannot
spin forever. A per-call cap does the same here without depending on the FIFO
eventually being empty, which a wedged MAC may never report.
"""


@dataclass(frozen=True)
class TxStatus:
    """One popped entry from MT_TX_STAT_FIFO (mac.c:157 mt76_tx_status)."""

    success: bool
    aggregate: bool
    ack_requested: bool
    packet_id: int
    wcid: int
    rate: int


class TxStatusFifo:
    """Read-pop access to the per-frame TX status the MAC emits."""

    def __init__(self, transport: MT7601UTransport) -> None:
        self.transport = transport

    def fetch(self) -> TxStatus | None:
        """Pop one entry, or None when the FIFO is empty (mac.c:157)."""
        val = self.transport.rr(MT_TX_STAT_FIFO)
        if not val & MT_TX_STAT_FIFO_VALID:
            return None
        return TxStatus(
            success=bool(val & MT_TX_STAT_FIFO_SUCCESS),
            aggregate=bool(val & MT_TX_STAT_FIFO_AGGR),
            ack_requested=bool(val & MT_TX_STAT_FIFO_ACKREQ),
            packet_id=(val & MT_TX_STAT_FIFO_PID_TYPE) >> 1,
            wcid=(val & MT_TX_STAT_FIFO_WCID) >> 8,
            rate=(val & MT_TX_STAT_FIFO_RATE) >> 16,
        )

    def drain(self, limit: int = DRAIN_LIMIT) -> list[TxStatus]:
        """Pop every pending entry, up to ``limit``.

        mac.c's stat_work loops while fetch returns valid and re-arms itself if
        it cleaned anything; the cap replaces the re-arming.
        """
        out: list[TxStatus] = []
        for _ in range(limit):
            entry = self.fetch()
            if entry is None:
                break
            out.append(entry)
        return out
