"""RTL8822BU TX path: 48-byte tx_pkt_desc builder + bulk-OUT writer.

Mirrors 8821au's tx.py but with chip-specific deltas:
  - TX_PKT_DESC_SZ = 48 (8822b) vs 40 (8821a)
  - `old_datarate_fb_limit = False` → no DATARATE_FB_LIMIT field set
  - 3 bulk-OUT pipes vs 4 → different rqpn mapping

The TX desc bit layout itself is shared (tx.h:25..60) — see
:mod:`wifit3.chips.rtw88_base.tx_common.fill_txdesc_checksum` for the
shared XOR-checksum encoding.
"""

from __future__ import annotations

import logging
import struct

import usb.core

from wifit3.chips.rtw88_base.registers import (
    DESC_RATE1M,
    DESC_RATE6M,
    RTW_DMA_MAPPING_HIGH,
    RTW_DMA_MAPPING_NORMAL,
    TX_DESC_QSEL_BEACON,
    TX_DESC_QSEL_H2C,
    TX_DESC_QSEL_HIGH,
    TX_DESC_QSEL_MGMT,
)
from wifit3.chips.rtw88_base.tx_common import (
    fill_txdesc_checksum,
    pick_bulk_out_ep as _shared_pick_bulk_out_ep,
)

from .constants import TX_PKT_DESC_SZ

logger = logging.getLogger(__name__)


# Rate-ID enum (main.h:237)
RTW_RATEID_B_20M = 8       # 2.4 GHz CCK
RTW_RATEID_G = 7           # 2.4 GHz OFDM


def _is_multicast_or_broadcast(addr: bytes) -> bool:
    """802.11 broadcast/multicast tell: addr1[0] has the I/G bit set."""
    return bool(addr[0] & 0x01)


def build_tx_desc_mgmt(mpdu: bytes, *, band_is_2g: bool = True) -> bytes:
    """Build a 48-byte tx_pkt_desc for an MGMT-queue injection.

    Mirrors `rtw_tx_fill_tx_desc` (tx.c:35) for the MGMT path. Differences
    from 8821au:
      - DESC size 48
      - `old_datarate_fb_limit = false` (rtw8822b.c:2547) → no FB_LIMIT
      - 16-u16 checksum written at W7 (offset 28) — same as 8821a
    """
    if len(mpdu) < 10:
        raise ValueError(f"MPDU too short ({len(mpdu)} bytes) for mgmt injection")

    pkt_size = len(mpdu) & 0xFFFF
    bmc = _is_multicast_or_broadcast(mpdu[4:10])

    if band_is_2g:
        rate = DESC_RATE1M
        rate_id = RTW_RATEID_B_20M
    else:
        rate = DESC_RATE6M
        rate_id = RTW_RATEID_G

    qsel = TX_DESC_QSEL_MGMT

    w0 = (
        (pkt_size & 0xFFFF)                # W0[15:0]   TXPKTSIZE
        | ((TX_PKT_DESC_SZ & 0xFF) << 16)  # W0[23:16]  OFFSET = 48
        | ((1 if bmc else 0) << 24)        # W0[24]     BMC
        | (1 << 26)                        # W0[26]     LS
        | (1 << 31)                        # W0[31]     DISQSELSEQ
    )
    w1 = (
        0                                  # W1[7:0]    MACID = 0
        | ((qsel & 0x1F) << 8)             # W1[12:8]   QSEL
        | ((rate_id & 0x1F) << 16)         # W1[20:16]  RATE_ID
        | (0 << 22)                        # W1[23:22]  SEC_TYPE
        | (0 << 24)                        # W1[28:24]  PKT_OFFSET
    )
    w2 = 0
    w3 = (
        (1 << 8)                           # W3[8]      USE_RATE
        | (1 << 10)                        # W3[10]     DISDATAFB
    )
    w4 = rate & 0x7F                       # W4[6:0]    DATARATE
    # 8822b has old_datarate_fb_limit=False (rtw8822b.c:2547) — no FB_LIMIT.
    w5 = 0
    w6 = 0
    w7 = 0                                 # checksum filled below
    w8 = 1 << 15                           # W8[15]     EN_HWSEQ
    w9 = 0
    w10 = 0
    w11 = 0

    desc = bytearray(struct.pack("<12I", w0, w1, w2, w3, w4, w5,
                                 w6, w7, w8, w9, w10, w11))
    fill_txdesc_checksum(desc)
    return bytes(desc)


def pick_bulk_out_ep(out_ep_addrs: list[int], queue: int = TX_DESC_QSEL_MGMT) -> int:
    """Map TX queue → physical bulk-OUT endpoint address.

    For 8822b on USB with 3 bulk-OUTs (rqpn_table_8822b[3]):
      dma_map_hi = HIGH    (BEACON, H2C, HIGH)
      dma_map_mg = HIGH    (MGMT)
      dma_map_be/bk = NORMAL
      dma_map_vi/vo = LOW

    For T3U the descriptor order is [0x05, 0x06, 0x08] →
    HIGH → out_ep[0] = 0x05.
    """
    mapping = {
        TX_DESC_QSEL_BEACON: RTW_DMA_MAPPING_HIGH,
        TX_DESC_QSEL_HIGH:   RTW_DMA_MAPPING_HIGH,
        TX_DESC_QSEL_MGMT:   RTW_DMA_MAPPING_HIGH,
        TX_DESC_QSEL_H2C:    RTW_DMA_MAPPING_HIGH,
    }
    dma = mapping.get(queue, RTW_DMA_MAPPING_NORMAL)
    return _shared_pick_bulk_out_ep(out_ep_addrs, dma)


def write_bulk(dev: usb.core.Device, ep: int, payload: bytes, *,
               timeout_ms: int = 200) -> int:
    sent = dev.write(ep, payload, timeout_ms)
    return int(sent)


def build_deauth_frame(ap_mac: bytes, client_mac: bytes,
                       *, reason: int = 7) -> bytes:
    fc = b"\xC0\x00"
    dur = b"\x00\x00"
    seq = b"\x00\x00"
    reason_bytes = struct.pack("<H", reason)
    return fc + dur + client_mac + ap_mac + ap_mac + seq + reason_bytes
