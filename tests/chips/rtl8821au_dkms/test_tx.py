"""Hardware-free regression for the M6 TX descriptor builder.

There is no cold-boot TX capture to diff against (the pcap is a passive monitor
bring-up), so these tests pin the SET_TX_DESC_*_8812 field bit positions against the
vendor macros, the XOR-16 checksum (rtl8812a_cal_txdesc_chksum), and a golden byte
string for the canonical unicast-deauth descriptor.
"""
import struct

from functools import reduce

from wifit3.chips.rtl8821au_dkms import tx


def _field(desc, byte_off, bit_start, bit_len):
    word = int.from_bytes(desc[byte_off:byte_off + 4], "little")
    return (word >> bit_start) & ((1 << bit_len) - 1)


# Verified output of build_mgmt_txdesc(26) — a 26-byte (deauth) MPDU, 1 Mbps CCK,
# RATE_ID B, unicast. Locks the exact field encoding + checksum against regression.
_GOLDEN_UNICAST_26 = bytes.fromhex(
    "1a00288c0012080000000000000100000000000000000000000000003a9f00000080000000000000"
)


def test_golden_unicast_descriptor():
    assert tx.build_mgmt_txdesc(26) == _GOLDEN_UNICAST_26


def test_size_and_default_fields():
    d = tx.build_mgmt_txdesc(64)
    assert len(d) == 40
    assert _field(d, 0, 0, 16) == 64            # PKT_SIZE
    assert _field(d, 0, 16, 8) == 40            # OFFSET = TXDESC_SIZE
    assert _field(d, 0, 26, 1) == 1             # LAST_SEG
    assert _field(d, 0, 27, 1) == 1             # FIRST_SEG
    assert _field(d, 0, 31, 1) == 1             # OWN
    assert _field(d, 0, 24, 1) == 0             # BMC off by default
    assert _field(d, 4, 8, 5) == tx.QSLT_MGNT   # QUEUE_SEL = 0x12
    assert _field(d, 4, 16, 5) == tx.RATEID_IDX_B
    assert _field(d, 4, 22, 2) == 0             # SEC_TYPE = 0 (no HW encryption)
    assert _field(d, 12, 8, 1) == 1             # USE_RATE
    assert _field(d, 32, 15, 1) == 1            # HWSEQ_EN (word8 bit15)
    assert _field(d, 16, 0, 7) == tx.DESC_RATE1M


def test_bmc_and_rate_params():
    d = tx.build_mgmt_txdesc(100, hw_rate=tx.DESC_RATE6M,
                             rate_id=tx.RATEID_IDX_G, bmc=True)
    assert _field(d, 0, 24, 1) == 1             # BMC set (broadcast deauth)
    assert _field(d, 16, 0, 7) == tx.DESC_RATE6M
    assert _field(d, 4, 16, 5) == tx.RATEID_IDX_G


def test_checksum_validates():
    # Re-deriving the checksum over the descriptor (its field zeroed) reproduces the
    # stored value, and it only covers the first 32 bytes.
    d = bytearray(tx.build_mgmt_txdesc(123, hw_rate=tx.DESC_RATE6M, bmc=True))
    stored = int.from_bytes(d[28:30], "little")
    d[28:30] = b"\x00\x00"
    assert tx.txdesc_checksum(d) == stored


def test_checksum_invariant_xor_zero():
    # By construction the XOR of all 16 LE u16 words over bytes 0..31 is zero once the
    # checksum field is filled — a one-line integrity check the HW relies on.
    for pkt_len, bmc in ((26, False), (100, True), (1500, False)):
        d = tx.build_mgmt_txdesc(pkt_len, bmc=bmc)
        assert reduce(lambda a, x: a ^ x, struct.unpack("<16H", d[:32]), 0) == 0
