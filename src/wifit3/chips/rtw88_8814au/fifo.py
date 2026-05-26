"""RTL8814AU TRX FIFO + priority-queue init (M2).

Port of rtw_init_trx_cfg (mac.c:1354) for the modern WCPU_3081 path:

    rtw_init_trx_cfg
      ├── txdma_queue_mapping   (REG_TXDMA_PQ_MAP from rqpn; REG_CR=MAC_TRX_ENABLE;
      │                          REG_H2CQ_CSR for 3081; USB RXDMA_ARBBW_EN)
      ├── priority_queue_cfg
      │     ├── rtw_set_trx_fifo_info  (compute rsvd boundary + h2cq addr)
      │     └── __priority_queue_cfg   (FIFOPAGE_INFO + AUTO LLT init + poll)  ← M2 GATE
      └── init_h2c              (H2C ring setup; no-op on 8051)

All FIFO numbers are computed from rtw8814a_hw_spec params (constants.py), not
hardcoded, so the reserved-page invariant the kernel asserts
(rsvd_boundary == rsvd_drv_addr) is reproduced exactly.
"""

from __future__ import annotations

import logging
import time

import usb.core
import usb.util

from wifit3.chips.rtw88_base.registers import (
    BIT_H2CQ_FULL,
    REG_CR,
    REG_H2CQ_CSR,
    REG_TXDMA_PQ_MAP,
    RTW_DMA_MAPPING_HIGH,
    RTW_DMA_MAPPING_LOW,
    RTW_DMA_MAPPING_NORMAL,
)

from . import constants as C
from .transport import RTL8814AUTransport

logger = logging.getLogger(__name__)

# rqpn_table_8814a USB rows (rtw8814a.c:2085), (vo, vi, be, bk, mg, hi).
# Indexed by bulkout_num: [2]=2-out, [3]=3-out, [4]=4-out.
_H, _N, _L = RTW_DMA_MAPPING_HIGH, RTW_DMA_MAPPING_NORMAL, RTW_DMA_MAPPING_LOW
RQPN_USB_8814A = {
    2: {"vo": _H, "vi": _H, "be": _N, "bk": _N, "mg": _H, "hi": _H},
    3: {"vo": _H, "vi": _N, "be": _L, "bk": _L, "mg": _H, "hi": _H},
    4: {"vo": _H, "vi": _N, "be": _L, "bk": _L, "mg": _H, "hi": _H},
}

# page_table_8814a USB rows (rtw8814a.c:2124) — ALL identical: hq/nq/lq/exq=32, gapq=0.
PAGE_TABLE_USB_8814A = {"hq": 32, "nq": 32, "lq": 32, "exq": 32, "gapq": 0}


def count_bulk_out_eps(dev: usb.core.Device) -> int:
    """Count bulk-OUT endpoints in the active configuration (kernel out_ep count)."""
    cfg = dev.get_active_configuration()
    n = 0
    for intf in cfg:
        for ep in intf:
            is_out = usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_OUT
            is_bulk = usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK
            if is_out and is_bulk:
                n += 1
    return n


def _poll8_clear(transport: RTL8814AUTransport, addr: int, mask: int,
                 attempts: int = 200, interval_s: float = 0.001) -> bool:
    for _ in range(attempts):
        if (transport.read8(addr) & mask) == 0:
            return True
        time.sleep(interval_s)
    return False


