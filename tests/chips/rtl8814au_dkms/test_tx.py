"""Hardware-free regression for the M4a TX descriptor builder.

Not pcap-diffable as a unit (the deauth/mgmt descriptor is not in the cold-boot
capture), so these tests pin the SET_TX_DESC field bit positions against the vendor
macros, the shared XOR checksum, and the layout it shares with the byte-verified M1
firmware-download descriptor.
"""
from wifit3.chips.rtl8814au_dkms import firmware, tx


def _field(desc, byte_off, bit_start, bit_len):
    word = int.from_bytes(desc[byte_off:byte_off + 4], "little")
    return (word >> bit_start) & ((1 << bit_len) - 1)


def test_size_and_default_fields():
    d = tx.build_mgmt_txdesc(64)
    assert len(d) == 40
    assert _field(d, 0, 0, 16) == 64            # PKT_SIZE
    assert _field(d, 0, 16, 8) == 40            # OFFSET = TXDESC_SIZE
    assert _field(d, 0, 26, 1) == 1             # LAST_SEG
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
    # stored value, and the checksum only covers the first 32 bytes.
    d = bytearray(tx.build_mgmt_txdesc(123, hw_rate=tx.DESC_RATE6M, bmc=True))
    stored = int.from_bytes(d[28:30], "little")
    d[28:30] = b"\x00\x00"
    assert tx.txdesc_checksum(bytes(d)) == stored


def test_shares_word0_and_checksum_with_fw_descriptor():
    # Anchor: the mgmt builder and the byte-verified M1 beacon builder encode the shared
    # word0 sub-fields identically and validate under the same checksum function. (They
    # differ elsewhere: the FW descriptor rides QSLT_BEACON with retry-limit/sw_define.)
    length = 200
    mgmt = tx.build_mgmt_txdesc(length)
    fw = firmware.build_fw_txdesc(length, bmc=False)
    assert _field(mgmt, 0, 0, 16) == _field(fw, 0, 0, 16) == length    # PKT_SIZE
    assert _field(mgmt, 0, 16, 8) == _field(fw, 0, 16, 8) == 40        # OFFSET
    assert _field(mgmt, 0, 26, 1) == _field(fw, 0, 26, 1) == 1         # LAST_SEG
    for desc in (mgmt, fw):
        b = bytearray(desc)
        stored = int.from_bytes(b[28:30], "little")
        b[28:30] = b"\x00\x00"
        assert firmware.txdesc_checksum(bytes(b)) == stored
