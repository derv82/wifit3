"""Periodic MAC statistics and the MAC-wedge recovery for MT7601U.

Ported from driver_sources/mt7601u-source-v6.19/mac.c: mt7601u_mac_work and
mt7601u_check_mac_err. The counters are read-to-clear, so the sweep is not optional
bookkeeping -- it is what keeps them from saturating.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .constants import (
    MT_MAC_SYS_CTRL,
    MT_MAC_SYS_CTRL_RESET_CSR,
    MT_MPDU_DENSITY_CNT,
    MT_RX_STA_CNT0,
    MT_TX_AGG_CNT_BASE0,
    MT_TX_AGG_CNT_BASE1,
    MT_TX_AGG_STAT,
    MT_TX_STA_CNT0,
)
from .transport import MT7601UTransport

logger = logging.getLogger(__name__)

MAC_ERR_STATUS = 0x10F4
"""mac.c:290 reads this with no symbolic name in regs.h."""
MAC_ERR_ARMED = 1 << 29
"""mac.c:292 -- the condition is only meaningful when this bit is set."""
MAC_ERR_CONDITION = (1 << 7) | (1 << 5)
"""mac.c:292 -- either of these alongside MAC_ERR_ARMED means the MAC has wedged."""

STAT_WORK_INTERVAL_S = 10.0
"""mac.c:351 re-queues mac_work at 10 * HZ, not at MT_CALIBRATE_INTERVAL."""


@dataclass
class MacStats:
    """mt7601u.h struct mt7601u_stats -- every counter mac_work accumulates."""
    rx_stat: list[int] = field(default_factory=lambda: [0] * 6)
    tx_stat: list[int] = field(default_factory=lambda: [0] * 6)
    aggr_stat: list[int] = field(default_factory=lambda: [0] * 2)
    zero_len_del: list[int] = field(default_factory=lambda: [0] * 2)
    aggr_n: list[int] = field(default_factory=lambda: [0] * 32)
    avg_ampdu_len: int = 1


def _spans(stats: MacStats) -> list[tuple[int, int, list[int], int]]:
    """mac.c:305-316 -- the six (base, span, accumulator, first index) rows, in order.

    The C's last row points at `&stats.aggr_n[16]`; a Python slice would copy, so the
    offset is carried instead and both rows accumulate into the one list.
    """
    return [
        (MT_RX_STA_CNT0, 3, stats.rx_stat, 0),
        (MT_TX_STA_CNT0, 3, stats.tx_stat, 0),
        (MT_TX_AGG_STAT, 1, stats.aggr_stat, 0),
        (MT_MPDU_DENSITY_CNT, 1, stats.zero_len_del, 0),
        (MT_TX_AGG_CNT_BASE0, 8, stats.aggr_n, 0),
        (MT_TX_AGG_CNT_BASE1, 8, stats.aggr_n, 16),
    ]


def mac_work(tp: MT7601UTransport, stats: MacStats) -> None:
    """mac.c:301 mt7601u_mac_work -- sweep the counters, then check for a wedged MAC.

    mac.c:320 notes that MCU_RANDOM_READ is slower than reading the registers one by
    one, so the sweep is 24 plain register reads in the C's order. Each read clears the
    counter it reports, which is why the halves are accumulated rather than assigned.
    """
    k = n = total = 0
    for base, span, acc, first in _spans(stats):
        for j in range(span):
            val = tp.rr(base + j * 4)
            acc[first + j * 2] += val & 0xFFFF
            acc[first + j * 2 + 1] += val >> 16
            if base not in (MT_TX_AGG_CNT_BASE0, MT_TX_AGG_CNT_BASE1):
                continue
            # mac.c:336-341 folds the per-length aggregation bins into a mean.
            n += (val >> 16) + (val & 0xFFFF)
            total += (val & 0xFFFF) * (1 + k * 2) + (val >> 16) * (2 + k * 2)
            k += 1

    stats.avg_ampdu_len = ((total + n // 2) // n) if n else 1   # DIV_ROUND_CLOSEST
    check_mac_err(tp)


def check_mac_err(tp: MT7601UTransport) -> None:
    """mac.c:288 mt7601u_check_mac_err -- pulse RESET_CSR when the MAC reports a wedge."""
    val = tp.rr(MAC_ERR_STATUS)
    if not val & MAC_ERR_ARMED or not val & MAC_ERR_CONDITION:
        return
    logger.error("Error: MAC specific condition occurred")
    tp.rmw(MT_MAC_SYS_CTRL, 0, MT_MAC_SYS_CTRL_RESET_CSR)        # mt76_set: mask 0
    time.sleep(0.000010)                                        # udelay(10)
    tp.rmw(MT_MAC_SYS_CTRL, MT_MAC_SYS_CTRL_RESET_CSR, 0)