def rtw_set_trx_fifo_info(transport: RTL8814AUTransport) -> dict:
    """mac.c:1138..1189 (3081 path). Returns {rsvd_boundary, rsvd_h2cq_addr}.

    Reproduces the reserved-page walk and asserts the kernel invariant
    rsvd_boundary == rsvd_drv_addr.
    """
    txff_pg_num = C.TXFF_SIZE // C.PAGE_SIZE
    rsvd_pg_num = (
        C.RSVD_DRV_PG_NUM
        + C.RSVD_PG_H2C_EXTRAINFO_NUM
        + C.RSVD_PG_H2C_STATICINFO_NUM
        + C.RSVD_PG_H2CQ_NUM
        + C.RSVD_PG_CPU_INSTRUCTION_NUM
        + C.RSVD_PG_FW_TXBUF_NUM
        + C.CSI_BUF_PG_NUM
    )
    if rsvd_pg_num > txff_pg_num:
        raise IOError("rsvd_pg_num exceeds txff_pg_num")

    acq_pg_num = txff_pg_num - rsvd_pg_num
    rsvd_boundary = txff_pg_num - rsvd_pg_num

    # Reserved-page address walk (3081), top of FIFO downward.
    cur = txff_pg_num
    cur -= C.CSI_BUF_PG_NUM                 # rsvd_csibuf_addr
    cur -= C.RSVD_PG_FW_TXBUF_NUM           # rsvd_fw_txbuf_addr
    cur -= C.RSVD_PG_CPU_INSTRUCTION_NUM    # rsvd_cpu_instr_addr
    cur -= C.RSVD_PG_H2CQ_NUM
    rsvd_h2cq_addr = cur
    cur -= C.RSVD_PG_H2C_STATICINFO_NUM     # rsvd_h2c_sta_info_addr
    cur -= C.RSVD_PG_H2C_EXTRAINFO_NUM      # rsvd_h2c_info_addr
    cur -= C.RSVD_DRV_PG_NUM
    rsvd_drv_addr = cur

    if rsvd_boundary != rsvd_drv_addr:
        raise IOError(
            f"wrong rsvd driver address: boundary={rsvd_boundary} "
            f"drv_addr={rsvd_drv_addr}"
        )

    return {
        "rsvd_boundary": rsvd_boundary,
        "rsvd_h2cq_addr": rsvd_h2cq_addr,
        "acq_pg_num": acq_pg_num,
    }


def txdma_queue_mapping(transport: RTL8814AUTransport, bulkout_num: int) -> None:
    """mac.c:1057..1135 (USB, 3081)."""
    if bulkout_num not in RQPN_USB_8814A:
        raise ValueError(f"unsupported bulkout_num={bulkout_num}")
    rqpn = RQPN_USB_8814A[bulkout_num]

    txdma_pq_map = 0
    for q, shift in C.TXDMA_MAP_SHIFTS.items():
        txdma_pq_map |= (rqpn[q] & C.TXDMA_MAP_MASK) << shift
    transport.write16(REG_TXDMA_PQ_MAP, txdma_pq_map)

    transport.write8(REG_CR, 0)
    transport.write8(REG_CR, C.MAC_TRX_ENABLE)
    # 3081: prime the H2C queue CSR.
    transport.write32(REG_H2CQ_CSR, BIT_H2CQ_FULL)
    # USB: enable RXDMA arbiter bandwidth.
    transport.write8_set(REG_TXDMA_PQ_MAP, C.BIT_RXDMA_ARBBW_EN)


