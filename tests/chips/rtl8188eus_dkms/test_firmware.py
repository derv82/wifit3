"""Hardware-free regression for the RTL8188EUS (DKMS) firmware-download primitives.

Locks the FW-blob identity and the block-write splitting so a refactor can't drift
from the cold-boot wire. The full USB-replay verification (byte-for-byte against the
capture) lives in ``scripts/chips/rtl8188eus_dkms/verify_pcap.py``.
"""
from wifit3.chips.rtl8188eus_dkms import firmware
from wifit3.chips.rtl8188eus_dkms.constants import (
    FW_8188E_START_ADDRESS,
    FWDL_ChkSum_rpt,
    MAX_REG_BLOCK_SIZE,
    REG_HMETFR,
    WINTINI_RDY,
)


class FakeTx:
    """Records control writes; serves canned reads so download_firmware completes."""

    def __init__(self, reads=None):
        self.writes = []          # (addr, width, value)
        self.blocks = []          # (addr, bytes) from write_block
        self._reads = dict(reads or {})

    def read8(self, a):
        return self._reads.get((a, 1), 0x00)

    def read32(self, a):
        return self._reads.get((a, 4), 0x00)

    def write8(self, a, v):
        self.writes.append((a, 1, v & 0xFF))
        return True

    def write32(self, a, v):
        self.writes.append((a, 4, v & 0xFFFFFFFF))
        return True

    def write_block(self, a, data):
        self.blocks.append((a, bytes(data)))
        return True


def test_blob_identity():
    blob = firmware.load_firmware_blob()
    assert len(blob) == 15262
    sig = int.from_bytes(blob[0:2], "little")
    assert sig == 0x88E1                       # 8188E FW signature (header present)


def test_block_write_split_matches_capture_profile():
    # 15230-byte payload (15262 - 32B header) -> the capture's 75x196 + 66x8 + 2x1.
    buf = bytes(range(256)) * 60          # 15360 -> trim to 15230
    buf = buf[:15230]
    t = FakeTx()
    firmware._write_fw(t, buf)

    block_lens = [len(d) for _, d in t.blocks]
    byte_writes = [w for w in t.writes
                   if w[1] == 1 and w[0] >= FW_8188E_START_ADDRESS]
    assert block_lens.count(196) == 75
    assert block_lens.count(8) == 66
    assert len(byte_writes) == 2

    # Per-page the FW-SRAM address restarts at FW_8188E_START_ADDRESS.
    assert t.blocks[0][0] == FW_8188E_START_ADDRESS
    assert t.blocks[1][0] == FW_8188E_START_ADDRESS + MAX_REG_BLOCK_SIZE


def test_download_firmware_rewrites_the_image_after_a_failed_page_write(mocker):
    t = mocker.Mock()
    t.read8.return_value = 0
    t.read32.return_value = FWDL_ChkSum_rpt | WINTINI_RDY
    t.write_block.side_effect = [False, True]
    fw_blob = bytes(MAX_REG_BLOCK_SIZE)
    assert firmware.download_firmware(t, fw_blob) is True
    assert t.write_block.call_count == 2


def test_download_firmware_inits_the_firmware_vars_after_a_failed_download(mocker):
    mocker.patch("wifit3.chips.rtl8188eus_dkms.firmware.time.sleep")
    mocker.patch("wifit3.chips.rtl8188eus_dkms.firmware.time.perf_counter",
                 side_effect=range(1000))
    t = FakeTx()
    assert firmware.download_firmware(t, bytes(MAX_REG_BLOCK_SIZE)) is False
    assert len(t.blocks) == 3
    assert t.writes[-1] == (REG_HMETFR, 1, 0x0F)
