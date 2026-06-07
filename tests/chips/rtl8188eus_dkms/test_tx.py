"""Hardware-free regression for the RTL8188EUS (DKMS) TX descriptor builder.

Locks the 32-byte mgmt descriptor field positions (OWN/FSG/LSG, OFFSET, PKT_SIZE, MGMT
queue, HW-seq, driver-uses-rate, BMC) and the XOR checksum. Live TX is the user's to fire.
"""
from wifit3.chips.rtl8188eus_dkms import tx


def _field(desc, byte_off, bit_start, bit_len):
    word = int.from_bytes(desc[byte_off:byte_off + 4], "little")
    return (word >> bit_start) & ((1 << bit_len) - 1)


def test_mgmt_txdesc_fields():
    d = tx.build_mgmt_txdesc(42)
    assert len(d) == 32
    assert _field(d, 0, 0, 16) == 42                # PKT_SIZE
    assert _field(d, 0, 16, 8) == 32                # OFFSET = TXDESC_SIZE
    assert _field(d, 0, 26, 1) == 1                 # LSG
    assert _field(d, 0, 27, 1) == 1                 # FSG
    assert _field(d, 0, 31, 1) == 1                 # OWN
    assert _field(d, 0, 24, 1) == 0                 # BMC clear by default
    assert _field(d, 4, 8, 5) == tx.QSLT_MGNT       # MGMT queue
    assert _field(d, 16, 7, 1) == 1                 # HW sequence number
    assert _field(d, 16, 8, 1) == 1                 # driver uses rate


def test_mgmt_txdesc_bmc():
    d = tx.build_mgmt_txdesc(30, bmc=True)
    assert _field(d, 0, 24, 1) == 1                 # BMC set (group-addressed)


def test_txdesc_checksum_xor():
    d = bytearray(tx.build_mgmt_txdesc(64))
    # The stored checksum makes the full 16-u16 XOR (with the field included) self-consistent:
    # XOR of all 16 words == 0 once the checksum word holds the XOR of the other 15.
    words = [int.from_bytes(d[2 * i:2 * i + 2], "little") for i in range(16)]
    xor_all = 0
    for w in words:
        xor_all ^= w
    assert xor_all == 0
    # And recomputing (field-zeroed) reproduces the stored checksum.
    assert tx.txdesc_checksum(bytes(d)) == int.from_bytes(d[28:30], "little")
