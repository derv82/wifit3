"""TX/RX FIFO partition computation for RTL8821AU.

Port of `rtw_set_trx_fifo_info` (mac.c:1138). Pure-Python computation — no
hardware I/O. Returns a :class:`FifoConf` consumed by the queue/page init
helpers in :mod:`.mac`.

For the 8051 wlan-CPU path (which 8821a uses), the kernel's only RSVD
allocation is `rsvd_drv_pg_num`; modern (3081) chips reserve extra pages
for H2C/CPU-instruction/CSI/etc. — none of that applies here.

Derived constants for 8821A (txff_size=65536, page_size=256,
rsvd_drv_pg_num=8):

    txff_pg_num   = txff_size / page_size      = 256
    rsvd_pg_num   = rsvd_drv_pg_num            = 8
    acq_pg_num    = txff_pg_num - rsvd_pg_num  = 248
    rsvd_boundary = acq_pg_num                 = 248
"""
from __future__ import annotations

from dataclasses import dataclass

from .constants import PAGE_SIZE, RSVD_DRV_PG_NUM, TXFF_SIZE


@dataclass(frozen=True)
class FifoConf:
    txff_pg_num: int
    rsvd_drv_pg_num: int
    rsvd_pg_num: int
    acq_pg_num: int
    rsvd_boundary: int


def set_trx_fifo_info() -> FifoConf:
    """Compute FifoConf for 8821A (8051 wlan-CPU)."""
    txff_pg_num = TXFF_SIZE // PAGE_SIZE
    rsvd_drv_pg_num = RSVD_DRV_PG_NUM
    rsvd_pg_num = rsvd_drv_pg_num   # 8051 path
    if rsvd_pg_num > txff_pg_num:
        raise ValueError("rsvd_pg_num exceeds txff_pg_num")
    acq_pg_num = txff_pg_num - rsvd_pg_num
    rsvd_boundary = acq_pg_num
    return FifoConf(
        txff_pg_num=txff_pg_num,
        rsvd_drv_pg_num=rsvd_drv_pg_num,
        rsvd_pg_num=rsvd_pg_num,
        acq_pg_num=acq_pg_num,
        rsvd_boundary=rsvd_boundary,
    )
