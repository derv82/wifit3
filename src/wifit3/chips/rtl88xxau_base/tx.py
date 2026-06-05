"""RTL88xxAU TX descriptor builder — the vendor "send this frame directly" descriptor.

Ports ``rtl8812a_fill_fake_txdesc`` [SRC] rtl8812a_xmit.c:265 — the vendor's minimal,
self-contained injection descriptor, shared by the whole 88xxA family (it is the 8812a
function; the 8821a reuses it). That field set is exactly what monitor-mode injection
needs: one non-aggregated, unencrypted frame at a fixed low rate, HW-assigned sequence
number. wifit3 uses it for every injected frame:

  * deauth / fake-auth / assoc — management frames (SEC_TYPE = 0).
  * WEP ARP replay — the captured ARP is ALREADY WEP-encrypted, so it is re-injected
    raw with SEC_TYPE = 0 (no HW re-encryption) on the same path. The vendor's
    ``bDataFrame`` SEC_TYPE branch never applies here, so one descriptor serves both.

This is NOT the full ``update_txdesc`` (rate adaptation, aggregation, HW security, RTS).
Field bit positions [SRC] include/rtl8812a_xmit.h SET_TX_DESC_*_8812; queue/rate
constants [SRC] include/hal_com.h, include/ieee80211.h.
"""
from __future__ import annotations

from .registers import TXDESC_SIZE

# Queue-select: injected frames ride the MGMT queue [SRC] hal_com.h QSLT_MGNT.
QSLT_MGNT = 0x12
# Rate-ID groups [SRC] ieee80211.h: RATEID_IDX_B (CCK basic rates) is the safe 2.4 GHz
# default; RATEID_IDX_G selects the OFDM group.
RATEID_IDX_G = 7
RATEID_IDX_B = 8
# DESC hardware rate codes [SRC] hal_com.h (the MRateToHwRate output).
DESC_RATE1M = 0x00
DESC_RATE6M = 0x04


def _set_bits(desc: bytearray, byte_off: int, bit_start: int, bit_len: int,
              value: int) -> None:
    """[SRC] SET_BITS_TO_LE_4BYTE — write ``value`` into [bit_start +: bit_len] of the
    little-endian u32 at ``byte_off``."""
    mask = ((1 << bit_len) - 1) << bit_start
    word = int.from_bytes(desc[byte_off:byte_off + 4], "little")
    word = (word & ~mask) | ((value << bit_start) & mask)
    desc[byte_off:byte_off + 4] = (word & 0xFFFFFFFF).to_bytes(4, "little")


def txdesc_checksum(desc: bytearray) -> int:
    """[SRC] rtl8812a_cal_txdesc_chksum — XOR of the first 16 LE u16 words (32 bytes).

    The checksum field (byte 28) must already be zero when this runs; the USB HW drops
    a frame whose descriptor checksum is wrong, which is how it recovers from a bulk-out
    error. The span is always the first 32 bytes regardless of descriptor length.
    """
    chk = 0
    for i in range(0, 32, 2):
        chk ^= int.from_bytes(desc[i:i + 2], "little")
    return chk


def build_mgmt_txdesc(pkt_len: int, *, hw_rate: int = DESC_RATE1M,
                      rate_id: int = RATEID_IDX_B, bmc: bool = False) -> bytes:
    """Build the 40-byte TX descriptor for one injected frame.

    [SRC] rtl8812a_fill_fake_txdesc (not-PsPoll, not-data-frame case): FIRST_SEG +
    LAST_SEG, OFFSET = TXDESC_SIZE, PKT_SIZE, QUEUE_SEL = QSLT_MGNT, RATE_ID, HWSEQ_EN
    (HW assigns the sequence number), USE_RATE + TX_RATE (fixed rate, no rate
    adaptation), OWN, SEC_TYPE = 0 (the frame is already final — no HW encryption), then
    the descriptor checksum. ``bmc`` sets the broadcast/multicast bit when addr1 is a
    group address (e.g. a broadcast deauth); the caller derives it from the frame. The
    checksum covers the first 32 bytes with its own field zeroed, so it is computed last
    (HWSEQ_EN at byte 32 sits outside that range and does not affect it).
    """
    d = bytearray(TXDESC_SIZE)
    _set_bits(d, 0, 27, 1, 1)               # FIRST_SEG
    _set_bits(d, 0, 26, 1, 1)               # LAST_SEG
    _set_bits(d, 0, 16, 8, TXDESC_SIZE)     # OFFSET (descriptor bytes ahead of the MPDU)
    _set_bits(d, 0, 0, 16, pkt_len)         # PKT_SIZE
    if bmc:
        _set_bits(d, 0, 24, 1, 1)           # BMC (group-addressed frame)
    _set_bits(d, 0, 31, 1, 1)               # OWN
    _set_bits(d, 4, 8, 5, QSLT_MGNT)        # QUEUE_SEL
    _set_bits(d, 4, 16, 5, rate_id)         # RATE_ID
    _set_bits(d, 32, 15, 1, 1)              # HWSEQ_EN
    _set_bits(d, 12, 8, 1, 1)               # USE_RATE
    _set_bits(d, 16, 0, 7, hw_rate)         # TX_RATE
    _set_bits(d, 28, 0, 16, txdesc_checksum(d))   # TX_DESC_CHECKSUM (field zeroed first)
    return bytes(d)
