"""Hardware-free regression for the RTL8822BU (DKMS) firmware blob + header parse.

The shipped blob is the morrownr ``array_mp_8822b_fw_nic`` (v30.20, 161240 B); the
full byte-for-byte download verification lives in the pcap gate
(``scripts/chips/rtl8822bu_dkms/verify_pcap.py``) once the iDDMA download is wired.
"""
from wifit3.chips.rtl8822bu_dkms import firmware


def test_blob_loads_with_expected_size():
    blob = firmware.load_firmware_blob()
    assert len(blob) == 161240
    # WLAN_FW signature word (chip id) and version 30.20.
    assert blob[0] | (blob[1] << 8) == 0x8822
    assert (blob[4], blob[6]) == (30, 20)


def test_header_parse_matches_blob():
    h = firmware.parse_fw_header(firmware.load_firmware_blob())
    # sizes include the 8-byte per-segment checksum; no emem on this NIC FW.
    assert (h.dmem_size, h.imem_size, h.emem_size) == (11216, 149960, 0)
    # addresses have the BIT(31) flag masked off (raw 0x80200000 / 0x80000000).
    assert (h.dmem_addr, h.imem_addr) == (0x00200000, 0x00000000)
    # header (64) + dmem + imem + emem accounts for every byte.
    assert 64 + h.dmem_size + h.imem_size + h.emem_size == 161240


def test_fw_txdesc_matches_capture():
    # Golden 48-byte TX descriptor of the first FW packet (4096 B chunk) from capture-1:
    # TXPKTSIZE=0x1000, OFFSET=0x30, QSEL=0x10 (beacon), XOR-16 checksum 0x0030 @ byte 28.
    golden = bytes.fromhex(
        "00103000" "00100000" + "00" * 20 + "30000000" + "00" * 16)
    assert firmware.build_fw_txdesc(4096) == golden
    # The descriptor's own checksum (XOR of all 16 LE u16 words) is self-consistent: zero.
    d = firmware.build_fw_txdesc(3025)
    assert sum(int.from_bytes(d[2 * i:2 * i + 2], "little") for i in range(16)) % 2 == 0
    chk = 0
    for i in range(16):
        chk ^= int.from_bytes(d[2 * i:2 * i + 2], "little")
    assert chk == 0
