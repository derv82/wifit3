"""RTL8814AU TX descriptor builder (M4a) — vendor faithful.

Ports `rtl8814a_fill_fake_txdesc` [SRC rtl8814a_xmit.c:267] — the vendor's minimal,
self-contained management TX descriptor (what it hands the HW to transmit a frame
directly). That field set is exactly what a monitor-mode deauth needs: one
non-aggregated, unencrypted management frame at a fixed low rate.

This is NOT the full `update_txdesc` (rate adaptation, aggregation, security, the data
queues), and it is NOT the beacon/rsvd-page descriptor that M1's `build_fw_txdesc`
emits (that one rides QSLT_BEACON and carries retry-limit / sw_define / DISQSELSEQ that
a bare management frame does not). The 40-byte size, the SET_TX_DESC field bit
positions, and the XOR checksum are shared, so the descriptor checksum reuses the
M1-verified `rtl8814a_cal_txdesc_chksum` port (`firmware.txdesc_checksum`).

Field bit positions [SRC include/rtl8814a_xmit.h SET_TX_DESC_*_8814A]; queue/rate
constants [SRC include/hal_com.h, include/ieee80211.h].
"""
from __future__ import annotations

from .constants import TXDESC_SIZE
from .firmware import txdesc_checksum  # shared XOR checksum, byte-verified by M1

# Queue-select: a management frame uses the MGMT queue [SRC hal_com.h QSLT_MGNT].
QSLT_MGNT = 0x12
# Rate-ID groups [SRC ieee80211.h]: RATEID_IDX_B (CCK basic rates) is the safe 2.4 GHz
# management default; RATEID_IDX_G selects the OFDM group.
RATEID_IDX_G = 7
RATEID_IDX_B = 8
# DESC hardware rate codes [SRC hal_com.h] (the MRateToHwRate output).
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


def build_mgmt_txdesc(pkt_len: int, *, hw_rate: int = DESC_RATE1M,
                      rate_id: int = RATEID_IDX_B, bmc: bool = False) -> bytes:
    """Build the 40-byte TX descriptor for one management frame.

    [SRC] rtl8814a_fill_fake_txdesc, not-PS-Poll / not-data-frame case: LAST_SEG,
    OFFSET=TXDESC_SIZE, PKT_SIZE, QUEUE_SEL=QSLT_MGNT, RATE_ID, HWSEQ_EN (HW assigns the
    sequence number), USE_RATE + TX_RATE (send at the fixed rate, no rate adaptation),
    SEC_TYPE=0 (the injected frame is already final — no HW encryption), then the
    descriptor checksum. ``bmc`` sets the broadcast/multicast bit when addr1 is a group
    address (e.g. a broadcast deauth); `update_txdesc` derives it from the frame, the
    caller passes it here. The checksum covers the first 32 bytes with its own field
    zeroed, so it is computed last (HWSEQ_EN at offset 32 sits outside that range).
    """
    d = bytearray(TXDESC_SIZE)
    _set_bits(d, 0, 26, 1, 1)               # LAST_SEG
    _set_bits(d, 0, 16, 8, TXDESC_SIZE)     # OFFSET (descriptor bytes ahead of the MPDU)
    _set_bits(d, 0, 0, 16, pkt_len)         # PKT_SIZE
    if bmc:
        _set_bits(d, 0, 24, 1, 1)           # BMC (group-addressed frame)
    _set_bits(d, 4, 8, 5, QSLT_MGNT)        # QUEUE_SEL
    _set_bits(d, 4, 16, 5, rate_id)         # RATE_ID
    _set_bits(d, 32, 15, 1, 1)              # HWSEQ_EN
    _set_bits(d, 12, 8, 1, 1)               # USE_RATE
    _set_bits(d, 16, 0, 7, hw_rate)         # TX_RATE
    _set_bits(d, 28, 0, 16, txdesc_checksum(d))   # TX_DESC_CHECKSUM (field zeroed first)
    return bytes(d)
