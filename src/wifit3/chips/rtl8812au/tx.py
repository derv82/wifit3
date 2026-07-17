"""RTL8812AU TX path: 40-byte tx_pkt_desc builder + bulk-OUT writer.

Port of `rtw_tx_fill_tx_desc` (tx.c:35) for the 8812A path. 8812A and
8821A share the same tx_pkt_desc_sz (40), checksum encoding (XOR of 16
u16 words into W7[15:0]), and `old_datarate_fb_limit=True` semantics
(W4 DATARATE_FB_LIMIT = 0x1F).

The only delta vs 8821au is the bulk-OUT endpoint mapping:

  8821au (2 bulkout, USB2 lane table):
      MGMT → NORMAL → out_ep[1]
  8812au (3 bulkout per `rqpn_table_8812a[3]`):
      dma_map_{hi=HIGH, mg=NORMAL, bk=LOW, be=LOW, vi=HIGH, vo=HIGH}
      MGMT → NORMAL → out_ep[1]
      HIGH/BEACON → HIGH → out_ep[0]

For our AWUS036ACH (bulk_out = [0x02, 0x03, 0x04]), MGMT lands on 0x03.

References:
    tx.h:25..82   bit layouts
    tx.c:35       rtw_tx_fill_tx_desc
    tx.c:319      rtw_tx_mgmt_pkt_info_update
    rtw8812a.c:1083  old_datarate_fb_limit = true
"""
from __future__ import annotations

import logging
import struct
from typing import List

import usb.core

from wifit3.chips.rtw88_base.tx_common import (
    fill_txdesc_checksum,
    pick_bulk_out_ep as _shared_pick_bulk_out_ep,
)

from .constants import (
    DESC_RATE1M,
    RTW_DMA_MAPPING_HIGH,
    RTW_DMA_MAPPING_NORMAL,
)

logger = logging.getLogger(__name__)

TX_PKT_DESC_SZ = 40       # 8812A chip param (rtw8812a.c:1043)
TX_CHECKSUM_WORDS = 16    # XOR'd u16s covering bytes 0..31

# Queue-select values (tx.h:62)
TX_DESC_QSEL_BEACON = 16
TX_DESC_QSEL_HIGH = 17
TX_DESC_QSEL_MGMT = 18
TX_DESC_QSEL_H2C = 19

# Rate-ID enum (main.h:237)
RTW_RATEID_B_20M = 8
RTW_RATEID_G = 7


def _is_multicast_or_broadcast(addr: bytes) -> bool:
    """802.11 broadcast/multicast tell: addr1[0] has the I/G bit set."""
    return bool(addr[0] & 0x01)


