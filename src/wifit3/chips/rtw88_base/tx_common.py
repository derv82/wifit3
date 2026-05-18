"""Generic rtw88 TX-descriptor helpers.

The exact tx_pkt_desc layout differs per chip (40 bytes for 8821a/8812a,
48 bytes for 8822b/8822c) but the XOR-checksum encoding is shared, and
the dma_mapping → endpoint-index lookup is shared.

References:
    tx.c:119         fill_txdesc_checksum_common
    usb.c:222        dma_mapping_to_ep
"""

from __future__ import annotations

import logging
import struct

from .registers import (
    RTW_DMA_MAPPING_EXTRA,
    RTW_DMA_MAPPING_HIGH,
    RTW_DMA_MAPPING_LOW,
    RTW_DMA_MAPPING_NORMAL,
)

logger = logging.getLogger(__name__)


def fill_txdesc_checksum(desc: bytearray, *, num_u16_words: int = 16,
                         w7_byte_offset: int = 7 * 4) -> None:
    """XOR `num_u16_words` u16s starting at offset 0, store low 16 bits at W7.

    Mirrors fill_txdesc_checksum_common (tx.c:119). Both 8821a (40-byte desc)
    and 8822b (48-byte desc) checksum the first 32 bytes (16 u16 words) and
    store the result in W7[15:0].
    """
    if len(desc) < num_u16_words * 2:
        raise ValueError(
            f"desc too short for checksum: {len(desc)} bytes "
            f"(need >= {num_u16_words * 2})"
        )
    chksum = 0
    for i in range(num_u16_words):
        chksum ^= struct.unpack_from("<H", desc, i * 2)[0]
    struct.pack_into("<H", desc, w7_byte_offset, chksum & 0xFFFF)


def dma_mapping_to_ep_index(dma_mapping: int) -> int:
    """Map RTW_DMA_MAPPING_* → endpoint index in the parsed bulk-OUT list.

    Mirrors `dma_mapping_to_ep` (usb.c:222). Endpoints are listed in the
    order they appear in the USB interface descriptor.
    """
    table = {
        RTW_DMA_MAPPING_HIGH: 0,
        RTW_DMA_MAPPING_NORMAL: 1,
        RTW_DMA_MAPPING_LOW: 2,
        RTW_DMA_MAPPING_EXTRA: 3,
    }
    return table[dma_mapping]


def pick_bulk_out_ep(out_ep_addrs: list[int], dma_mapping: int) -> int:
    """Resolve dma_mapping → physical endpoint address.

    `out_ep_addrs` is the list of bulk-OUT endpoint addresses in descriptor
    order (e.g. `[0x05, 0x06, 0x08]`).
    """
    idx = dma_mapping_to_ep_index(dma_mapping)
    if idx >= len(out_ep_addrs):
        logger.warning(
            "dma_mapping=%d wants out_ep[%d] but only %d bulk-OUTs exist; "
            "falling back to out_ep[0]=0x%02x",
            dma_mapping, idx, len(out_ep_addrs), out_ep_addrs[0],
        )
        return out_ep_addrs[0]
    return out_ep_addrs[idx]