def priority_queue_cfg(transport: RTL8814AUTransport, fifo: dict) -> None:
    """mac.c:1192..1230 `__priority_queue_cfg` (modern path). Raises on LLT fail."""
    pg = PAGE_TABLE_USB_8814A
    pubq_num = (
        fifo["acq_pg_num"]
        - pg["hq"] - pg["lq"] - pg["nq"] - pg["exq"] - pg["gapq"]
    )
    rsvd_boundary = fifo["rsvd_boundary"]

    transport.write16(C.REG_FIFOPAGE_INFO_1, pg["hq"])
    transport.write16(C.REG_FIFOPAGE_INFO_2, pg["lq"])
    transport.write16(C.REG_FIFOPAGE_INFO_3, pg["nq"])
    transport.write16(C.REG_FIFOPAGE_INFO_4, pg["exq"])
    transport.write16(C.REG_FIFOPAGE_INFO_5, pubq_num)

    transport.write32_set(C.REG_RQPN_CTRL_2, C.BIT_LD_RQPN)

    transport.write16(C.REG_FIFOPAGE_CTRL_2, rsvd_boundary)
    transport.write8_set(C.REG_FWHW_TXQ_CTRL + 2, (C.BIT_EN_WR_FREE_TAIL >> 16) & 0xFF)

    transport.write16(C.REG_BCNQ_BDNY_V1, rsvd_boundary)
    transport.write16(C.REG_FIFOPAGE_CTRL_2 + 2, rsvd_boundary)
    transport.write16(C.REG_BCNQ1_BDNY_V1, rsvd_boundary)
    transport.write32(C.REG_RXFF_BNDY, C.RXFF_SIZE - C.C2H_PKT_BUF - 1)

    # USB-specific: BLK_DESC_NUM + agg desc + offset check.
    cur = transport.read8(C.REG_AUTO_LLT_V1)
    cur = (cur & ~C.BIT_MASK_BLK_DESC_NUM) | (
        (C.USB_TX_AGG_DESC_NUM << 4) & C.BIT_MASK_BLK_DESC_NUM
    )
    transport.write8(C.REG_AUTO_LLT_V1, cur & 0xFF)
    transport.write8(C.REG_AUTO_LLT_V1 + 3, C.USB_TX_AGG_DESC_NUM)
    transport.write8_set(C.REG_TXDMA_OFFSET_CHK + 1, 1 << 1)

    # Trigger auto LLT init + poll for completion — the M2 gate.
    transport.write8_set(C.REG_AUTO_LLT_V1, C.BIT_AUTO_INIT_LLT_V1)
    if not _poll8_clear(transport, C.REG_AUTO_LLT_V1, C.BIT_AUTO_INIT_LLT_V1):
        raise IOError("BIT_AUTO_INIT_LLT_V1 didn't clear — LLT init failed")

    transport.write8(REG_CR + 3, 0)


def init_h2c(transport: RTL8814AUTransport, fifo: dict) -> None:
    """mac.c:1301..1352 (3081). Sets up the H2C ring; verifies free == size."""
    h2cq_addr = fifo["rsvd_h2cq_addr"] << C.TX_PAGE_SIZE_SHIFT
    h2cq_size = C.RSVD_PG_H2CQ_NUM << C.TX_PAGE_SIZE_SHIFT

    v = (transport.read32(C.REG_H2C_HEAD) & 0xFFFC0000) | h2cq_addr
    transport.write32(C.REG_H2C_HEAD, v)

    v = (transport.read32(C.REG_H2C_READ_ADDR) & 0xFFFC0000) | h2cq_addr
    transport.write32(C.REG_H2C_READ_ADDR, v)

    v = transport.read32(C.REG_H2C_TAIL) & 0xFFFC0000
    v |= (h2cq_addr + h2cq_size)
    transport.write32(C.REG_H2C_TAIL, v)

    v8 = (transport.read8(C.REG_H2C_INFO) & 0xFC) | 0x01
    transport.write8(C.REG_H2C_INFO, v8)
    v8 = (transport.read8(C.REG_H2C_INFO) & 0xFB) | 0x04
    transport.write8(C.REG_H2C_INFO, v8)

    v8 = (transport.read8(C.REG_TXDMA_OFFSET_CHK + 1) & 0x7F) | 0x80
    transport.write8(C.REG_TXDMA_OFFSET_CHK + 1, v8)

    wp = transport.read32(C.REG_H2C_PKT_WRITEADDR) & 0x3FFFF
    rp = transport.read32(C.REG_H2C_PKT_READADDR) & 0x3FFFF
    h2cq_free = h2cq_size - (wp - rp) if wp >= rp else rp - wp
    if h2cq_size != h2cq_free:
        raise IOError(f"H2C queue mismatch: size={h2cq_size} free={h2cq_free}")


def rtw_init_trx_cfg(transport: RTL8814AUTransport, bulkout_num: int) -> dict:
    """mac.c:1354..1371. Returns the computed fifo dict."""
    fifo = rtw_set_trx_fifo_info(transport)
    txdma_queue_mapping(transport, bulkout_num)
    priority_queue_cfg(transport, fifo)
    init_h2c(transport, fifo)
    return fifo
