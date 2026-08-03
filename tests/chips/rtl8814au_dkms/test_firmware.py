"""Hardware-free regression for the RTL8814AU (DKMS) firmware-download primitives.

Golden TX-descriptor bytes are taken verbatim from the cold-boot capture
(``driver_captures/captures_rtl8814au/capture-1.pcap``); these lock the wire-exact
descriptor + checksum so a future refactor can't silently drift. The full
USB-replay verification lives in ``scripts/chips/rtl8814au_dkms/verify_m1_pcap.py``.
"""
from importlib import resources

from wifit3.chips.rtl8814au_dkms import firmware

# (length, bmc, txdesc[0:40]) — captured FW packets idx 0, 1, and 45.
_GOLDEN = [
    (1488, False, "d005288401100800000000000001000000001a000000000001000000ea9000000080000000000000"),
    (1488, True,  "d0052885011008000000003f0001000000001a000000000001000000eaae00000080000000000000"),
    (1456, False, "b005288401100800000000000001000000001a0000000000010000008a9000000080000000000000"),
]


def _blob() -> bytes:
    return (resources.files("wifit3.chips.rtl8814au_dkms") / "assets" / "rtl8814au_fw.bin").read_bytes()


def test_txdesc_matches_capture():
    for length, bmc, hexbytes in _GOLDEN:
        assert firmware.build_fw_txdesc(length, bmc) == bytes.fromhex(hexbytes)


def test_txdesc_checksum_known_value():
    # First FW packet's embedded descriptor checksum is 0x90ea (word7 low half).
    desc = bytearray(firmware.build_fw_txdesc(1488, bmc=False))
    assert desc[28] | (desc[29] << 8) == 0x90EA
    # Recomputing over the zeroed field reproduces it; XOR of a valid desc is 0.
    desc[28] = desc[29] = 0
    assert firmware.txdesc_checksum(bytes(desc)) == 0x90EA
    assert firmware.txdesc_checksum(firmware.build_fw_txdesc(1488, bmc=False)) == 0


def test_header_parse():
    dmem_pkt, iram_pkt = firmware.parse_fw_header(_blob())
    assert (dmem_pkt, iram_pkt) == (5792, 62464)  # incl. 8-byte checksum dummies


def test_download_blocks_cover_blob_exactly():
    fw = _blob()
    dmem_pkt, iram_pkt = firmware.parse_fw_header(fw)
    blocks = list(firmware._download_blocks(fw, dmem_pkt, iram_pkt))

    # 46 packets; sizes match the capture (3x1488 + 1328 DMEM, 41x1488 + 1456 IRAM).
    sizes = [len(b[0]) for b in blocks]
    assert sizes == [1488, 1488, 1488, 1328] + [1488] * 41 + [1456]

    # First/last block of each region is fs/ls; concatenated payload == fw[64:].
    assert blocks[0][2] is True and blocks[3][3] is True   # DMEM fs / ls
    assert blocks[4][2] is True and blocks[45][3] is True  # IRAM fs / ls
    assert b"".join(b[0] for b in blocks) == fw[64:]
