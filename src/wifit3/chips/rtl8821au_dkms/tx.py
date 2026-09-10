"""RTL8821AU (DKMS) M6: TX descriptor builder — vendor port.

Ports `rtl8812a_fill_fake_txdesc` [SRC] rtl8812a_xmit.c:265 — the vendor's minimal,
self-contained "send this frame directly" descriptor. That field set is exactly what
monitor-mode injection needs: one non-aggregated, unencrypted frame at a fixed low
rate, HW-assigned sequence number. wifit3 uses it for every injected frame:

  * deauth / fake-auth / assoc — management frames (SEC_TYPE = 0).
  * WEP ARP replay — the captured ARP is ALREADY WEP-encrypted, so it is re-injected
    raw with SEC_TYPE = 0 (no HW re-encryption) on the same path. The vendor's
    `bDataFrame` SEC_TYPE branch never applies here, so one descriptor serves both.

This is NOT the full `update_txdesc` (rate adaptation, aggregation, HW security, RTS).
The 40-byte size, the SET_TX_DESC_*_8812 field bit positions, and the XOR-16 checksum
are identical to the rtl8814au_dkms sibling; the 8812a additionally sets FIRST_SEG and
OWN (the sibling omitted them — both are correct for a single-segment frame, and the
vendor sets them), so they are ported here.

Field bit positions [SRC] include/rtl8812a_xmit.h SET_TX_DESC_*_8812; queue/rate
constants [SRC] include/hal_com.h, include/ieee80211.h.
"""
from __future__ import annotations

import struct

from .constants import TXDESC_SIZE

# Queue-select: injected frames ride the MGMT queue [SRC] hal_com.h QSLT_MGNT.
QSLT_MGNT = 0x12
# Rate-ID groups [SRC] ieee80211.h: RATEID_IDX_B (CCK basic rates) is the safe 2.4 GHz
# default; RATEID_IDX_G selects the OFDM group.
RATEID_IDX_G = 7
RATEID_IDX_B = 8
# DESC hardware rate codes [SRC] hal_com.h (the MRateToHwRate output).
DESC_RATE1M = 0x00
DESC_RATE6M = 0x04



def txdesc_checksum(desc: bytes | bytearray) -> int:
    """[SRC] rtl8812a_cal_txdesc_chksum — XOR of the first 16 LE u16 words (32 bytes).

    The checksum field (byte 28) must already be zero when this runs; the USB HW drops
    a frame whose descriptor checksum is wrong, which is how it recovers from a bulk-out
    error. The span is always the first 32 bytes regardless of descriptor length.
    """
    chk = 0
    for word in struct.unpack_from("<16H", desc, 0):
        chk ^= word
    return chk


def _checksum_from_words(*words: int) -> int:
    chk = 0
    for word in words:
        chk ^= word & 0xFFFF
        chk ^= (word >> 16) & 0xFFFF
    return chk


def build_mgmt_txdesc(pkt_len: int, *, hw_rate: int = DESC_RATE1M,
                      rate_id: int = RATEID_IDX_B, bmc: bool = False,
                      retry_limit: int | None = None) -> bytes:
    """Build the 40-byte TX descriptor for one injected frame.

    [SRC] rtl8812a_fill_fake_txdesc (not-PsPoll, not-data-frame case): FIRST_SEG +
    LAST_SEG, OFFSET = TXDESC_SIZE, PKT_SIZE, QUEUE_SEL = QSLT_MGNT, RATE_ID, HWSEQ_EN
    (HW assigns the sequence number), USE_RATE + TX_RATE (fixed rate, no rate
    adaptation), OWN, SEC_TYPE = 0 (the frame is already final — no HW encryption), then
    the descriptor checksum. ``bmc`` sets the broadcast/multicast bit when addr1 is a
    group address (e.g. a broadcast deauth); the caller derives it from the frame. The
    checksum covers the first 32 bytes with its own field zeroed, so it is computed last
    (HWSEQ_EN at byte 32 sits outside that range and does not affect it).

    ``retry_limit`` (when not None) caps the HW ACK-retry count: it sets RTY_LMT_EN plus
    the 6-bit RTS_DATA_RTY_LMT [SRC] rtl8812a_xmit.h SET_TX_DESC_DATA_RETRY_LIMIT. The
    inject path leaves it None, so the field stays clear (the fake-txdesc's historical
    default, so the HW global retry register applies).
    """
    if not 0 <= pkt_len <= 0xFFFF:
        raise ValueError("pkt_len must fit the 16-bit TX descriptor PKT_SIZE field")
    if retry_limit is not None and not 0 <= retry_limit <= 0x3F:
        raise ValueError("retry_limit must fit the 6-bit RTS_DATA_RTY_LMT field")

    dw0 = pkt_len | (TXDESC_SIZE << 16) | (1 << 26) | (1 << 27) | (1 << 31)
    if bmc:
        dw0 |= 1 << 24                       # BMC (group-addressed frame)
    dw1 = (QSLT_MGNT << 8) | ((rate_id & 0x1F) << 16)
    dw3 = 1 << 8                             # USE_RATE
    dw4 = hw_rate & 0x7F                     # TX_RATE
    if retry_limit is not None:
        dw4 |= 1 << 17                       # RTY_LMT_EN
        dw4 |= (retry_limit & 0x3F) << 18    # RTS_DATA_RTY_LMT (6-bit HW ACK-retry limit)
    chk = _checksum_from_words(dw0, dw1, 0, dw3, dw4, 0, 0, 0)
    return struct.pack("<IIIIIIIIII", dw0, dw1, 0, dw3, dw4, 0, 0, chk, 1 << 15, 0)
