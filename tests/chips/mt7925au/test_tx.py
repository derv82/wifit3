"""connac3 TX descriptor build for mt7925au (tx.build_tx / driver TX wiring).

The TXWI dwords below are exactly those the mt76 xmit path emits for a directed deauth
(the port is byte-verified 573/573 against the real TX captures in
scripts/mt7925au/verify_pcap.py). MAC addresses here are synthetic placeholders: the
TXWI does not depend on the address bytes (only addr1's group bit and seq_ctrl), so the
descriptor is identical. These lock the per-field TXWI layout against a silent regress.
"""
import struct

from wifit3.chips.mt7925au import tx
from wifit3.chips.mt7925au.constants import MT792x_WTBL_RESERVED
from wifit3.chips.mt7925au.driver import MT7925AUDriver

# A directed deauth (fc=0x00c0, reason 7), 26 bytes, seq_ctrl = 0. Synthetic unicast MACs.
DEAUTH_MPDU = bytes.fromhex(
    "c0003a01"              # frame_control=0x00c0, duration=0x013a
    "020000000001"          # addr1 (DA)   — locally-administered, unicast
    "020000000002"          # addr2 (SA)
    "020000000002"          # addr3 (BSSID)
    "0000"                  # seq_ctrl
    "0700"                  # reason code 7
)
# The exact 100-byte wire frame the mt76 xmit path produces for that MPDU.
DEAUTH_WIRE = bytes.fromhex(
    "5a000000"              # SDIO hdr: tx_bytes = 64 (TXD) + 26 (MPDU) = 90
    "5a008020" "13b00c80" "0c000000" "01780090"   # txwi[0..3]
    "00000000" "00000000" "1c000f00" "00000000"   # txwi[4..7]
    "00000000" "00000000" "00000000" "00000000"   # txwi[8..11] (zero)
    "00000000" "00000000" "00000000" "00000000"   # txwi[12..15] (zero)
    + DEAUTH_MPDU.hex()
    + "000000000000"        # pad: round_up(4+64+26,4)-that +4 = 6
)


def test_build_tx_deauth_byte_exact():
    frame = tx.build_tx(DEAUTH_MPDU, wcid_idx=MT792x_WTBL_RESERVED)
    assert frame == DEAUTH_WIRE
    assert len(frame) == 100


def test_txwi_fields():
    frame = tx.build_tx(DEAUTH_MPDU, wcid_idx=MT792x_WTBL_RESERVED)
    txwi = struct.unpack_from("<16I", frame, 4)
    assert txwi[0] & 0xFFFF == 90            # TX_BYTES = MPDU + 64
    assert (txwi[0] >> 23) & 0x3 == 1        # PKT_FMT = MT_TX_TYPE_SF
    assert (txwi[0] >> 25) & 0x7F == 0x10    # Q_IDX = MT_LMAC_ALTX0
    assert txwi[1] >> 31 == 1                # FIXED_RATE
    assert (txwi[1] >> 14) & 0x3 == 2        # HDR_FORMAT = 802_11
    assert (txwi[1] >> 16) & 0x1F == 12      # HDR_INFO = hdrlen(24)/2
    assert (txwi[1] >> 12) & 0x3 == 3        # TGID = band_idx 0xff & 3
    assert txwi[1] & 0xFFF == MT792x_WTBL_RESERVED
    assert txwi[2] == 0x0C                   # FRAME_TYPE 0 (mgmt), SUB_TYPE 12 (deauth)
    assert txwi[3] >> 31 == 1                # SN_VALID
    assert (txwi[3] >> 28) & 1 == 1          # BA_DISABLE
    assert (txwi[3] >> 11) & 0x1F == 15      # REM_TX_COUNT
    assert txwi[3] & 1 == 1                  # NO_ACK
    assert (txwi[6] >> 16) & 0x3F == 15      # TX_RATE = basic_rates_idx
    assert txwi[7] == 0
    assert txwi[8:] == (0,) * 8              # trailing 32 B of the 64 B TXD stay zero


def test_seq_is_read_from_mpdu():
    """The injected-frame branch copies the 802.11 sequence into txwi[3] SEQ; only that
    field changes with the frame's seq_ctrl."""
    mpdu = bytearray(DEAUTH_MPDU)
    struct.pack_into("<H", mpdu, 22, 0x60)   # SN = 6
    txwi = struct.unpack_from("<16I", tx.build_tx(bytes(mpdu), wcid_idx=19), 4)
    assert (txwi[3] >> 16) & 0xFFF == 6


def test_stamp_tx_seq_increments_and_feeds_txwi():
    drv = MT7925AUDriver.__new__(MT7925AUDriver)
    drv._tx_seq = 0
    first = drv._stamp_tx_seq(DEAUTH_MPDU)
    second = drv._stamp_tx_seq(DEAUTH_MPDU)
    assert struct.unpack_from("<H", first, 22)[0] == (1 << 4)    # SN 1, frag 0
    assert struct.unpack_from("<H", second, 22)[0] == (2 << 4)   # SN 2
    txwi = struct.unpack_from("<16I", tx.build_tx(second, wcid_idx=19), 4)
    assert (txwi[3] >> 16) & 0xFFF == 2


def test_broadcast_sets_bcm():
    """A group-addressed frame (probe request) sets txwi[3] BCM."""
    probe = bytearray(DEAUTH_MPDU)
    probe[0:2] = struct.pack("<H", 0x0040)   # probe request
    probe[4:10] = b"\xff" * 6                 # broadcast addr1
    txwi = struct.unpack_from("<16I", tx.build_tx(bytes(probe), wcid_idx=19), 4)
    assert (txwi[3] >> 4) & 1 == 1            # BCM
