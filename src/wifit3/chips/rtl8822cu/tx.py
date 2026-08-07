"""RTL8822CU management-frame TX over the rtw88 48-byte descriptor."""
from __future__ import annotations

import struct
import usb.core

from wifit3.chips.rtw88_base.registers import (
    DESC_RATE1M, DESC_RATE6M, RTW_DMA_MAPPING_HIGH, RTW_DMA_MAPPING_NORMAL,
    TX_DESC_QSEL_BEACON, TX_DESC_QSEL_H2C, TX_DESC_QSEL_HIGH, TX_DESC_QSEL_MGMT,
)
from wifit3.chips.rtw88_base.tx_common import fill_txdesc_checksum, pick_bulk_out_ep as _pick

TX_PKT_DESC_SZ = 48
RTW_RATEID_B_20M = 8
RTW_RATEID_G = 7

def build_tx_desc_mgmt(mpdu: bytes, *, band_is_2g: bool = True,
                       retry_limit: int | None = None) -> bytes:
    if len(mpdu) < 10:
        raise ValueError(f"MPDU too short ({len(mpdu)} bytes)")
    bmc = bool(mpdu[4] & 1)
    rate = DESC_RATE1M if band_is_2g else DESC_RATE6M
    rate_id = RTW_RATEID_B_20M if band_is_2g else RTW_RATEID_G
    w0 = len(mpdu) | (TX_PKT_DESC_SZ << 16) | (int(bmc) << 24) | (1 << 26) | (1 << 31)
    w1 = (TX_DESC_QSEL_MGMT << 8) | (rate_id << 16)
    w3 = (1 << 8) | (1 << 10)
    w4 = rate & 0x7F
    if retry_limit is not None:
        w4 |= (1 << 17) | ((retry_limit & 0x3F) << 18)
    desc = bytearray(struct.pack("<12I", w0, w1, 0, w3, w4, 0, 0, 0, 1 << 15, 0, 0, 0))
    fill_txdesc_checksum(desc)
    return bytes(desc)

def pick_bulk_out_ep(out_ep_addrs: list[int], queue: int = TX_DESC_QSEL_MGMT) -> int:
    dma = RTW_DMA_MAPPING_HIGH if queue in (TX_DESC_QSEL_BEACON, TX_DESC_QSEL_HIGH,
                                             TX_DESC_QSEL_MGMT, TX_DESC_QSEL_H2C) else RTW_DMA_MAPPING_NORMAL
    return _pick(out_ep_addrs, dma)

def write_bulk(dev: usb.core.Device, ep: int, payload: bytes, *, timeout_ms: int = 200) -> int:
    return int(dev.write(ep, payload, timeout_ms))