def build_tx_desc_mgmt(mpdu: bytes, *, band_is_2g: bool = True,
                       retry_limit: int | None = None) -> bytes:
    """Build a 40-byte tx_pkt_desc for an MGMT-queue injection.

    Returns the descriptor bytes ready to prepend to `mpdu` for bulk-OUT.
    Includes the W7 checksum (XOR of bytes 0..31 as 16 u16s).

    ``retry_limit`` (when not None) caps the HW ACK-retry count via RTY_LMT_EN (W4[17]) plus
    the 6-bit DATA_RT_LMT (W4[18:24]) — the same 8812a descriptor bits the DKMS sibling
    byte-verifies against the recorded aireplay TX. The inject path passes
    ``DEFAULT_HW_ACK_RETRIES``; left None the field stays clear (the HW global retry applies).
    """
    if len(mpdu) < 10:
        raise ValueError(f"MPDU too short ({len(mpdu)} bytes) for mgmt injection")

    pkt_size = len(mpdu) & 0xFFFF
    bmc = _is_multicast_or_broadcast(mpdu[4:10])   # addr1 = bytes 4..10

    if band_is_2g:
        rate = DESC_RATE1M
        rate_id = RTW_RATEID_B_20M
    else:
        rate = 4  # DESC_RATE6M
        rate_id = RTW_RATEID_G

    qsel = TX_DESC_QSEL_MGMT

    w0 = (
        (pkt_size & 0xFFFF)                # W0[15:0]   TXPKTSIZE
        | ((TX_PKT_DESC_SZ & 0xFF) << 16)  # W0[23:16]  OFFSET
        | ((1 if bmc else 0) << 24)        # W0[24]     BMC
        | (1 << 26)                        # W0[26]     LS (last segment)
        | (1 << 31)                        # W0[31]     DISQSELSEQ
    )

    w1 = (
        0                                  # W1[7:0]    MACID = 0
        | ((qsel & 0x1F) << 8)             # W1[12:8]   QSEL
        | ((rate_id & 0x1F) << 16)         # W1[20:16]  RATE_ID
    )

    w2 = 0
    w3 = (
        (1 << 8)                           # W3[8]      USE_RATE
        | (1 << 10)                        # W3[10]     DISDATAFB
    )

    # 8812a has old_datarate_fb_limit=True (rtw8812a.c:1083) — same as 8821a.
    w4 = (rate & 0x7F)                     # W4[6:0]    DATARATE
    w4 |= (0x1F & 0x1F) << 8               # W4[12:8]   DATARATE_FB_LIMIT = 0x1F
    if retry_limit is not None:
        w4 |= (1 << 17)                    # W4[17]     RTY_LMT_EN
        w4 |= (retry_limit & 0x3F) << 18   # W4[18:24]  DATA_RT_LMT (HW ACK-retry cap)

    w5 = 0
    w6 = 0
    w7 = 0                                 # filled by checksum below
    w8 = (1 << 15)                         # W8[15]     EN_HWSEQ
    w9 = 0

    desc = bytearray(struct.pack("<10I", w0, w1, w2, w3, w4, w5, w6, w7, w8, w9))
    fill_txdesc_checksum(desc, num_u16_words=TX_CHECKSUM_WORDS, w7_byte_offset=7 * 4)
    return bytes(desc)


def pick_bulk_out_ep(out_ep_addrs: List[int], queue: int = TX_DESC_QSEL_MGMT) -> int:
    """Map a logical queue → physical bulk-OUT endpoint address (8812A 3-bulkout).

    Per `rqpn_table_8812a[3]`:
        dma_map_{hi, mg, bk, be, vi, vo} = {HIGH, NORMAL, LOW, LOW, HIGH, HIGH}

    AWUS036ACH bulk_out = [0x02, 0x03, 0x04] (descriptor order):
        MGMT  → NORMAL → out_ep[1] = 0x03
        HIGH  → HIGH   → out_ep[0] = 0x02
        BEACON→ HIGH   → out_ep[0] = 0x02
    """
    queue_to_dma = {
        TX_DESC_QSEL_BEACON: RTW_DMA_MAPPING_HIGH,
        TX_DESC_QSEL_HIGH:   RTW_DMA_MAPPING_HIGH,
        TX_DESC_QSEL_MGMT:   RTW_DMA_MAPPING_NORMAL,
        TX_DESC_QSEL_H2C:    RTW_DMA_MAPPING_HIGH,
    }
    dma = queue_to_dma.get(queue, RTW_DMA_MAPPING_NORMAL)
    return _shared_pick_bulk_out_ep(out_ep_addrs, dma)


def write_bulk(dev: usb.core.Device, ep: int, payload: bytes, *,
               timeout_ms: int = 200) -> int:
    """Single bulk-OUT write. Returns bytes written."""
    return int(dev.write(ep, payload, timeout_ms))


def build_deauth_frame(ap_mac: bytes, client_mac: bytes,
                       *, reason: int = 7) -> bytes:
    """Construct a raw 802.11 deauth frame from `ap_mac` to `client_mac`.

    Frame Control: 0xC0 0x00 (subtype=12 deauth, type=mgmt). Duration=0,
    seq=0 (HW fills via EN_HWSEQ). Reason 7 = "Class 3 frame from
    nonassociated STA". For broadcast deauth pass `client_mac =
    b'\\xff\\xff\\xff\\xff\\xff\\xff'`.

    Addr1 = destination (client), Addr2 = source (spoofed = AP),
    Addr3 = BSSID (AP). For client-deauth attacks the AP is spoofed.
    """
    fc = b"\xC0\x00"
    dur = b"\x00\x00"
    seq = b"\x00\x00"
    reason_bytes = struct.pack("<H", reason)
    return fc + dur + client_mac + ap_mac + ap_mac + seq + reason_bytes
