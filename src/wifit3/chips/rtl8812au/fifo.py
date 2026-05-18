"""TX/RX FIFO partition computation for RTL8812AU.

Port of `rtw_set_trx_fifo_info` (mac.c:1138) for the 8051 wlan-CPU path.

8812A constants (rtw8812a_hw_spec, rtw8812a.c:1038):
    txff_size        = 131072
    page_size        = 512
    rsvd_drv_pg_num  = 9
    txff_pg_num      = 131072 / 512    = 256
    rsvd_pg_num      = rsvd_drv_pg_num = 9    (8051 path — no H2C/CSI extras)
    acq_pg_num       = 256 - 9         = 247
    rsvd_boundary    = acq_pg_num      = 247

Pure-Python computation — no hardware I/O.
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
    """Compute FifoConf for 8812A (8051 wlan-CPU)."""
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
