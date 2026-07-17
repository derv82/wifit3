"""RTL8814AU TX path: 40-byte tx_pkt_desc builder + bulk-OUT writer.

The tx_pkt_desc bit layout is the rtw88-family-shared one (tx.h); 8814a's
`tx_pkt_desc_sz` is 40 (10 u32 words, w0..w9) — same size as 8812a/8821a. The
one chip-specific bit vs the 8812a 40-byte builder: 8814a has
`old_datarate_fb_limit = false` (rtw8814a_hw_spec), so we do NOT set the W4
DATARATE_FB_LIMIT field (same as the 8822b).

MGMT injection goes out the HIGH bulk-OUT lane (rqpn_table_8814a[3]: mg=HIGH),
which on the AWUS1900 is out_ep[0] = 0x02 — the same lane used for FW upload.
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
    return bool(addr[0] & 0x01)


def build_tx_desc_mgmt(mpdu: bytes, *, band_is_2g: bool = True,
                       retry_limit: int | None = None) -> bytes:
    """Build a 40-byte tx_pkt_desc for an MGMT-queue injection.

    Mirrors rtw_tx_fill_tx_desc for the MGMT path. 8814a packs 10 u32 (40 B)
    and, unlike the 8812a, sets NO DATARATE_FB_LIMIT (old_datarate_fb_limit=false).

    ``retry_limit`` (when not None) sets the per-frame HW ACK-retry limit in W4: the
    Realtek 40-byte desc carries RTY_LMT_EN (W4[17]) + DATA_RTY_LMT (W4[23:18], 6-bit),
    the same bits as the rtl88xxau/rtl8xxxu siblings. The rtw88 reference leaves them 0
    and relies on the global REG_RETRY_LIMIT instead; the inject path opts in per frame so
    each injected frame carries its own limit. None (the FW/HW-test callers) keeps them 0.
    """
    if len(mpdu) < 10:
        raise ValueError(f"MPDU too short ({len(mpdu)} bytes) for mgmt injection")

    pkt_size = len(mpdu) & 0xFFFF
    bmc = _is_multicast_or_broadcast(mpdu[4:10])

    if band_is_2g:
        rate, rate_id = DESC_RATE1M, RTW_RATEID_B_20M
    else:
        rate, rate_id = DESC_RATE6M, RTW_RATEID_G

    qsel = TX_DESC_QSEL_MGMT

    w0 = (
        (pkt_size & 0xFFFF)                # W0[15:0]   TXPKTSIZE
        | ((TX_PKT_DESC_SZ & 0xFF) << 16)  # W0[23:16]  OFFSET = 40
        | ((1 if bmc else 0) << 24)        # W0[24]     BMC
        | (1 << 26)                        # W0[26]     LS
        | (1 << 31)                        # W0[31]     DISQSELSEQ
    )
    w1 = (
        ((qsel & 0x1F) << 8)               # W1[12:8]   QSEL
        | ((rate_id & 0x1F) << 16)         # W1[20:16]  RATE_ID
    )
    w2 = 0
    w3 = (1 << 8) | (1 << 10)              # W3[8] USE_RATE, W3[10] DISDATAFB
    w4 = rate & 0x7F                       # W4[6:0] DATARATE (no FB_LIMIT)
    if retry_limit is not None:
        w4 |= 1 << 17                      # W4[17] RTY_LMT_EN
        w4 |= (retry_limit & 0x3F) << 18   # W4[23:18] DATA_RTY_LMT (6-bit)
    w5 = w6 = w7 = 0                        # w7 = checksum, filled below
    w8 = 1 << 15                           # W8[15] EN_HWSEQ
    w9 = 0

    desc = bytearray(struct.pack("<10I", w0, w1, w2, w3, w4, w5, w6, w7, w8, w9))
    fill_txdesc_checksum(desc)
    return bytes(desc)


def pick_bulk_out_ep(out_ep_addrs: list[int], queue: int = TX_DESC_QSEL_MGMT) -> int:
    """Map TX queue → bulk-OUT endpoint. rqpn_table_8814a[3]: mg/hi/beacon=HIGH."""
    mapping = {
        TX_DESC_QSEL_BEACON: RTW_DMA_MAPPING_HIGH,
        TX_DESC_QSEL_HIGH: RTW_DMA_MAPPING_HIGH,
        TX_DESC_QSEL_MGMT: RTW_DMA_MAPPING_HIGH,
        TX_DESC_QSEL_H2C: RTW_DMA_MAPPING_HIGH,
    }
    dma = mapping.get(queue, RTW_DMA_MAPPING_NORMAL)
    return _shared_pick_bulk_out_ep(out_ep_addrs, dma)


def write_bulk(dev: usb.core.Device, ep: int, payload: bytes, *,
               timeout_ms: int = 200) -> int:
    return int(dev.write(ep, payload, timeout_ms))


def build_deauth_frame(ap_mac: bytes, client_mac: bytes, *, reason: int = 7) -> bytes:
    """Deauth MPDU: addr1=client (DA), addr2=ap (SA), addr3=ap (BSSID)."""
    fc = b"\xC0\x00"          # type=mgmt, subtype=deauth
    dur = b"\x00\x00"
    seq = b"\x00\x00"
    return fc + dur + client_mac + ap_mac + ap_mac + seq + struct.pack("<H", reason)
