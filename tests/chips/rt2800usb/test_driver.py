"""M1 unit tests for rt2800usb — chip-ID probe + warm detection.

Uses a tiny dict-backed mock transport so we can assert MAC_CSR0 / MAC
/ warm-bit decoding without touching hardware.
"""
from __future__ import annotations

from typing import Sequence

from wifit3.chips.rt2800usb.constants import (
    MAC_ADDR_DW0,
    MAC_ADDR_DW1,
    MAC_CSR0,
    PBF_SYS_CTRL,
    PBF_SYS_CTRL_READY,
    RT_RT3572,
    RT_RT5390,
    RT_RT5392,
    RT_RT5592,
    USB_PID_RT3572,
    USB_VID_RALINK,
)
from wifit3.chips.rt2800usb import SUPPORTED_IDS
from wifit3.chips.rt2800usb.driver import RT2800USBDriver
from wifit3.chips.rt2800usb.mac import (
    is_chip_warm,
    read_chip_id,
    read_perm_mac,
)


class FakeTransport:
    """Byte-addressable register space backed by a dict.

    Implements the minimum interface that mac/firmware/reg_init helpers
    use — read32 / write32 / read_multi.
    """

    class _FakeDev:
        """Minimal usb.core.Device stand-in for the vendor ctrl_transfer that
        usb_init_registers issues (USB_MODE_RESET, no data phase)."""
        def ctrl_transfer(self, *args, **kwargs):
            return b""

    def __init__(self) -> None:
        self.regs: dict[int, int] = {}
        self.dev = self._FakeDev()
        # MAC_CSR0 (0x1000) must read non-zero so usb_init_registers' wait_csr_ready
        # passes — it is nested inside init_registers (matching rt2800_init_registers).
        self.write32(0x1000, 0x55920222)

    def write_bytes(self, addr: int, data: Sequence[int]) -> None:
        for i, b in enumerate(data):
            self.regs[addr + i] = b & 0xFF

    def _load(self, addr: int, n: int) -> int:
        out = 0
        for i in range(n):
            out |= self.regs.get(addr + i, 0) << (8 * i)
        return out

    def read32(self, addr: int) -> int:
        return self._load(addr, 4)

    def write32(self, addr: int, val: int) -> None:
        for i in range(4):
            self.regs[addr + i] = (val >> (i * 8)) & 0xFF

    def read_multi(self, addr: int, length: int) -> bytes:
        return bytes(self.regs.get(addr + i, 0) for i in range(length))

    def write_multi(self, addr: int, data) -> None:
        for i, b in enumerate(bytes(data)):
            self.regs[addr + i] = b & 0xFF


# Alias for tests that use the "RecordingTransport" name from the
# rtl8187 test suite shape. Same interface — naming difference only.
RecordingTransport = FakeTransport


def test_supported_ids_cover_all_variants():
    # 148f:5572 (RT5572 / PAU09) moved to the standalone chips/rt5572 driver;
    # rt2800usb now claims only RT3572 (the AWUS051NH v2).
    pids = {entry.pid for entry in SUPPORTED_IDS}
    assert pids == {USB_PID_RT3572}
    assert all(entry.vid == USB_VID_RALINK for entry in SUPPORTED_IDS)
    # chip_id hints are populated for downstream variant dispatch
    hints = {entry.extras["chip_id"] for entry in SUPPORTED_IDS}
    assert hints == {"rt3572"}


def test_supported_channels_covers_2g_plus_5g_non_dfs():
    """M-A2 extends to 5 GHz non-DFS channels. RT5392 will fail-soft on
    these (driver.set_channel returns False); RT3572 + RT5572 use them."""
    from wifit3.chips.rt2800usb.chan import CHANNELS_5G_NON_DFS
    expected = list(range(1, 15)) + list(CHANNELS_5G_NON_DFS)
    assert RT2800USBDriver.SUPPORTED_CHANNELS == expected
    # Spot-check that the canonical non-DFS UNII channels are all present.
    for ch in (36, 40, 44, 48, 149, 153, 157, 161, 165):
        assert ch in RT2800USBDriver.SUPPORTED_CHANNELS


def _set_mac_csr0(t: FakeTransport, silicon: int, revision: int) -> None:
    word = (silicon << 16) | (revision & 0xFFFF)
    t.write_bytes(MAC_CSR0, [
        word & 0xFF, (word >> 8) & 0xFF, (word >> 16) & 0xFF, (word >> 24) & 0xFF,
    ])


def test_read_chip_id_decodes_rt5390_for_panda_pau05():
    """USB PID 0x5372 reports silicon 0x5390 OR 0x5392 (the RT539x
    family covers RT5370/RT5372 across silicon revisions). Make sure
    the decoder doesn't trip on the mismatch between marketing name
    (RT5372) and silicon ID (RT5390/RT5392)."""
    t = FakeTransport()
    _set_mac_csr0(t, silicon=RT_RT5390, revision=0x0223)
    chip = read_chip_id(t)
    assert chip.silicon_id == 0x5390
    assert chip.name == "RT5390"
    assert chip.is_supported is True


def test_read_chip_id_decodes_rt5392_real_panda_pau05_hw():
    """User's actual Panda PAU05 reports 0x5392 rev 0x0223 (not 0x5390
    as the marketing name 'RT5372' would suggest). [WIRE M1]"""
    t = FakeTransport()
    _set_mac_csr0(t, silicon=RT_RT5392, revision=0x0223)
    chip = read_chip_id(t)
    assert chip.silicon_id == 0x5392
    assert chip.name == "RT5392"
    assert chip.is_supported is True


def test_read_chip_id_decodes_rt5592_for_panda_pau09():
    t = FakeTransport()
    _set_mac_csr0(t, silicon=RT_RT5592, revision=0x0222)
    chip = read_chip_id(t)
    assert chip.silicon_id == 0x5592
    assert chip.name == "RT5592"
    assert chip.is_supported is True


def test_read_chip_id_decodes_rt3572():
    t = FakeTransport()
    _set_mac_csr0(t, silicon=RT_RT3572, revision=0x0101)
    chip = read_chip_id(t)
    assert chip.silicon_id == 0x3572
    assert chip.name == "RT3572"
    assert chip.is_supported is True


def test_read_chip_id_unknown_silicon_marked_unsupported():
    t = FakeTransport()
    _set_mac_csr0(t, silicon=0xAA55, revision=0)
    chip = read_chip_id(t)
    assert chip.silicon_id == 0xAA55
    assert chip.is_supported is False
    # Falls back to the hex representation when the silicon name isn't known
    assert chip.name == "0xaa55"


def test_read_perm_mac_assembles_dw0_dw1():
    t = FakeTransport()
    # DW0 = bytes 0..3, DW1 = bytes 4..5 (only low 16 bits of DW1 used)
    t.write_bytes(MAC_ADDR_DW0, [0x12, 0x34, 0x56, 0x78])
    t.write_bytes(MAC_ADDR_DW1, [0x9A, 0xBC, 0x00, 0x00])
    assert read_perm_mac(t) == bytes.fromhex("123456789abc")


# ----------------------------------------------------------------------
# M2a firmware tests
# ----------------------------------------------------------------------
def test_check_firmware_crc_accepts_valid_blob():
    """Build a synthetic 4096-byte chunk with a correct CRC-CCITT trailer
    and verify the checker accepts it."""
    from wifit3.chips.rt2800usb.firmware import _crc_ccitt, check_firmware_crc

    payload = (bytes(range(256)) * 16)[:4094]  # exactly 4094 bytes
    crc = _crc_ccitt(payload)
    crc_swapped = ((crc & 0xFF) << 8) | (crc >> 8)
    blob = payload + bytes(((crc_swapped >> 8) & 0xFF, crc_swapped & 0xFF))
    assert len(blob) == 4096
    assert check_firmware_crc(blob) is True


def test_check_firmware_crc_rejects_corruption():
    """Flip one byte mid-payload and confirm CRC fails."""
    from wifit3.chips.rt2800usb.firmware import _crc_ccitt, check_firmware_crc

    payload = (bytes(range(256)) * 16)[:4094]
    crc = _crc_ccitt(payload)
    crc_swapped = ((crc & 0xFF) << 8) | (crc >> 8)
    blob = payload + bytes(((crc_swapped >> 8) & 0xFF, crc_swapped & 0xFF))
    corrupted = blob[:100] + bytes([blob[100] ^ 0xFF]) + blob[101:]
    assert check_firmware_crc(corrupted) is False


def test_bundled_rt5572_bin_passes_crc():
    """The shipped assets/rt5572.bin is 4096 bytes with a trailing CRC.
    Sanity-check that it survives our own CRC validation — otherwise
    M2a will reject it on the hw test before even attempting upload."""
    from wifit3.chips.rt2800usb.firmware import check_firmware_crc, load_firmware_blob

    blob = load_firmware_blob()
    assert len(blob) == 4096, f"expected 4096-byte blob, got {len(blob)}"
    assert check_firmware_crc(blob) is True, "bundled rt5572.bin fails CRC"


# ----------------------------------------------------------------------
# M2b-2 init_registers tests
# ----------------------------------------------------------------------
def test_set_field32_helper():
    """Verify the bit-field set helper matches kernel rt2x00_set_field32
    semantics across a few representative masks."""
    from wifit3.chips.rt2800usb.reg_init import _set_field32

    # Lowest-byte field
    assert _set_field32(0x00000000, 0x000000FF, 0x42) == 0x00000042
    # Field that needs shift
    assert _set_field32(0x00000000, 0x0000FF00, 0x42) == 0x00004200
    # Field that needs shift + clear of old bits
    assert _set_field32(0xDEADBEEF, 0x0000FF00, 0x42) == 0xDEAD42EF
    # Top byte
    assert _set_field32(0x00000000, 0xFF000000, 0x42) == 0x42000000
    # 16-bit BEACON_INTERVAL field
    assert _set_field32(0x00000000, 0x0000FFFF, 1600) == 0x00000640


def test_init_registers_writes_basic_rates(monkeypatch):
    """Smoke test: a known-good RecordingTransport sequence ends with
    LEGACY_BASIC_RATE = 0x13F and HT_BASIC_RATE = 0x8003 latched."""
    from wifit3.chips.rt2800usb.constants import (
        HT_BASIC_RATE, LEGACY_BASIC_RATE, RT_RT5392,
        WPDMA_GLO_CFG,
    )
    from wifit3.chips.rt2800usb.reg_init import init_registers

    t = RecordingTransport()
    # Avoid the disable_wpdma read returning 0 forever; preseed something.
    t.write_bytes(WPDMA_GLO_CFG, [0, 0, 0, 0])
    init_registers(t, silicon_id=RT_RT5392)

    # LEGACY_BASIC_RATE and HT_BASIC_RATE are direct writes (no R-M-W),
    # so they should land exactly as written.
    legacy = t._load(LEGACY_BASIC_RATE, 4)
    ht = t._load(HT_BASIC_RATE, 4)
    assert legacy == 0x0000013F, f"LEGACY_BASIC_RATE = 0x{legacy:08x}"
    assert ht == 0x00008003, f"HT_BASIC_RATE = 0x{ht:08x}"


def test_init_registers_writes_tx_sw_cfg_for_rt5392(monkeypatch):
    """RT5392 path writes TX_SW_CFG0/1/2 = 0x404 / 0x080606 / 0."""
    from wifit3.chips.rt2800usb.constants import (
        RT_RT5392, TX_SW_CFG0, TX_SW_CFG1, TX_SW_CFG2, WPDMA_GLO_CFG,
    )
    from wifit3.chips.rt2800usb.reg_init import init_registers

    t = RecordingTransport()
    t.write_bytes(WPDMA_GLO_CFG, [0, 0, 0, 0])
    init_registers(t, silicon_id=RT_RT5392)

    assert t._load(TX_SW_CFG0, 4) == 0x00000404
    assert t._load(TX_SW_CFG1, 4) == 0x00080606
    assert t._load(TX_SW_CFG2, 4) == 0x00000000


def test_init_registers_picks_txop_hldr_et_per_chip(monkeypatch):
    """TXOP_HLDR_ET = 0x82 for RT5592, 0x02 for everything else."""
    from wifit3.chips.rt2800usb.constants import (
        RT_RT5392, RT_RT5592, TXOP_HLDR_ET, WPDMA_GLO_CFG,
    )
    from wifit3.chips.rt2800usb.reg_init import init_registers

    for silicon, expected in ((RT_RT5392, 0x02), (RT_RT5592, 0x82)):
        t = RecordingTransport()
        t.write_bytes(WPDMA_GLO_CFG, [0, 0, 0, 0])
        init_registers(t, silicon_id=silicon)
        assert t._load(TXOP_HLDR_ET, 4) == expected, \
            f"silicon=0x{silicon:04x}: TXOP_HLDR_ET = 0x{t._load(TXOP_HLDR_ET, 4):08x}"


# ----------------------------------------------------------------------
# M2b-3 BBP indirect access + init tests
# ----------------------------------------------------------------------
class BbpFakeTransport(FakeTransport):
    """FakeTransport with a working BBP_CSR_CFG protocol — the chip
    auto-clears BUSY on every read so the wait loop terminates."""

    def __init__(self) -> None:
        super().__init__()
        # In-chip BBP register file (separate from MMIO regs).
        self.bbp_regs: dict[int, int] = {}

    def read32(self, addr: int) -> int:
        from wifit3.chips.rt2800usb.constants import (
            BBP_CSR_CFG, BBP_CSR_CFG_BUSY,
        )
        val = super().read32(addr)
        if addr == BBP_CSR_CFG:
            # Simulate hw auto-clearing BUSY + populating VALUE on reads.
            val &= ~BBP_CSR_CFG_BUSY
        return val

    def write32(self, addr: int, val: int) -> None:
        from wifit3.chips.rt2800usb.constants import (
            BBP_CSR_CFG, BBP_CSR_CFG_BUSY,
            BBP_CSR_CFG_READ_CONTROL, BBP_CSR_CFG_REGNUM, BBP_CSR_CFG_VALUE,
        )
        if addr == BBP_CSR_CFG:
            regnum = (val & BBP_CSR_CFG_REGNUM) >> 8
            payload = val & BBP_CSR_CFG_VALUE
            is_read = bool(val & BBP_CSR_CFG_READ_CONTROL)
            if is_read:
                # Stash the read result so the next read_csr returns it.
                stored = self.bbp_regs.get(regnum, 0)
                final = (val & ~(BBP_CSR_CFG_BUSY | BBP_CSR_CFG_VALUE)) | (stored & 0xFF)
                # Persist post-read state with BUSY cleared.
                final &= ~BBP_CSR_CFG_BUSY
                super().write32(addr, final)
                return
            else:
                # Write to BBP register file; clear BUSY on the CSR.
                self.bbp_regs[regnum] = payload
                final = val & ~BBP_CSR_CFG_BUSY
                super().write32(addr, final)
                return
        super().write32(addr, val)


def test_bbp_write_then_read_roundtrip():
    from wifit3.chips.rt2800usb.bbp import bbp_read, bbp_write
    t = BbpFakeTransport()
    bbp_write(t, 65, 0x2C)
    bbp_write(t, 31, 0x08)
    bbp_write(t, 106, 0x12)
    assert bbp_read(t, 65) == 0x2C
    assert bbp_read(t, 31) == 0x08
    assert bbp_read(t, 106) == 0x12


def test_bbp4_mac_if_ctrl_sets_bit_0x40():
    from wifit3.chips.rt2800usb.bbp import bbp4_mac_if_ctrl, bbp_read, bbp_write
    t = BbpFakeTransport()
    # Pre-seed BBP[4] with some other bits to verify R-M-W preserves them.
    bbp_write(t, 4, 0x12)
    bbp4_mac_if_ctrl(t)
    assert bbp_read(t, 4) == 0x52  # 0x12 | 0x40


def test_init_bbp_53xx_rt5392_path():
    """Verify the RT5392-specific BBP writes land (88, 95, 98, 134, 135)
    that the RT5390 path skips."""
    from wifit3.chips.rt2800usb.bbp import bbp_read, init_bbp_53xx
    from wifit3.chips.rt2800usb.constants import RT_RT5392
    t = BbpFakeTransport()
    init_bbp_53xx(t, silicon_id=RT_RT5392)
    # Common writes (both RT5390 and RT5392)
    assert bbp_read(t, 31) == 0x08
    assert bbp_read(t, 65) == 0x2C
    assert bbp_read(t, 66) == 0x38
    # RT5392-specific
    assert bbp_read(t, 88) == 0x90
    assert bbp_read(t, 95) == 0x9A
    assert bbp_read(t, 98) == 0x12
    assert bbp_read(t, 134) == 0xD0
    assert bbp_read(t, 135) == 0xF6
    # BBP[106] = 0x12 for RT5392 (vs 0x03 for RT5390)
    assert bbp_read(t, 106) == 0x12
    # Freq calibration
    assert bbp_read(t, 142) == 1
    assert bbp_read(t, 143) == 57


def test_init_bbp_53xx_rt5390_path_uses_different_106():
    from wifit3.chips.rt2800usb.bbp import bbp_read, init_bbp_53xx
    from wifit3.chips.rt2800usb.constants import RT_RT5390
    t = BbpFakeTransport()
    init_bbp_53xx(t, silicon_id=RT_RT5390)
    # RT5390 writes 0x03 to BBP[106]; RT5392 writes 0x12
    assert bbp_read(t, 106) == 0x03
    # RT5390 should NOT have written the RT5392-specific BBPs (88, 95, 98, 134, 135)
    assert bbp_read(t, 88) == 0
    assert bbp_read(t, 95) == 0
    assert bbp_read(t, 98) == 0


def test_init_bbp_53xx_rejects_unsupported_silicon():
    import pytest
    from wifit3.chips.rt2800usb.bbp import init_bbp_53xx
    from wifit3.chips.rt2800usb.constants import RT_RT3572
    t = BbpFakeTransport()
    with pytest.raises(ValueError, match="unsupported silicon"):
        init_bbp_53xx(t, silicon_id=RT_RT3572)


def test_init_bbp_3572_lays_down_kernel_table():
    """Spot-check the init_bbp_3572 writes against the kernel table
    [SRC rt2800lib.c:6764-6798]."""
    from wifit3.chips.rt2800usb.bbp import bbp_read, init_bbp_3572
    t = BbpFakeTransport()
    init_bbp_3572(t, txpath=2, rxpath=2)
    expected = {
        31: 0x08, 65: 0x2C, 66: 0x38, 69: 0x12, 70: 0x0A,
        73: 0x10, 79: 0x13, 80: 0x05, 81: 0x33, 82: 0x62,
        83: 0x6A, 84: 0x99, 86: 0x00, 91: 0x04, 92: 0x00,
        103: 0xC0, 105: 0x05, 106: 0x35,
    }
    for word, value in expected.items():
        assert bbp_read(t, word) == value, \
            f"BBP[{word}] = 0x{bbp_read(t, word):02x}, expected 0x{value:02x}"


def test_init_bbp_dispatcher_routes_by_silicon():
    """init_bbp(silicon=RT5392) should hit the 53xx path; init_bbp(silicon=RT3572)
    should hit the 3572 path. Easy discriminator: BBP[106] is 0x12 for RT5392,
    0x35 for RT3572 — different bytes per kernel table."""
    from wifit3.chips.rt2800usb.bbp import bbp_read, init_bbp
    from wifit3.chips.rt2800usb.constants import RT_RT3572, RT_RT5392

    t_5392 = BbpFakeTransport()
    init_bbp(t_5392, RT_RT5392, txpath=1, rxpath=1)
    assert bbp_read(t_5392, 106) == 0x12

    t_3572 = BbpFakeTransport()
    init_bbp(t_3572, RT_RT3572, txpath=2, rxpath=2)
    assert bbp_read(t_3572, 106) == 0x35


def test_disable_unused_dac_adc_noop_for_2t2r():
    """RT3572 2T2R hw should NOT trigger either BBP138 mutation —
    the kernel only writes when txpath==1 or rxpath==1."""
    from wifit3.chips.rt2800usb.bbp import bbp_read, bbp_write, disable_unused_dac_adc
    t = BbpFakeTransport()
    bbp_write(t, 138, 0x55)   # arbitrary pre-state
    disable_unused_dac_adc(t, txpath=2, rxpath=2)
    assert bbp_read(t, 138) == 0x55, "2T2R should leave BBP[138] untouched"


# ----------------------------------------------------------------------
# M2c RFCSR + RF init tests
# ----------------------------------------------------------------------
class RfcsrFakeTransport(BbpFakeTransport):
    """Adds RF_CSR_CFG indirect access on top of the BBP fake."""

    def __init__(self) -> None:
        super().__init__()
        self.rf_regs: dict[int, int] = {}

    def read32(self, addr: int) -> int:
        from wifit3.chips.rt2800usb.constants import (
            RF_CSR_CFG, RF_CSR_CFG_BUSY,
        )
        val = super().read32(addr)
        if addr == RF_CSR_CFG:
            val &= ~RF_CSR_CFG_BUSY
        return val

    def write32(self, addr: int, val: int) -> None:
        from wifit3.chips.rt2800usb.constants import (
            RF_CSR_CFG, RF_CSR_CFG_BUSY, RF_CSR_CFG_DATA,
            RF_CSR_CFG_REGNUM, RF_CSR_CFG_WRITE,
        )
        if addr == RF_CSR_CFG:
            regnum = (val & RF_CSR_CFG_REGNUM) >> 8
            payload = val & RF_CSR_CFG_DATA
            is_write = bool(val & RF_CSR_CFG_WRITE)
            if is_write:
                self.rf_regs[regnum] = payload
                # Persist CSR with BUSY cleared.
                FakeTransport.write32(self, addr, val & ~RF_CSR_CFG_BUSY)
                return
            else:
                stored = self.rf_regs.get(regnum, 0)
                final = (val & ~(RF_CSR_CFG_BUSY | RF_CSR_CFG_DATA)) | (stored & 0xFF)
                final &= ~RF_CSR_CFG_BUSY
                FakeTransport.write32(self, addr, final)
                return
        super().write32(addr, val)


def test_rfcsr_write_then_read_roundtrip():
    from wifit3.chips.rt2800usb.rfcsr import rfcsr_read, rfcsr_write
    t = RfcsrFakeTransport()
    rfcsr_write(t, 1, 0x17)
    rfcsr_write(t, 33, 0xC0)
    rfcsr_write(t, 56, 0xA1)
    assert rfcsr_read(t, 1) == 0x17
    assert rfcsr_read(t, 33) == 0xC0
    assert rfcsr_read(t, 56) == 0xA1


def test_init_rfcsr_5392_writes_full_table(monkeypatch):
    """Spot-check a representative sample of the 56-entry RT5392 RF
    init table landed."""
    import wifit3.chips.rt2800usb.rfcsr as rfm
    monkeypatch.setattr(rfm.time, "sleep", lambda *_a, **_kw: None)

    from wifit3.chips.rt2800usb.constants import RT_RT5392
    from wifit3.chips.rt2800usb.rfcsr import init_rfcsr, rfcsr_read

    t = RfcsrFakeTransport()
    init_rfcsr(t, RT_RT5392)
    spot_checks = {
        1: 0x17, 3: 0x88, 6: 0xE0, 10: 0x53, 33: 0xC0,
        47: 0x0C, 56: 0xA1, 63: 0x07,
    }
    for word, expected in spot_checks.items():
        assert rfcsr_read(t, word) == expected, \
            f"RFCSR[{word}] = 0x{rfcsr_read(t, word):02x}, expected 0x{expected:02x}"


def test_init_rfcsr_5392_runs_normal_mode_setup(monkeypatch):
    """After init_rfcsr_5392 finishes, RFCSR38.RX_LO1_EN should be
    cleared and RFCSR30 should have RX_VCM = 2 (bits[4:3] = 0b10)."""
    import wifit3.chips.rt2800usb.rfcsr as rfm
    monkeypatch.setattr(rfm.time, "sleep", lambda *_a, **_kw: None)

    from wifit3.chips.rt2800usb.constants import RT_RT5392
    from wifit3.chips.rt2800usb.rfcsr import init_rfcsr, rfcsr_read

    t = RfcsrFakeTransport()
    init_rfcsr(t, RT_RT5392)
    rfcsr38 = rfcsr_read(t, 38)
    rfcsr30 = rfcsr_read(t, 30)
    assert not (rfcsr38 & 0x20), f"RFCSR38 RX_LO1_EN still set: 0x{rfcsr38:02x}"
    rx_vcm = (rfcsr30 & 0x18) >> 3
    assert rx_vcm == 2, f"RFCSR30 RX_VCM = {rx_vcm}, expected 2 (reg = 0x{rfcsr30:02x})"


def test_init_rfcsr_rejects_unsupported_silicon():
    import pytest
    from wifit3.chips.rt2800usb.constants import RT_RT5390
    from wifit3.chips.rt2800usb.rfcsr import init_rfcsr
    t = RfcsrFakeTransport()
    # RT5390 path isn't ported yet (NotImplementedError).
    # RT5592 was ported in M-B1; its routing is exercised by
    # test_init_rfcsr_dispatcher_routes_5592.
    with pytest.raises(NotImplementedError):
        init_rfcsr(t, RT_RT5390)


def test_init_rfcsr_3572_lays_down_kernel_table(monkeypatch):
    """Spot-check the RT3572 RFCSR init table [SRC rt2800lib.c:7907-7937]."""
    import wifit3.chips.rt2800usb.rfcsr as rfm
    monkeypatch.setattr(rfm.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(rfm, "_RX_FILTER_SETTLE_S", 0)   # skip the RX-filter-cal busy-wait

    from wifit3.chips.rt2800usb.constants import RT_RT3572
    from wifit3.chips.rt2800usb.rfcsr import init_rfcsr, rfcsr_read

    t = RfcsrFakeTransport()
    cal = init_rfcsr(t, RT_RT3572)
    # Sample entries from the 30-entry table. RFCSR24 + RFCSR31 are
    # overwritten by the rx_filter_calibration loop so we don't check them.
    expected = {
        0: 0x70, 1: 0x81, 2: 0xF1, 3: 0x02, 4: 0x4C,
        7: 0xD8, 14: 0xA0, 15: 0x53, 20: 0xB3,
        25: 0x15, 29: 0x9B, 30: 0x09,
    }
    for word, value in expected.items():
        actual = rfcsr_read(t, word)
        assert actual == value, \
            f"RFCSR[{word}] = 0x{actual:02x}, expected 0x{value:02x}"
    # RfFilterCal must be populated (init_rfcsr_3572 returns it).
    assert cal is not None
    # RT5392 path returns None to distinguish.
    from wifit3.chips.rt2800usb.constants import RT_RT5392
    t2 = RfcsrFakeTransport()
    assert init_rfcsr(t2, RT_RT5392) is None


def test_init_rfcsr_3572_sets_rfcsr6_r2_bit(monkeypatch):
    """init_rfcsr_3572 R-M-W's RFCSR6_R2 (bit 6) after the table write
    of 0x4A. 0x4A already has bit 6 set, so the visible result is 0x4A."""
    import wifit3.chips.rt2800usb.rfcsr as rfm
    monkeypatch.setattr(rfm.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(rfm, "_RX_FILTER_SETTLE_S", 0)   # skip the RX-filter-cal busy-wait

    from wifit3.chips.rt2800usb.constants import RT_RT3572
    from wifit3.chips.rt2800usb.rfcsr import init_rfcsr, rfcsr_read

    t = RfcsrFakeTransport()
    init_rfcsr(t, RT_RT3572)
    rfcsr6 = rfcsr_read(t, 6)
    assert rfcsr6 & 0x40, f"RFCSR[6] R2 bit not set: 0x{rfcsr6:02x}"


def test_init_rfcsr_3572_clears_rfcsr17_tx_lo1_en(monkeypatch):
    """normal_mode_setup_3xxx clears RFCSR17_TX_LO1_EN (bit 3) after
    the table writes RFCSR17=0x23. Expected after-state: 0x23 & ~0x08 = 0x23."""
    import wifit3.chips.rt2800usb.rfcsr as rfm
    monkeypatch.setattr(rfm.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(rfm, "_RX_FILTER_SETTLE_S", 0)   # skip the RX-filter-cal busy-wait

    from wifit3.chips.rt2800usb.constants import RT_RT3572
    from wifit3.chips.rt2800usb.rfcsr import init_rfcsr, rfcsr_read

    t = RfcsrFakeTransport()
    init_rfcsr(t, RT_RT3572)
    rfcsr17 = rfcsr_read(t, 17)
    assert not (rfcsr17 & 0x08), \
        f"RFCSR[17] TX_LO1_EN still set: 0x{rfcsr17:02x}"


# ----------------------------------------------------------------------
# M3 RX descriptor tests
# ----------------------------------------------------------------------
def _build_rt2800_rx_urb(frame_body: bytes, *, rssi_byte: int = 40, crc_error: bool = False,
                          mcs: int = 4) -> bytes:
    """Build a synthetic RX URB matching the kernel layout for RT539x:

      [RXINFO 4B] [RXWI 16B] [802.11 frame] [RXD 4B]

    Kernel docs say MPDU byte count includes a trailing 4-byte FCS, but
    live RT5572 / RT5372 diagnostics (see chips/rt2800usb/rx.py:168-181)
    showed the chip pre-strips FCS before the URB lands. Verified on
    PAU09 + PAU05 via PMKID harvest landing a clean 16-byte hash; the
    older `[:-4]` strip was clipping real EAPOL M1 payload. So this
    helper feeds frame_body as-is with no synthetic FCS appended — that's
    what `parse_rx_urb` actually sees on the wire.
    """
    import struct
    mpdu_len = len(frame_body)

    # rx_pkt_len covers RXWI + frame; doesn't include RXD.
    rxwi_size = 16
    rx_pkt_len = rxwi_size + mpdu_len

    rxinfo_w0 = rx_pkt_len & 0xFFFF
    rxinfo = struct.pack("<I", rxinfo_w0)

    rxwi_w0 = (mpdu_len & 0xFFF) << 16
    rxwi_w1 = (mcs & 0x7F) << 16
    rxwi_w2 = rssi_byte & 0xFF       # RSSI path 0 in low byte
    rxwi = struct.pack("<IIII", rxwi_w0, rxwi_w1, rxwi_w2, 0)

    rxd_w0 = 0
    if crc_error:
        rxd_w0 |= 0x100
    rxd = struct.pack("<I", rxd_w0)

    return rxinfo + rxwi + frame_body + rxd


def test_parse_rx_urb_decodes_trailer():
    """RX URB decode produces the 802.11 frame bytes verbatim — no
    synthetic FCS strip, since the chip pre-strips before delivery."""
    from wifit3.chips.rt2800usb.rx import parse_rx_urb
    body = b"\x80\x00" + b"\x00" * 22 + b"BEACON"   # 30-byte body
    urb = _build_rt2800_rx_urb(body, rssi_byte=40)
    rx = parse_rx_urb(urb)
    assert rx is not None
    assert rx.mpdu == body
    # RSSI: base_val (-12) - signed(40) = -52
    assert rx.rssi_dbm == -52
    assert rx.mcs == 4
    assert rx.has_fcs_error is False


def test_parse_rx_urb_handles_signed_rssi_byte():
    """Negative RSSI bytes (signed) should still produce sensible dBm."""
    from wifit3.chips.rt2800usb.rx import parse_rx_urb
    body = b"\x00" * 30
    # RSSI byte 0x80 = signed -128 → -12 - (-128) = +116 dBm (nonsense
    # but proves the sign extension works). Real chip values are 30-90.
    urb = _build_rt2800_rx_urb(body, rssi_byte=0x80)
    rx = parse_rx_urb(urb)
    assert rx is not None
    # The other two paths are 0 → -128; max picks the highest.
    assert rx.rssi_dbm > -128


def test_parse_rx_urb_returns_none_on_short_buffer():
    from wifit3.chips.rt2800usb.rx import parse_rx_urb
    assert parse_rx_urb(b"") is None
    assert parse_rx_urb(b"\x00" * 23) is None   # 1 short of 4+16+4 min


def test_parse_rx_urb_flags_crc_error():
    from wifit3.chips.rt2800usb.rx import parse_rx_urb
    body = b"\x00" * 30
    rx = parse_rx_urb(_build_rt2800_rx_urb(body, crc_error=True))
    assert rx is not None
    assert rx.has_fcs_error is True


def test_rxwi_size_for_silicon():
    from wifit3.chips.rt2800usb.constants import (
        RT_RT3572, RT_RT5390, RT_RT5392, RT_RT5592,
    )
    from wifit3.chips.rt2800usb.rx import rxwi_size_for_silicon
    assert rxwi_size_for_silicon(RT_RT5392) == 16
    assert rxwi_size_for_silicon(RT_RT5390) == 16
    assert rxwi_size_for_silicon(RT_RT3572) == 16
    assert rxwi_size_for_silicon(RT_RT5592) == 24   # 6-word RXWI


# ----------------------------------------------------------------------
# M4 set_channel tests
# ----------------------------------------------------------------------
def test_set_channel_rejects_out_of_range(monkeypatch):
    """Channels outside the rf_vals_3x table raise ValueError. 36 used
    to be rejected; M-A2 added it. Use channels that aren't in either
    band's kernel table (15, 50, 99, 142 — half-channels exist there
    so we pick truly unused numbers)."""
    import pytest
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import RT_RT3572, RT_RT5392
    from wifit3.chips.rt2800usb.rfcsr import RfFilterCal
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    cal = RfFilterCal(calibration_bw20=0x10, calibration_bw40=0x15,
                      bbp25=0x00, bbp26=0x00)
    t = RfcsrFakeTransport()
    for bad in (0, 15, 30, 99, 200, -1):
        with pytest.raises(ValueError):
            chan_mod.set_channel(t, RT_RT3572, bad, cal_result=cal)
    # RT5392 silicon rejects ANY 5 GHz channel (even table-valid ones).
    with pytest.raises(ValueError, match="2.4 GHz only"):
        chan_mod.set_channel(t, RT_RT5392, 36)


def test_set_channel_3572_5g_synth_table_channel_36(monkeypatch):
    """5 GHz channel 36 → rf1=0x56, rf2=0, rf3=4. RT3572 routes these
    to RFCSR 2/6_R1/3."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import RT_RT3572
    from wifit3.chips.rt2800usb.rfcsr import RfFilterCal, rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    cal = RfFilterCal(calibration_bw20=0x10, calibration_bw40=0x15,
                      bbp25=0x44, bbp26=0x55)
    chan_mod.set_channel(
        t, RT_RT3572, 36,
        cal_result=cal, tx_chain_num=2, rx_chain_num=2,
    )
    assert rfcsr_read(t, 2) == 0x56
    assert rfcsr_read(t, 3) == 4
    # RFCSR6.R1 should hold rf2 = 0; final RFCSR6 also has TXDIV (5G→1)
    rfcsr6 = rfcsr_read(t, 6)
    assert (rfcsr6 & 0x03) == 0, f"RFCSR6.R1 = {rfcsr6 & 0x03} (rf2 must be 0)"
    # RFCSR6.TXDIV = bits[3:2]; 5G → 1
    assert ((rfcsr6 & 0x0C) >> 2) == 1, f"RFCSR6.TXDIV bits = {(rfcsr6 & 0x0C) >> 2}"


def test_set_channel_3572_5g_band_dependent_r1_txdiv(monkeypatch):
    """RFCSR5.R1 and RFCSR6.TXDIV must flip per band."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import RT_RT3572
    from wifit3.chips.rt2800usb.rfcsr import RfFilterCal, rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    cal = RfFilterCal(calibration_bw20=0x10, calibration_bw40=0x15,
                      bbp25=0, bbp26=0)

    # 2.4G: R1=1, TXDIV=2
    t2g = RfcsrFakeTransport()
    chan_mod.set_channel(t2g, RT_RT3572, 1, cal_result=cal,
                         tx_chain_num=2, rx_chain_num=2)
    rfcsr5_2g = rfcsr_read(t2g, 5)
    rfcsr6_2g = rfcsr_read(t2g, 6)
    assert ((rfcsr5_2g & 0x0C) >> 2) == 1, "2.4G RFCSR5.R1 should be 1"
    assert ((rfcsr6_2g & 0x0C) >> 2) == 2, "2.4G RFCSR6.TXDIV should be 2"

    # 5G: R1=2, TXDIV=1
    t5g = RfcsrFakeTransport()
    chan_mod.set_channel(t5g, RT_RT3572, 36, cal_result=cal,
                         tx_chain_num=2, rx_chain_num=2)
    rfcsr5_5g = rfcsr_read(t5g, 5)
    rfcsr6_5g = rfcsr_read(t5g, 6)
    assert ((rfcsr5_5g & 0x0C) >> 2) == 2, "5G RFCSR5.R1 should be 2"
    assert ((rfcsr6_5g & 0x0C) >> 2) == 1, "5G RFCSR6.TXDIV should be 1"


def test_set_channel_3572_5g_subband_unii1(monkeypatch):
    """ch <= 64 sub-band (UNII-1/2): RFCSR19=0xB7, 20=0xF6, 25=0x3D."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import RT_RT3572
    from wifit3.chips.rt2800usb.rfcsr import RfFilterCal, rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    cal = RfFilterCal(calibration_bw20=0x10, calibration_bw40=0x15,
                      bbp25=0, bbp26=0)
    t = RfcsrFakeTransport()
    chan_mod.set_channel(t, RT_RT3572, 48, cal_result=cal,
                         tx_chain_num=2, rx_chain_num=2)
    assert rfcsr_read(t, 19) == 0xB7
    assert rfcsr_read(t, 20) == 0xF6
    assert rfcsr_read(t, 25) == 0x3D
    assert rfcsr_read(t, 26) == 0x87
    assert rfcsr_read(t, 29) == 0x9F


def test_set_channel_3572_5g_subband_hyperlan(monkeypatch):
    """64 < ch <= 128 sub-band: RFCSR19=0x74, 20=0xF4, 25=0x01."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import RT_RT3572
    from wifit3.chips.rt2800usb.rfcsr import RfFilterCal, rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    cal = RfFilterCal(calibration_bw20=0x10, calibration_bw40=0x15,
                      bbp25=0, bbp26=0)
    t = RfcsrFakeTransport()
    chan_mod.set_channel(t, RT_RT3572, 100, cal_result=cal,
                         tx_chain_num=2, rx_chain_num=2)
    assert rfcsr_read(t, 19) == 0x74
    assert rfcsr_read(t, 20) == 0xF4
    assert rfcsr_read(t, 25) == 0x01


def test_set_channel_3572_5g_subband_unii3(monkeypatch):
    """ch > 128 sub-band: RFCSR19=0x72, 20=0xF3, 25=0x01."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import RT_RT3572
    from wifit3.chips.rt2800usb.rfcsr import RfFilterCal, rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    cal = RfFilterCal(calibration_bw20=0x10, calibration_bw40=0x15,
                      bbp25=0, bbp26=0)
    t = RfcsrFakeTransport()
    chan_mod.set_channel(t, RT_RT3572, 149, cal_result=cal,
                         tx_chain_num=2, rx_chain_num=2)
    assert rfcsr_read(t, 19) == 0x72
    assert rfcsr_read(t, 20) == 0xF3
    assert rfcsr_read(t, 25) == 0x01


def test_set_channel_3572_5g_writes_bbp82_0x94(monkeypatch):
    """RT3572 5 GHz post-RF BBP82 = 0x94 (vs 0x84 for 2.4 GHz)."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.bbp import bbp_read
    from wifit3.chips.rt2800usb.constants import RT_RT3572
    from wifit3.chips.rt2800usb.rfcsr import RfFilterCal
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    cal = RfFilterCal(calibration_bw20=0x10, calibration_bw40=0x15,
                      bbp25=0, bbp26=0)

    t5g = RfcsrFakeTransport()
    chan_mod.set_channel(t5g, RT_RT3572, 36, cal_result=cal,
                         tx_chain_num=2, rx_chain_num=2)
    assert bbp_read(t5g, 82) == 0x94

    t2g = RfcsrFakeTransport()
    chan_mod.set_channel(t2g, RT_RT3572, 1, cal_result=cal,
                         tx_chain_num=2, rx_chain_num=2)
    assert bbp_read(t2g, 82) == 0x84


def test_set_channel_3572_2g_bbp82_75_honor_external_lna_bg(monkeypatch):
    """RT3572 2.4 GHz RX-AGC front-end coefficients branch on
    has_cap_external_lna_bg (NIC_CONF1.EXTERNAL_LNA_2G): an external 2.4 GHz
    LNA card writes BBP82=0x62 + BBP75=0x46; the internal-LNA default writes
    BBP82=0x84 + BBP75=0x50. [SRC] rt2800lib.c:4312-4322. [WIRE] the
    AWUS051NH v2 takes the external-LNA branch (captures_rt3572_tx_diff/
    aireplay.pcap: BBP82=0x62 ×2 + BBP75=0x46 on every 2.4 GHz tune)."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.bbp import bbp_read
    from wifit3.chips.rt2800usb.constants import RT_RT3572
    from wifit3.chips.rt2800usb.rfcsr import RfFilterCal
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    cal = RfFilterCal(calibration_bw20=0x10, calibration_bw40=0x15,
                      bbp25=0, bbp26=0)

    t_ext = RfcsrFakeTransport()
    chan_mod.set_channel(t_ext, RT_RT3572, 1, cal_result=cal,
                         tx_chain_num=1, rx_chain_num=1,
                         has_cap_external_lna_bg=True)
    assert bbp_read(t_ext, 82) == 0x62
    assert bbp_read(t_ext, 75) == 0x46

    t_int = RfcsrFakeTransport()
    chan_mod.set_channel(t_int, RT_RT3572, 1, cal_result=cal,
                         tx_chain_num=1, rx_chain_num=1,
                         has_cap_external_lna_bg=False)
    assert bbp_read(t_int, 82) == 0x84
    assert bbp_read(t_int, 75) == 0x50


def test_set_channel_3572_5g_bbp25_26_hardcoded(monkeypatch):
    """5 GHz hardcodes BBP25 = 0x09, BBP26 = 0xFF (IQ phase correction);
    2.4 GHz restores from cal_result.bbp25/26."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.bbp import bbp_read
    from wifit3.chips.rt2800usb.constants import RT_RT3572
    from wifit3.chips.rt2800usb.rfcsr import RfFilterCal
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    cal = RfFilterCal(calibration_bw20=0x10, calibration_bw40=0x15,
                      bbp25=0x44, bbp26=0x55)

    t5g = RfcsrFakeTransport()
    chan_mod.set_channel(t5g, RT_RT3572, 36, cal_result=cal,
                         tx_chain_num=2, rx_chain_num=2)
    assert bbp_read(t5g, 25) == 0x09
    assert bbp_read(t5g, 26) == 0xFF

    t2g = RfcsrFakeTransport()
    chan_mod.set_channel(t2g, RT_RT3572, 1, cal_result=cal,
                         tx_chain_num=2, rx_chain_num=2)
    assert bbp_read(t2g, 25) == 0x44
    assert bbp_read(t2g, 26) == 0x55


def test_set_channel_3572_5g_agc_formula(monkeypatch):
    """5 GHz AGC: BBP66 = 0x22 + (lna_gain * 5) // 3.
    2.4 GHz: BBP66 = 0x1C + 2*lna_gain."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.bbp import bbp_read
    from wifit3.chips.rt2800usb.constants import RT_RT3572
    from wifit3.chips.rt2800usb.rfcsr import RfFilterCal
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    cal = RfFilterCal(calibration_bw20=0x10, calibration_bw40=0x15,
                      bbp25=0, bbp26=0)

    # lna_gain = 0 → 5G BBP66 = 0x22, 2.4G BBP66 = 0x1C
    t5g = RfcsrFakeTransport()
    chan_mod.set_channel(t5g, RT_RT3572, 36, cal_result=cal,
                         lna_gain=0, tx_chain_num=2, rx_chain_num=2)
    assert bbp_read(t5g, 66) == 0x22

    t2g = RfcsrFakeTransport()
    chan_mod.set_channel(t2g, RT_RT3572, 1, cal_result=cal,
                         lna_gain=0, tx_chain_num=2, rx_chain_num=2)
    assert bbp_read(t2g, 66) == 0x1C

    # lna_gain = 6 → 5G BBP66 = 0x22 + (6*5)//3 = 0x22 + 10 = 0x2C
    #              → 2.4G BBP66 = 0x1C + 2*6 = 0x28
    t5g2 = RfcsrFakeTransport()
    chan_mod.set_channel(t5g2, RT_RT3572, 36, cal_result=cal,
                         lna_gain=6, tx_chain_num=2, rx_chain_num=2)
    assert bbp_read(t5g2, 66) == 0x2C

    t2g2 = RfcsrFakeTransport()
    chan_mod.set_channel(t2g2, RT_RT3572, 1, cal_result=cal,
                         lna_gain=6, tx_chain_num=2, rx_chain_num=2)
    assert bbp_read(t2g2, 66) == 0x28


def test_set_channel_3572_5g_rfcsr7_bits_set(monkeypatch):
    """5 GHz RMWs RFCSR7: BIT2 + BIT4 set, BIT3 + BITS67 cleared.
    Starting from 2.4G's hardcoded 0xD8 (set by a prior 2.4G call),
    the 5G branch transforms it to (0xD8 & ~(BIT3|BITS67)) | (BIT2|BIT4)
    = (0xD8 & ~(0x08 | 0xC0)) | (0x04 | 0x10) = 0x10 | 0x14 = 0x14.

    Then the channel-tune kick adds RF_TUNING (bit 0) → 0x15."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import RT_RT3572
    from wifit3.chips.rt2800usb.rfcsr import RfFilterCal, rfcsr_read, rfcsr_write
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    cal = RfFilterCal(calibration_bw20=0x10, calibration_bw40=0x15,
                      bbp25=0, bbp26=0)

    t = RfcsrFakeTransport()
    rfcsr_write(t, 7, 0xD8)            # simulate post-2.4G state
    chan_mod.set_channel(t, RT_RT3572, 36, cal_result=cal,
                         tx_chain_num=2, rx_chain_num=2)
    rfcsr7 = rfcsr_read(t, 7)
    # Bits set: BIT2 (0x04), BIT4 (0x10), RF_TUNING (0x01) — the kick.
    assert rfcsr7 & 0x04, f"RFCSR7.BIT2 not set: 0x{rfcsr7:02x}"
    assert rfcsr7 & 0x10, f"RFCSR7.BIT4 not set: 0x{rfcsr7:02x}"
    # Bits cleared: BIT3 (0x08), BITS67 (0xC0).
    assert not (rfcsr7 & 0x08), f"RFCSR7.BIT3 still set: 0x{rfcsr7:02x}"
    assert not (rfcsr7 & 0xC0), f"RFCSR7.BITS67 still set: 0x{rfcsr7:02x}"


def test_set_channel_3572_5g_gpio_ctrl_val7_clear(monkeypatch):
    """GPIO_CTRL bit 7 (band switch): 1 for 2.4G, 0 for 5G."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import GPIO_CTRL, GPIO_CTRL_VAL7, RT_RT3572
    from wifit3.chips.rt2800usb.rfcsr import RfFilterCal
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    cal = RfFilterCal(calibration_bw20=0x10, calibration_bw40=0x15,
                      bbp25=0, bbp26=0)

    t5g = RfcsrFakeTransport()
    chan_mod.set_channel(t5g, RT_RT3572, 36, cal_result=cal,
                         tx_chain_num=2, rx_chain_num=2)
    assert not (t5g.read32(GPIO_CTRL) & GPIO_CTRL_VAL7), "5G must clear VAL7"

    t2g = RfcsrFakeTransport()
    chan_mod.set_channel(t2g, RT_RT3572, 1, cal_result=cal,
                         tx_chain_num=2, rx_chain_num=2)
    assert t2g.read32(GPIO_CTRL) & GPIO_CTRL_VAL7, "2.4G must set VAL7"


def test_set_channel_3572_5g_tx_pin_uses_a_pa(monkeypatch):
    """5 GHz TX_PIN_CFG: PA_PE_A0_EN (bit 0) for primary, PA_PE_A1_EN
    (bit 2) for secondary. 2.4G uses G0/G1 instead."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import (
        RT_RT3572, TX_PIN_CFG_REG,
        TX_PIN_CFG_PA_PE_A0_EN_BIT, TX_PIN_CFG_PA_PE_A1_EN,
        TX_PIN_CFG_PA_PE_G0_EN_BIT, TX_PIN_CFG_PA_PE_G1_EN,
    )
    from wifit3.chips.rt2800usb.rfcsr import RfFilterCal
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    cal = RfFilterCal(calibration_bw20=0x10, calibration_bw40=0x15,
                      bbp25=0, bbp26=0)

    # 5 GHz, 2T2R
    t5g = RfcsrFakeTransport()
    chan_mod.set_channel(t5g, RT_RT3572, 36, cal_result=cal,
                         tx_chain_num=2, rx_chain_num=2)
    tx_pin_5g = t5g.read32(TX_PIN_CFG_REG)
    assert tx_pin_5g & TX_PIN_CFG_PA_PE_A0_EN_BIT, "5G must set PA_PE_A0_EN"
    assert tx_pin_5g & TX_PIN_CFG_PA_PE_A1_EN, "5G/2T must set PA_PE_A1_EN"
    assert not (tx_pin_5g & TX_PIN_CFG_PA_PE_G0_EN_BIT), "5G must NOT set PA_PE_G0_EN"
    assert not (tx_pin_5g & TX_PIN_CFG_PA_PE_G1_EN), "5G must NOT set PA_PE_G1_EN"

    # 2.4 GHz, 2T2R
    t2g = RfcsrFakeTransport()
    chan_mod.set_channel(t2g, RT_RT3572, 1, cal_result=cal,
                         tx_chain_num=2, rx_chain_num=2)
    tx_pin_2g = t2g.read32(TX_PIN_CFG_REG)
    assert tx_pin_2g & TX_PIN_CFG_PA_PE_G0_EN_BIT, "2.4G must set PA_PE_G0_EN"
    assert tx_pin_2g & TX_PIN_CFG_PA_PE_G1_EN, "2.4G/2T must set PA_PE_G1_EN"
    assert not (tx_pin_2g & TX_PIN_CFG_PA_PE_A0_EN_BIT), "2.4G must NOT set PA_PE_A0_EN"
    assert not (tx_pin_2g & TX_PIN_CFG_PA_PE_A1_EN), "2.4G must NOT set PA_PE_A1_EN"


def test_set_channel_3572_5g_tx_band_cfg(monkeypatch):
    """TX_BAND_CFG: 5G sets A bit, clears BG bit. 2.4G is reversed."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import (
        RT_RT3572, TX_BAND_CFG_A, TX_BAND_CFG_BG_BIT, TX_BAND_CFG_REG,
    )
    from wifit3.chips.rt2800usb.rfcsr import RfFilterCal
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    cal = RfFilterCal(calibration_bw20=0x10, calibration_bw40=0x15,
                      bbp25=0, bbp26=0)

    t5g = RfcsrFakeTransport()
    chan_mod.set_channel(t5g, RT_RT3572, 36, cal_result=cal,
                         tx_chain_num=2, rx_chain_num=2)
    band_5g = t5g.read32(TX_BAND_CFG_REG)
    assert band_5g & TX_BAND_CFG_A
    assert not (band_5g & TX_BAND_CFG_BG_BIT)

    t2g = RfcsrFakeTransport()
    chan_mod.set_channel(t2g, RT_RT3572, 1, cal_result=cal,
                         tx_chain_num=2, rx_chain_num=2)
    band_2g = t2g.read32(TX_BAND_CFG_REG)
    assert not (band_2g & TX_BAND_CFG_A)
    assert band_2g & TX_BAND_CFG_BG_BIT


def test_set_channel_3572_5g_freq_offset_to_rfcsr23(monkeypatch):
    """Sanity-check: freq_offset arg still lands in RFCSR23 low 7 bits
    on the 5 GHz path (the bug-prone area highlighted in the M-A2
    handoff doc — easy to miss if the 5G branch shadows the RFCSR23
    write)."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import RT_RT3572
    from wifit3.chips.rt2800usb.rfcsr import RfFilterCal, rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    cal = RfFilterCal(calibration_bw20=0x10, calibration_bw40=0x15,
                      bbp25=0, bbp26=0)
    t = RfcsrFakeTransport()
    chan_mod.set_channel(t, RT_RT3572, 36, cal_result=cal,
                         freq_offset=53, tx_chain_num=2, rx_chain_num=2)
    assert (rfcsr_read(t, 23) & 0x7F) == 53


def test_eeprom_unburned_default_is_60():
    """Pcap-derived value: kernel-pcap (captures_rt2800usb_rt3572) shows
    RFCSR23 channel-tune writes = 0x35 (53 decimal) on a burned dongle.
    Sweep on user's unburned dongle peaks at 60. We pick 60 as the
    'sweep-peak' default — see eeprom.py module comment for rationale."""
    from wifit3.chips.rt2800usb.eeprom import (
        UNBURNED_FREQ_OFFSET_DEFAULT, parse_eeprom,
    )
    assert UNBURNED_FREQ_OFFSET_DEFAULT == 60

    # parse_eeprom should apply the default for both 0x00 and 0xFF FREQ
    # low bytes (NOT just the kernel-checked 0xFFFF).
    buf = bytearray(0x200)
    # FREQ word at offset 0x1D × 2 = 0x3A.
    buf[0x3A] = 0x00
    buf[0x3B] = 0x00
    ee = parse_eeprom(bytes(buf))
    assert ee.freq_offset == 60

    buf[0x3A] = 0xFF
    ee = parse_eeprom(bytes(buf))
    assert ee.freq_offset == 60

    # A non-empty value passes through.
    buf[0x3A] = 0x35
    ee = parse_eeprom(bytes(buf))
    assert ee.freq_offset == 0x35


def test_eeprom_nic_conf0_0x0f0f_treated_as_unburned():
    """PAU09 N600 EFUSE returns NIC_CONF0=0x0F0F (txpath=0, rxpath=15 —
    both physically impossible). Must apply the kernel default of
    1 TX / 1 RX so set_channel matches the wire (RFCSR1=0xf1)."""
    from wifit3.chips.rt2800usb.eeprom import parse_eeprom
    buf = bytearray(0x200)
    # NIC_CONF0 at word 0x1A → byte 0x34.
    buf[0x34] = 0x0F
    buf[0x35] = 0x0F
    ee = parse_eeprom(bytes(buf))
    assert ee.txpath == 1, "0x0F0F should default txpath to 1, not 0"
    assert ee.rxpath == 1, "0x0F0F should default rxpath to 1, not 15"


def test_eeprom_nic_conf0_impossible_values_treated_as_unburned():
    """Any NIC_CONF0 with txpath==0 or rxpath > 3 is by definition
    unburned (max physical chain count is 3T3R)."""
    from wifit3.chips.rt2800usb.eeprom import parse_eeprom

    def _ee(nc0: int):
        buf = bytearray(0x200)
        buf[0x34] = nc0 & 0xFF
        buf[0x35] = (nc0 >> 8) & 0xFF
        return parse_eeprom(bytes(buf))

    # txpath=0 (impossible)
    ee = _ee(0x0005)   # rxpath=5, txpath=0 — both invalid
    assert ee.txpath == 1
    assert ee.rxpath == 1

    # rxpath > 3 (impossible)
    ee = _ee(0x0204)   # txpath=2, rxpath=4
    assert ee.txpath == 1
    assert ee.rxpath == 1

    # Legit 2T2R passes through unchanged.
    ee = _ee(0x0022)   # txpath=2, rxpath=2
    assert ee.txpath == 2
    assert ee.rxpath == 2


# ---- RF-chip identification (kernel rt2800_init_eeprom) ---------------------
def test_eeprom_rf_type_decodes_nic_conf0_bits_11_8():
    """NIC_CONF0.RF_TYPE = FIELD16(0x0f00) — the RF-chip nibble a burned RT3572
    EEPROM encodes (RF3052 = 0x9), independent of the antenna low byte."""
    from wifit3.chips.rt2800usb.eeprom import RF3052, parse_eeprom
    buf = bytearray(0x200)
    # NIC_CONF0 word 0x1A → byte 0x34/0x35; RF3052 (0x9 in the high nibble) + 2T2R.
    buf[0x34] = 0x22   # txpath=2, rxpath=2
    buf[0x35] = 0x09   # RF_TYPE nibble = RF3052
    ee = parse_eeprom(bytes(buf))
    assert ee.rf_type == RF3052
    assert ee.txpath == 2 and ee.rxpath == 2


def test_resolve_rf_chip_rt3572_burned_is_rf3052_ported():
    from wifit3.chips.rt2800usb.eeprom import RF3052, parse_eeprom, resolve_rf_chip
    buf = bytearray(0x200)
    buf[0x34] = 0x22   # 2T2R
    buf[0x35] = 0x09   # RF3052
    rf = resolve_rf_chip(RT_RT3572, parse_eeprom(bytes(buf)))
    assert rf.rf_id == RF3052
    assert rf.name == "RF3052"
    assert rf.ported is True


def test_resolve_rf_chip_rt3572_unburned_gives_zero_not_fail():
    """Reference AWUS051NH v2: unburned NIC_CONF0=0x0000 → RF_TYPE 0. Kernel
    would -ENODEV; we return rf_id=0 (ported=False) and the caller runs the
    silicon default (RF3052) so the erased-EEPROM dongle still comes up."""
    from wifit3.chips.rt2800usb.eeprom import parse_eeprom, resolve_rf_chip
    rf = resolve_rf_chip(RT_RT3572, parse_eeprom(bytes(0x200)))
    assert rf.rf_id == 0
    assert rf.ported is False


def test_resolve_rf_chip_rt5392_reads_chip_id_word():
    """RT5390/RT5392 silicon take the RF id from EEPROM_CHIP_ID (word 0), not
    NIC_CONF0.RF_TYPE. [SRC] rt2800lib.c:11187-11191."""
    from wifit3.chips.rt2800usb.eeprom import RF5392, parse_eeprom, resolve_rf_chip
    buf = bytearray(0x200)
    buf[0x00] = 0x92   # EEPROM_CHIP_ID = 0x5392 (RF5392)
    buf[0x01] = 0x53
    rf = resolve_rf_chip(RT_RT5392, parse_eeprom(bytes(buf)))
    assert rf.rf_id == RF5392
    assert rf.ported is True


def test_resolve_rf_chip_rt5592_hardcoded_rf5592():
    """RT5592 silicon hardcodes RF5592 regardless of EEPROM contents.
    [SRC] rt2800lib.c:11198-11199."""
    from wifit3.chips.rt2800usb.eeprom import RF5592, parse_eeprom, resolve_rf_chip
    rf = resolve_rf_chip(RT_RT5592, parse_eeprom(bytes(0x200)))
    assert rf.rf_id == RF5592
    assert rf.ported is True


def test_resolve_rf_chip_unknown_rf_marked_unported():
    """A burned RT3572 EEPROM claiming an RF the port has no tune path for
    (RF3022 = 0x8) is flagged unported, not crashed — the driver still runs the
    silicon default and logs an 'untested variant' warning."""
    from wifit3.chips.rt2800usb.eeprom import RF3022, parse_eeprom, resolve_rf_chip
    buf = bytearray(0x200)
    buf[0x34] = 0x22   # 2T2R
    buf[0x35] = 0x08   # RF3022 nibble — not a ported path
    rf = resolve_rf_chip(RT_RT3572, parse_eeprom(bytes(buf)))
    assert rf.rf_id == RF3022
    assert rf.ported is False


def test_eeprom_exposes_lna_gain_a_and_capabilities():
    """M-A2: per-band LNA + NIC_CONF1 capability flags must be plumbed
    so _channel_kwargs() can hand the right values to set_channel."""
    from wifit3.chips.rt2800usb.eeprom import parse_eeprom
    buf = bytearray(0x200)
    # LNA word at offset 0x22 × 2 = 0x44.
    buf[0x44] = 0x10    # lna_bg
    buf[0x45] = 0x20    # lna_a (high byte of LNA word)
    # NIC_CONF1 at offset 0x1B × 2 = 0x36. Kernel EEPROM_NIC_CONF1 field bits
    # (rt2800.h:2706-2720): bit 2 = EXTERNAL_LNA_2G (bg), bit 3 = EXTERNAL_LNA_5G
    # (a), bit 14 = BT_COEXIST. (The old port read bits 8/9/13 = BW40M_2G/5G /
    # INTERNAL_TX_ALC — which mis-decoded RT5572's external LNA-BG as absent.)
    nic1 = (1 << 3) | (1 << 14)  # external_lna_a + bt_coexist, NOT external_lna_bg
    buf[0x36] = nic1 & 0xFF
    buf[0x37] = (nic1 >> 8) & 0xFF
    # FREQ word — non-empty so default doesn't fire.
    buf[0x3A] = 0x35
    buf[0x3B] = 0x00

    ee = parse_eeprom(bytes(buf))
    assert ee.lna_gain_bg == 0x10
    assert ee.lna_gain_a == 0x20
    assert ee.has_cap_bt_coexist is True
    assert ee.has_cap_external_lna_a is True
    assert ee.has_cap_external_lna_bg is False
    # bit 2 (EXTERNAL_LNA_2G) set → external_lna_bg True: this is the RT5572's
    # BBP82=0x62 (twice) / BBP75=0x46 tune path.
    buf[0x36] = ((1 << 2) | (1 << 3)) & 0xFF
    buf[0x37] = 0x00
    ee2 = parse_eeprom(bytes(buf))
    assert ee2.has_cap_external_lna_bg is True
    assert ee2.has_cap_external_lna_a is True
    assert ee2.has_cap_bt_coexist is False


def test_eeprom_txmixer_gain_decode_and_fallback():
    """RFCSR16.TXMIXER_GAIN source: EEPROM words 0x24 (2.4 GHz) / 0x26 (5 GHz),
    bits[2:0], with the kernel's low-byte-0xff -> 0 fallback. This is the field
    that was pinned to 0, killing 2.4 GHz TX on the burned-mixer/unburned-conf0
    AWUS051NH v2. [SRC] rt2800lib.c:10996 / 11011."""
    from wifit3.chips.rt2800usb.eeprom import parse_eeprom
    # AWUS051NH v2 profile: word 0x24 = 0x0004, word 0x26 = 0x0002.
    buf = bytearray(0x200)
    buf[0x24 * 2] = 0x04    # 24g low byte -> gain bits[2:0] = 4
    buf[0x24 * 2 + 1] = 0x00
    buf[0x26 * 2] = 0x02    # 5g low byte -> gain 2
    buf[0x26 * 2 + 1] = 0x00
    ee = parse_eeprom(bytes(buf))
    assert ee.txmixer_gain_bg == 4
    assert ee.txmixer_gain_a == 2

    # Only bits[2:0] belong to TXMIXER_GAIN (the word overlaps RSSI_BG2/A2).
    buf[0x24 * 2] = 0x2C    # low byte 0x2c -> bits[2:0] = 4
    ee = parse_eeprom(bytes(buf))
    assert ee.txmixer_gain_bg == 4

    # Genuinely-unburned low byte 0xff -> 0 (kernel fallback).
    buf[0x24 * 2] = 0xFF
    ee = parse_eeprom(bytes(buf))
    assert ee.txmixer_gain_bg == 0

    # A hand-built EepromValues with no raw dump -> 0 (no EEPROM to read).
    from wifit3.chips.rt2800usb.eeprom import EepromValues
    bare = EepromValues(
        mac_address=b"\x00" * 6, nic_conf0=0, nic_conf1=0, freq_offset=0,
        lna_gain_bg=0, lna_gain_a=0, rssi_bg_offset0=0, rssi_bg_offset1=0,
    )
    assert bare.txmixer_gain_bg == 0
    assert bare.txmixer_gain_a == 0


def test_set_channel_3572_2g_rfcsr16_honors_txmixer_gain(monkeypatch):
    """RF3052 2.4 GHz tune: RFCSR16 base 0x4c with TXMIXER_GAIN (bits[2:0]) set
    from the EEPROM. gain 4 -> 0x4c (matches the in-tree capture); gain 0 ->
    0x48 (the old pinned-to-0 bug that zeroed the 2.4 GHz TX mixer gain).
    [SRC] rt2800lib.c:2739-2742."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import RT_RT3572
    from wifit3.chips.rt2800usb.rfcsr import RfFilterCal, rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    cal = RfFilterCal(calibration_bw20=0x10, calibration_bw40=0x15, bbp25=0x44, bbp26=0x55)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(t, RT_RT3572, 1, cal_result=cal,
                         tx_chain_num=1, rx_chain_num=1, txmixer_gain_24g=4)
    assert rfcsr_read(t, 16) == 0x4C

    t0 = RfcsrFakeTransport()
    chan_mod.set_channel(t0, RT_RT3572, 1, cal_result=cal,
                         tx_chain_num=1, rx_chain_num=1, txmixer_gain_24g=0)
    assert rfcsr_read(t0, 16) == 0x48


def test_set_channel_3572_5g_rfcsr16_honors_txmixer_gain(monkeypatch):
    """RF3052 5 GHz tune: RFCSR16 base 0x7a with TXMIXER_GAIN from EEPROM.
    gain 2 -> 0x7a. [SRC] rt2800lib.c:2761-2764."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import RT_RT3572
    from wifit3.chips.rt2800usb.rfcsr import RfFilterCal, rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    cal = RfFilterCal(calibration_bw20=0x10, calibration_bw40=0x15, bbp25=0x44, bbp26=0x55)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(t, RT_RT3572, 36, cal_result=cal,
                         tx_chain_num=1, rx_chain_num=1, txmixer_gain_5g=2)
    assert rfcsr_read(t, 16) == 0x7A


def test_set_channel_rejects_unsupported_silicon(monkeypatch):
    """An unknown silicon ID raises NotImplementedError. (RT5592 was
    ported in M-B1 — exercised by test_set_channel_5592_2g_* below.)"""
    import pytest
    import wifit3.chips.rt2800usb.chan as chan_mod
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    t = RfcsrFakeTransport()
    with pytest.raises(NotImplementedError):
        chan_mod.set_channel(t, 0xDEAD, 1)


def test_set_channel_3572_writes_rfcsr2_for_channel_1(monkeypatch):
    """RT3572 uses RFCSR2 (not RFCSR8) for the synthesizer N value;
    channel 1 → rf1=241 → RFCSR2 = 241."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import RT_RT3572
    from wifit3.chips.rt2800usb.rfcsr import RfFilterCal, rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    cal = RfFilterCal(calibration_bw20=0x10, calibration_bw40=0x15, bbp25=0x44, bbp26=0x55)
    chan_mod.set_channel(
        t, RT_RT3572, 1,
        cal_result=cal, tx_chain_num=2, rx_chain_num=2,
    )
    assert rfcsr_read(t, 2) == 241
    # RFCSR8 should NOT be the synth N for RT3572 — instead it's used
    # for the RT3572-only AGC kick (final write = 0x80).
    assert rfcsr_read(t, 8) == 0x80
    # Calibration_bw20 replays into RFCSR24 + RFCSR31.
    assert rfcsr_read(t, 24) == 0x10
    assert rfcsr_read(t, 31) == 0x10


def test_set_channel_3572_requires_cal_result(monkeypatch):
    """The RT3572 path needs the filter calibration captured at init
    time; a None cal_result should raise."""
    import pytest
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import RT_RT3572
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    t = RfcsrFakeTransport()
    with pytest.raises(ValueError, match="cal_result"):
        chan_mod.set_channel(t, RT_RT3572, 1)


def test_set_channel_writes_rfcsr8_for_channel_1(monkeypatch):
    """Channel 1 → rf1=241 → RFCSR8 = 241."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import RT_RT5392
    from wifit3.chips.rt2800usb.rfcsr import rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    # Skip the MCU freq cal request (needs H2M_MAILBOX_CSR plumbing).
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(t, RT_RT5392, 1)
    assert rfcsr_read(t, 8) == 241


def test_set_channel_writes_correct_synth_for_each_2g_channel(monkeypatch):
    """Spot-check rf1/rf2/rf3 values from the rf_vals_3x table land in
    RFCSR 8/9/11."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import RT_RT5392
    from wifit3.chips.rt2800usb.rfcsr import rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    # (channel, expected_rf1, expected_rf2, expected_rf3)
    cases = [
        (1, 241, 2, 2),
        (6, 243, 2, 7),
        (11, 246, 2, 2),
        (13, 247, 2, 2),
        (14, 248, 2, 4),
    ]
    for ch, rf1, rf2, rf3 in cases:
        t = RfcsrFakeTransport()
        chan_mod.set_channel(t, RT_RT5392, ch)
        assert rfcsr_read(t, 8) == rf1, f"ch={ch}: RFCSR8 = 0x{rfcsr_read(t, 8):02x}"
        assert rfcsr_read(t, 9) == rf3, f"ch={ch}: RFCSR9 = 0x{rfcsr_read(t, 9):02x}"
        assert (rfcsr_read(t, 11) & 0x03) == rf2, f"ch={ch}: RFCSR11.R wrong"


# ----------------------------------------------------------------------
# M5 TX inject tests
# ----------------------------------------------------------------------
def test_build_tx_descriptors_default_shape():
    """For a 26-byte deauth + use_no_ack=True + MCS=0/CCK, the descriptors
    mirror kernel rt2800usb aireplay-ng deauth TXWI (verified against
    driver_captures/captures_rt2800usb_rt3572/capture-1.pcap frame 43087):
    MPDU byte count = 26, ACK = 0, NSEQ = 0, WCID = 0, WIV = 1,
    QSEL = EDCA (2), TX_OP = HT_TXOP_NONE (3), PACKETID_QUEUE = 0,
    PACKETID_ENTRY = 2."""
    import struct
    from wifit3.chips.rt2800usb.tx import build_tx_descriptors
    desc = build_tx_descriptors(26, txwi_size=16, use_no_ack=True)
    assert len(desc) == 4 + 16  # TXINFO + TXWI

    txinfo_w0, txwi_w0, txwi_w1, txwi_w2, txwi_w3 = struct.unpack("<5I", desc)
    # TXINFO: pkt_len = TXWI(16) + aligned(26→28) = 44; WIV=1; QSEL=2(EDCA)
    assert (txinfo_w0 & 0xFFFF) == 44
    assert txinfo_w0 & (1 << 24), "WIV should be set"
    # EDCA qsel = 2 → bits[26:25] = 2
    assert ((txinfo_w0 >> 25) & 0x3) == 2
    # TXWI_W0: MCS=0, PHYMODE=CCK(0), TX_OP=HT_TXOP_NONE(3) — bits[9:8] = 3
    assert ((txwi_w0 >> 8) & 0x3) == 3, "TX_OP should be HT_TXOP_NONE"
    assert ((txwi_w0 >> 16) & 0x7F) == 0, "MCS should be 0"
    assert ((txwi_w0 >> 30) & 0x3) == 0, "PHYMODE should be CCK"
    # TXWI_W1: ACK=0, NSEQ=0, WCID=0, MPDU=26, QID=0, ENTRY=2 — matches kernel
    assert (txwi_w1 & 1) == 0, "ACK should be 0 for use_no_ack"
    assert (txwi_w1 >> 1) & 1 == 0, "NSEQ should be 0 (use seqctl from frame)"
    assert ((txwi_w1 >> 8) & 0xFF) == 0, "WCID should be 0 (kernel broadcast slot)"
    assert ((txwi_w1 >> 16) & 0xFFF) == 26, "MPDU_TOTAL_BYTE_COUNT should be 26"
    assert ((txwi_w1 >> 28) & 0x3) == 0, "PACKETID_QUEUE should be 0"
    assert ((txwi_w1 >> 30) & 0x3) == 2, "PACKETID_ENTRY should be 2"
    # TXWI W2/W3 = 0 (no encryption IV)
    assert txwi_w2 == 0
    assert txwi_w3 == 0


def test_build_tx_descriptors_use_ack_sets_ack_bit():
    import struct
    from wifit3.chips.rt2800usb.tx import build_tx_descriptors
    desc = build_tx_descriptors(26, txwi_size=16, use_no_ack=False)
    _, _, txwi_w1, _, _ = struct.unpack("<5I", desc)
    assert (txwi_w1 & 1) == 1, "ACK should be set when use_no_ack=False"


def test_build_tx_descriptors_rt5592_uses_5word_txwi():
    """RT5592 silicon needs a 5-word (20-byte) TXWI; total prefix = 24 B."""
    from wifit3.chips.rt2800usb.tx import build_tx_descriptors
    desc = build_tx_descriptors(26, txwi_size=20)
    assert len(desc) == 4 + 20


def test_build_tx_descriptors_phymode_matches_rt5572_capture():
    """5 GHz TX is OFDM, 2.4 GHz is CCK — the only TXWI byte that differs by band
    is W0[30:31] PHYMODE. Prefixes are byte-exact vs the kernel rt2800usb RT5572
    deauth (ch1 CCK, ch149 OFDM)."""
    from wifit3.chips.rt2800usb.constants import TXWI_PHYMODE_CCK, TXWI_PHYMODE_OFDM
    from wifit3.chips.rt2800usb.tx import build_tx_descriptors
    cck = build_tx_descriptors(26, txwi_size=20, use_no_ack=True, mcs=0, phymode=TXWI_PHYMODE_CCK)
    ofdm = build_tx_descriptors(26, txwi_size=20, use_no_ack=True, mcs=0, phymode=TXWI_PHYMODE_OFDM)
    assert cck.hex() == "300000050003000000001a80000000000000000000000000"
    assert ofdm.hex() == "300000050003004000001a80000000000000000000000000"


def test_txwi_size_for_silicon():
    from wifit3.chips.rt2800usb.constants import RT_RT3572, RT_RT5392, RT_RT5592
    from wifit3.chips.rt2800usb.tx import txwi_size_for_silicon
    assert txwi_size_for_silicon(RT_RT5392) == 16
    assert txwi_size_for_silicon(RT_RT3572) == 16
    assert txwi_size_for_silicon(RT_RT5592) == 20


def test_build_deauth_structure():
    from wifit3.chips.rt2800usb.tx import BROADCAST_MAC, build_deauth
    bssid = bytes.fromhex("aabbccddeeff")
    f = build_deauth(BROADCAST_MAC, bssid)
    assert len(f) == 26
    assert f[0] == 0xC0  # mgmt, deauth
    assert f[4:10] == BROADCAST_MAC
    assert f[10:16] == bssid
    assert f[16:22] == bssid
    assert f[24] == 7   # CLASS3 reason


# ----------------------------------------------------------------------
# M-B1 RT5572 / RF5592 tests — init_bbp_5592, init_rfcsr_5592,
# _set_channel_5592_2g, dispatcher routing, xtal selection.
# ----------------------------------------------------------------------
def test_init_bbp_dispatcher_routes_5592(monkeypatch):
    """RT5592 routes to init_bbp_5592 — discriminator: BBP[20]=0x06
    (only init_bbp_5592 writes this) and BBP[68]=0xDD (vs init_bbp_3572's
    0x0B from init_bbp_early — overwritten by 5592 body)."""
    import wifit3.chips.rt2800usb.bbp as bbp_mod
    monkeypatch.setattr(bbp_mod.time, "sleep", lambda *_a, **_kw: None)

    t = BbpFakeTransport()
    bbp_mod.init_bbp(t, RT_RT5592, txpath=2, rxpath=2)
    assert bbp_mod.bbp_read(t, 20) == 0x06
    assert bbp_mod.bbp_read(t, 68) == 0xDD
    # BBP[84] final = 0x19, not the 0x9a intermediate
    assert bbp_mod.bbp_read(t, 84) == 0x19


def test_init_bbp_5592_writes_full_table(monkeypatch):
    """Spot-check the kernel init_bbp_5592 table writes land (using the
    final-value rule for registers the kernel writes more than once).
    [SRC] rt2800lib.c:6967-7039."""
    import wifit3.chips.rt2800usb.bbp as bbp_mod
    monkeypatch.setattr(bbp_mod.time, "sleep", lambda *_a, **_kw: None)

    t = BbpFakeTransport()
    bbp_mod.init_bbp_5592(t, rxpath=2, ant_diversity=0, chip_rev=0)
    expected = {
        20: 0x06, 31: 0x08,
        65: 0x2C, 68: 0xDD, 69: 0x1A, 70: 0x05, 73: 0x13,
        74: 0x0F, 75: 0x4F, 76: 0x28, 77: 0x59,
        # 84 written twice: 0x9A then 0x19 at the end → 0x19 wins.
        84: 0x19,
        86: 0x38, 88: 0x90, 91: 0x04, 92: 0x02, 95: 0x9A,
        98: 0x12, 103: 0xC0, 104: 0x92,
        # 105 written twice: MLD R-M-W (~0x04 set), then 0x3C → 0x3C wins.
        105: 0x3C,
        106: 0x35, 128: 0x12, 134: 0xD0, 135: 0xF6, 137: 0x0F,
    }
    for word, value in expected.items():
        got = bbp_mod.bbp_read(t, word)
        assert got == value, f"BBP[{word}] = 0x{got:02x}, expected 0x{value:02x}"
    # BBP[4].MAC_IF_CTRL set (called twice in init_bbp_5592 — same effect).
    assert (bbp_mod.bbp_read(t, 4) & 0x40) == 0x40


def test_init_bbp_5592_glrt_table_replay(monkeypatch):
    """The 70-byte GLRT table writes BBP195=offset + BBP196=value pairs
    for offsets 128..211. After init_bbp_5592 finishes, the BBP195/196
    indirect pair holds the LAST written entry: offset=211, value=0x6e."""
    import wifit3.chips.rt2800usb.bbp as bbp_mod
    monkeypatch.setattr(bbp_mod.time, "sleep", lambda *_a, **_kw: None)

    t = BbpFakeTransport()
    bbp_mod.init_bbp_5592_glrt(t)
    # Last entry in _RT5592_GLRT_TABLE is 0x6e at offset 211.
    assert bbp_mod.bbp_read(t, 195) == 211
    assert bbp_mod.bbp_read(t, 196) == 0x6E


def test_init_bbp_5592_main_antenna_default():
    """ant_diversity != 3 (kernel default-path) → BBP152 bit 7 SET."""
    import wifit3.chips.rt2800usb.bbp as bbp_mod
    t = BbpFakeTransport()
    bbp_mod.init_bbp_5592(t, rxpath=2, ant_diversity=0, chip_rev=0)
    assert (bbp_mod.bbp_read(t, 152) & 0x80) == 0x80


def test_init_bbp_5592_aux_antenna_when_div_3():
    """ant_diversity == 3 → BBP152 bit 7 CLEAR (aux antenna)."""
    import wifit3.chips.rt2800usb.bbp as bbp_mod
    t = BbpFakeTransport()
    bbp_mod.init_bbp_5592(t, rxpath=2, ant_diversity=3, chip_rev=0)
    assert (bbp_mod.bbp_read(t, 152) & 0x80) == 0x00


def test_init_bbp_5592_rev_5592c_extra_writes(monkeypatch):
    """chip_rev >= REV_RT5592C (0x0221) triggers BBP254 bit 7 + BBP103=0xC0."""
    import wifit3.chips.rt2800usb.bbp as bbp_mod
    from wifit3.chips.rt2800usb.constants import REV_RT5592C
    monkeypatch.setattr(bbp_mod.time, "sleep", lambda *_a, **_kw: None)

    t = BbpFakeTransport()
    bbp_mod.init_bbp_5592(t, rxpath=2, ant_diversity=0, chip_rev=REV_RT5592C)
    assert (bbp_mod.bbp_read(t, 254) & 0x80) == 0x80
    # BBP103 written twice on REV_RT5592C+: first 0xC0 from the main
    # body, then 0xC0 again at the end of init_bbp_5592 (the rev-gated
    # tail). Either way final = 0xC0.
    assert bbp_mod.bbp_read(t, 103) == 0xC0


def test_init_bbp_5592_pre_rev_5592c_no_bbp254(monkeypatch):
    """chip_rev < REV_RT5592C → BBP254 untouched (left at 0)."""
    import wifit3.chips.rt2800usb.bbp as bbp_mod
    monkeypatch.setattr(bbp_mod.time, "sleep", lambda *_a, **_kw: None)

    t = BbpFakeTransport()
    bbp_mod.init_bbp_5592(t, rxpath=2, ant_diversity=0, chip_rev=0x0200)
    assert bbp_mod.bbp_read(t, 254) == 0x00


def test_init_rfcsr_dispatcher_routes_5592(monkeypatch):
    """RT5592 routes to init_rfcsr_5592 — discriminator: RFCSR1 = 0x3F
    (RT5392 writes 0x17 here; RT3572 writes 0x81)."""
    import wifit3.chips.rt2800usb.rfcsr as rfm
    monkeypatch.setattr(rfm.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(rfm, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    result = rfm.init_rfcsr(t, RT_RT5592, freq_offset=0, chip_rev=0)
    assert result is None
    assert rfm.rfcsr_read(t, 1) == 0x3F


def test_init_rfcsr_5592_writes_full_table(monkeypatch):
    """Spot-check the 21-entry RT5592 RFCSR table.
    [SRC] rt2800lib.c:8466-8486."""
    import wifit3.chips.rt2800usb.rfcsr as rfm
    monkeypatch.setattr(rfm.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(rfm, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    rfm.init_rfcsr_5592(t, freq_offset=0, chip_rev=0)
    expected = {
        1: 0x3F, 3: 0x08, 5: 0x10, 6: 0xE4, 7: 0x00,
        14: 0x00, 15: 0x00, 16: 0x00, 18: 0x03, 19: 0x4D,
        20: 0x10, 21: 0x8D, 26: 0x82, 28: 0x00, 29: 0x10,
        33: 0xC0, 34: 0x07, 35: 0x12, 47: 0x0C, 53: 0x22, 63: 0x07,
    }
    for word, value in expected.items():
        got = rfm.rfcsr_read(t, word)
        assert got == value, f"RFCSR[{word}] = 0x{got:02x}, expected 0x{value:02x}"


def test_init_rfcsr_5592_kicks_rfcsr2(monkeypatch):
    """RFCSR2 = 0x80 (cal kick) fires after the bulk table write."""
    import wifit3.chips.rt2800usb.rfcsr as rfm
    monkeypatch.setattr(rfm.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(rfm, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    rfm.init_rfcsr_5592(t, freq_offset=0, chip_rev=0)
    assert rfm.rfcsr_read(t, 2) == 0x80


def test_init_rfcsr_5592_rev_gate_pre_5592c_writes_rfcsr27(monkeypatch):
    """chip_rev < REV_RT5592C → RFCSR27 = 0x03; BBP103 untouched."""
    import wifit3.chips.rt2800usb.bbp as bbp_mod
    import wifit3.chips.rt2800usb.rfcsr as rfm
    monkeypatch.setattr(rfm.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(rfm, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    rfm.init_rfcsr_5592(t, freq_offset=0, chip_rev=0x0200)
    assert rfm.rfcsr_read(t, 27) == 0x03
    assert bbp_mod.bbp_read(t, 103) == 0x00


def test_init_rfcsr_5592_rev_gate_5592c_writes_bbp103(monkeypatch):
    """chip_rev >= REV_RT5592C → BBP103 = 0xC0; RFCSR27 untouched."""
    import wifit3.chips.rt2800usb.bbp as bbp_mod
    import wifit3.chips.rt2800usb.rfcsr as rfm
    from wifit3.chips.rt2800usb.constants import REV_RT5592C
    monkeypatch.setattr(rfm.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(rfm, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    rfm.init_rfcsr_5592(t, freq_offset=0, chip_rev=REV_RT5592C)
    assert bbp_mod.bbp_read(t, 103) == 0xC0
    assert rfm.rfcsr_read(t, 27) == 0x00


def test_set_channel_5592_rejects_channel_not_in_table(monkeypatch):
    """A non-table channel (e.g. ch 2.4 GHz 15) raises ValueError."""
    import pytest
    import wifit3.chips.rt2800usb.chan as chan_mod
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    with pytest.raises(ValueError, match="not in rf_vals_5592"):
        chan_mod.set_channel(t, RT_RT5592, 15, xtal_40mhz=True)


def test_set_channel_5592_2g_xtal40_writes_rfcsr8_for_ch1(monkeypatch):
    """xtal40 ch 1 → N=241, RFCSR8 = 0xF1."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.rfcsr import rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 1,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
    )
    assert rfcsr_read(t, 8) == 0xF1


def test_set_channel_5592_2g_xtal20_writes_rfcsr8_for_ch1(monkeypatch):
    """xtal20 ch 1 → N=482, RFCSR8 = 482 & 0xff = 0xE2."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.rfcsr import rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 1,
        xtal_40mhz=False, tx_chain_num=2, rx_chain_num=2,
    )
    assert rfcsr_read(t, 8) == 0xE2


def test_set_channel_5592_2g_synth_pack_for_each_channel(monkeypatch):
    """Spot-check RFCSR8 against the xtal40 N column for several
    channels. RFCSR9 is hard to verify directly (kernel R-M-W from
    prior state) so we just nail RFCSR8 — RFCSR9/11 packing math is
    covered by the unit test on _RF_VALS_5592_XTAL40_2G content."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.rfcsr import rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    cases = [
        (1, 241), (6, 243), (11, 246), (13, 247), (14, 248),
    ]
    for ch, expected_n in cases:
        t = RfcsrFakeTransport()
        chan_mod.set_channel(
            t, RT_RT5592, ch,
            xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
        )
        assert rfcsr_read(t, 8) == expected_n & 0xFF, \
            f"ch={ch}: RFCSR8 = 0x{rfcsr_read(t, 8):02x}, expected 0x{expected_n & 0xFF:02x}"


def test_set_channel_5592_2g_ch_edge_rfcsr23_low(monkeypatch):
    """ch 1-10 → RFCSR23 = RFCSR59 = 0x07."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.rfcsr import rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 6,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
    )
    assert rfcsr_read(t, 23) == 0x07
    assert rfcsr_read(t, 59) == 0x07


def test_set_channel_5592_2g_ch_edge_rfcsr23_high(monkeypatch):
    """ch 11-14 → RFCSR23 = RFCSR59 = 0x06."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.rfcsr import rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 11,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
    )
    assert rfcsr_read(t, 23) == 0x06
    assert rfcsr_read(t, 59) == 0x06


def test_set_channel_5592_2g_writes_bbp82_0x84_final(monkeypatch):
    """The post-RF tail overwrites the rt2800_config_channel_rf55xx-
    written BBP82 (0x62) with 0x84 (no ext_lna_bg default branch)."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.bbp import bbp_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 1,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
    )
    assert bbp_read(t, 82) == 0x84
    assert bbp_read(t, 75) == 0x50


def test_set_channel_5592_2g_writes_bbp141_glrt_0x1a(monkeypatch):
    """RT5592-only block (rt2800lib.c:4485-4493): BBP141 GLRT = 0x1a
    for HT20. Writes go through the BBP195/196 indirect pair."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.bbp import bbp_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 1,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
    )
    # After set_channel, BBP66 fan-out via bbp_write_with_rx_chain
    # leaves BBP195/27 in a final state — but BBP141 went through
    # bbp_glrt_write (BBP195=141, BBP196=0x1a). The last GLRT write
    # was BBP141, AFTER BBP195/196 settled on 211/0x6e during init.
    # So BBP195 should now be 141 and BBP196 should be 0x1A.
    assert bbp_read(t, 195) == 141
    assert bbp_read(t, 196) == 0x1A


def test_set_channel_5592_2g_bbp66_agc_formula(monkeypatch):
    """BBP66 AGC for 2.4 GHz: (0x1c + 2 * lna_gain) fanned across
    rx_chain_num chains. Verify final BBP66 (last chain written wins)."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.bbp import bbp_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 1,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
        lna_gain=4,
    )
    assert bbp_read(t, 66) == (0x1C + 2 * 4) & 0xFF   # 0x24


def test_is_xtal_40mhz_reads_mac_debug_index_bit():
    """is_xtal_40mhz returns True iff MAC_DEBUG_INDEX bit 31 is set."""
    from wifit3.chips.rt2800usb.chan import is_xtal_40mhz
    from wifit3.chips.rt2800usb.constants import MAC_DEBUG_INDEX

    t = FakeTransport()
    t.write32(MAC_DEBUG_INDEX, 0x00000000)
    assert is_xtal_40mhz(t) is False
    t.write32(MAC_DEBUG_INDEX, 0x80000000)
    assert is_xtal_40mhz(t) is True
    # Other bits set but not bit 31 — still 20 MHz.
    t.write32(MAC_DEBUG_INDEX, 0x7FFFFFFF)
    assert is_xtal_40mhz(t) is False


# ----------------------------------------------------------------------
# M-B2 RT5572 5 GHz tests — _set_channel_5592_5g + IQ cal.
# ----------------------------------------------------------------------
def test_iq_calibration_struct_ff_only_globals_map_to_zero():
    """kernel rt2800_iq_calibrate: the per-band TX0/TX1 gain/phase cal bytes are
    written to BBP159 VERBATIM (rt2x00_eeprom_byte, no fallback), even 0xFF —
    only the two global RF-IQ comp/imbalance bytes get the 0xFF → 0 fallback
    (rt2800lib.c:4103, 4109). The old port applied 0xFF → 0 to all of them,
    diverging from a burned-EEPROM 5 GHz tune."""
    from wifit3.chips.rt2800usb.eeprom import parse_eeprom
    buf = bytearray(0x200)
    # All-FF EFUSE bytes — typical for an unprogrammed dongle.
    for i in range(len(buf)):
        buf[i] = 0xFF
    # FREQ word must NOT be 0xFFFF for the default-injection path to skip;
    # set it to a normal value (just for this test).
    buf[0x3A] = 0x35
    buf[0x3B] = 0x00
    ee = parse_eeprom(bytes(buf))
    assert ee.iq_cal is not None
    # Per-band TX cal bytes: raw (0xFF preserved).
    assert ee.iq_cal.tx0_gain_2g == 0xFF
    assert ee.iq_cal.tx0_phase_2g == 0xFF
    assert ee.iq_cal.tx1_gain_5g_hi == 0xFF
    # Global comp/imbalance controls: 0xFF → 0.
    assert ee.iq_cal.rf_iq_comp == 0
    assert ee.iq_cal.rf_iq_imbal == 0


def test_iq_calibration_picks_correct_band_for_channel():
    """for_channel(N) selects the right per-band byte tuple."""
    from wifit3.chips.rt2800usb.eeprom import IqCalibration
    iq = IqCalibration(
        tx0_gain_2g=0x10, tx0_phase_2g=0x11, tx1_gain_2g=0x12, tx1_phase_2g=0x13,
        tx0_gain_5g_lo=0x20, tx0_phase_5g_lo=0x21, tx1_gain_5g_lo=0x22, tx1_phase_5g_lo=0x23,
        tx0_gain_5g_mid=0x30, tx0_phase_5g_mid=0x31, tx1_gain_5g_mid=0x32, tx1_phase_5g_mid=0x33,
        tx0_gain_5g_hi=0x40, tx0_phase_5g_hi=0x41, tx1_gain_5g_hi=0x42, tx1_phase_5g_hi=0x43,
        rf_iq_comp=0xAA, rf_iq_imbal=0xBB,
    )
    # 2.4 GHz
    c = iq.for_channel(6)
    assert c.tx0_gain == 0x10 and c.tx1_phase == 0x13 and c.rf_iq_comp == 0xAA
    # UNII-1/2
    c = iq.for_channel(36)
    assert c.tx0_gain == 0x20 and c.tx1_phase == 0x23
    c = iq.for_channel(64)
    assert c.tx0_gain == 0x20
    # UNII-2-ext
    c = iq.for_channel(100)
    assert c.tx0_gain == 0x30 and c.tx1_phase == 0x33
    c = iq.for_channel(138)
    assert c.tx0_gain == 0x30
    # UNII-3
    c = iq.for_channel(140)
    assert c.tx0_gain == 0x40 and c.tx1_phase == 0x43
    c = iq.for_channel(165)
    assert c.tx0_gain == 0x40
    # Channels outside known sub-bands fall through to cal=0.
    c = iq.for_channel(170)
    assert c.tx0_gain == 0 and c.tx0_phase == 0
    # Global IQ comp/imbal still carried through.
    assert c.rf_iq_comp == 0xAA
    assert c.rf_iq_imbal == 0xBB


def test_iq_calibrate_writes_bbp158_159_pairs():
    """iq_calibrate writes 6 BBP158/159 index/data pairs (TX0/TX1
    gain/phase + global RF IQ compensation + imbalance)."""
    import wifit3.chips.rt2800usb.bbp as bbp_mod
    from wifit3.chips.rt2800usb.chan import iq_calibrate
    from wifit3.chips.rt2800usb.eeprom import IqCalChannel

    iq = IqCalChannel(
        tx0_gain=0x11, tx0_phase=0x22,
        tx1_gain=0x33, tx1_phase=0x44,
        rf_iq_comp=0x55, rf_iq_imbal=0x66,
    )
    t = BbpFakeTransport()
    iq_calibrate(t, 6, iq)
    # After the sequence, BBP158/159 hold the LAST pair's values:
    # BBP158=0x03 (imbal indirect), BBP159=0x66 (imbal value).
    assert bbp_mod.bbp_read(t, 158) == 0x03
    assert bbp_mod.bbp_read(t, 159) == 0x66


def test_set_channel_5592_5g_unii1_writes_rfcsr10_0x97(monkeypatch):
    """5 GHz fixed-block first write — RFCSR10 = 0x97 (vs 2.4G's 0x90)."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.rfcsr import rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 36,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
    )
    assert rfcsr_read(t, 10) == 0x97


def test_set_channel_5592_5g_synth_pack_xtal40(monkeypatch):
    """xtal40 ch 36 → N=86 → RFCSR8 = 0x56. Spot-check across UNII bands."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.rfcsr import rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    # ch 36 → N=86, ch 100 → N=91, ch 149 → N=95, ch 165 → N=97 (xtal40)
    cases = [(36, 86), (100, 91), (149, 95), (165, 97)]
    for ch, expected_n in cases:
        t = RfcsrFakeTransport()
        chan_mod.set_channel(
            t, RT_RT5592, ch,
            xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
        )
        assert rfcsr_read(t, 8) == expected_n & 0xFF, (
            f"ch={ch}: RFCSR8 = 0x{rfcsr_read(t, 8):02x}, expected 0x{expected_n & 0xFF:02x}"
        )


def test_set_channel_5592_5g_unii1_ch36_uses_rfcsr24_0x09(monkeypatch):
    """ch <= 50 in UNII-1/2 → RFCSR24 = 0x09."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.rfcsr import rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 36,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
    )
    assert rfcsr_read(t, 24) == 0x09


def test_set_channel_5592_5g_unii1_ch52_uses_rfcsr24_0x07(monkeypatch):
    """ch >= 52 in UNII-1/2 → RFCSR24 = 0x07 + RFCSR55 = 0x04 + RFCSR56 = 0xBB."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.rfcsr import rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 52,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
    )
    assert rfcsr_read(t, 24) == 0x07
    assert rfcsr_read(t, 55) == 0x04
    assert rfcsr_read(t, 56) == 0xBB


def test_set_channel_5592_5g_unii3_ch153_uses_rfcsr23_0x3c(monkeypatch):
    """ch <= 153 in UNII-2-ext/UNII-3 block → RFCSR23 = 0x3C, RFCSR24 = 0x06."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.rfcsr import rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 153,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
    )
    assert rfcsr_read(t, 23) == 0x3C
    assert rfcsr_read(t, 24) == 0x06


def test_set_channel_5592_5g_unii3_ch157_uses_rfcsr23_0x38(monkeypatch):
    """ch >= 155 in UNII-3 → RFCSR23 = 0x38, RFCSR24 = 0x05."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.rfcsr import rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 157,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
    )
    assert rfcsr_read(t, 23) == 0x38
    assert rfcsr_read(t, 24) == 0x05


def test_set_channel_5592_5g_unii2ext_ch100_breakpoints(monkeypatch):
    """ch <= 138 in 100-165 block uses the 'low' tuple for RFCSR39/43/44/46."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.rfcsr import rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 100,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
    )
    assert rfcsr_read(t, 39) == 0x1A
    assert rfcsr_read(t, 43) == 0x3B
    assert rfcsr_read(t, 44) == 0x20
    assert rfcsr_read(t, 46) == 0x18


def test_set_channel_5592_5g_unii2ext_ch140_breakpoints(monkeypatch):
    """ch >= 140 in 100-165 block uses the 'high' tuple for RFCSR39/43/44/46."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.rfcsr import rfcsr_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 149,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
    )
    assert rfcsr_read(t, 39) == 0x18
    assert rfcsr_read(t, 43) == 0x1B
    assert rfcsr_read(t, 44) == 0x10
    assert rfcsr_read(t, 46) == 0x08


def test_set_channel_5592_5g_writes_bbp82_0xf2(monkeypatch):
    """5 GHz post-RF tail overwrites BBP82 with 0xF2 (vs 2.4G's 0x84)."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.bbp import bbp_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 36,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
    )
    assert bbp_read(t, 82) == 0xF2


def test_set_channel_5592_5g_bbp75_with_ext_lna_a(monkeypatch):
    """has_cap_external_lna_a=True → BBP75 = 0x46 (vs default 0x50)."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.bbp import bbp_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 36,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
        has_cap_external_lna_a=True,
    )
    assert bbp_read(t, 75) == 0x46

    t2 = RfcsrFakeTransport()
    chan_mod.set_channel(
        t2, RT_RT5592, 36,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
        has_cap_external_lna_a=False,
    )
    assert bbp_read(t2, 75) == 0x50


def test_set_channel_5592_5g_tx_band_cfg_a_bit(monkeypatch):
    """5 GHz → TX_BAND_CFG.A = 1, .BG = 0."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import (
        TX_BAND_CFG_A, TX_BAND_CFG_BG_BIT, TX_BAND_CFG_REG,
    )
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 36,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
    )
    reg = t.read32(TX_BAND_CFG_REG)
    assert reg & TX_BAND_CFG_A, "A bit must be set for 5 GHz"
    assert not (reg & TX_BAND_CFG_BG_BIT), "BG bit must be clear for 5 GHz"


def test_set_channel_5592_5g_tx_pin_cfg_uses_a_side_pas(monkeypatch):
    """5 GHz TX_PIN_CFG: A-side PAs (A0+A1), LNAs on both A+G sides per
    active chain. G-side PAs must be clear."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import (
        TX_PIN_CFG_LNA_PE_A0_EN_BIT, TX_PIN_CFG_LNA_PE_A1_EN,
        TX_PIN_CFG_LNA_PE_G0_EN_BIT, TX_PIN_CFG_LNA_PE_G1_EN,
        TX_PIN_CFG_PA_PE_A0_EN_BIT, TX_PIN_CFG_PA_PE_A1_EN,
        TX_PIN_CFG_PA_PE_G0_EN_BIT, TX_PIN_CFG_PA_PE_G1_EN,
        TX_PIN_CFG_REG,
    )
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 36,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
    )
    tx_pin = t.read32(TX_PIN_CFG_REG)
    # PA: A0 + A1 set; G0/G1 cleared.
    assert tx_pin & TX_PIN_CFG_PA_PE_A0_EN_BIT
    assert tx_pin & TX_PIN_CFG_PA_PE_A1_EN
    assert not (tx_pin & TX_PIN_CFG_PA_PE_G0_EN_BIT)
    assert not (tx_pin & TX_PIN_CFG_PA_PE_G1_EN)
    # LNAs: BOTH A- and G-side enables for every active chain.
    assert tx_pin & TX_PIN_CFG_LNA_PE_A0_EN_BIT
    assert tx_pin & TX_PIN_CFG_LNA_PE_A1_EN
    assert tx_pin & TX_PIN_CFG_LNA_PE_G0_EN_BIT
    assert tx_pin & TX_PIN_CFG_LNA_PE_G1_EN


def test_set_channel_5592_5g_bbp66_agc_formula(monkeypatch):
    """5 GHz BBP66 AGC = 0x24 + 2*lna_gain (vs 0x1c for 2.4G)."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.bbp import bbp_read
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 36,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
        lna_gain=4,
    )
    assert bbp_read(t, 66) == (0x24 + 2 * 4) & 0xFF   # 0x2C


def test_set_channel_5592_5g_iq_cal_runs_per_tune(monkeypatch):
    """When iq_cal is plumbed in, the BBP158/159 last-pair leaves
    BBP158=0x03 (RF IQ imbalance index) + BBP159=<imbal byte>."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.bbp import bbp_read
    from wifit3.chips.rt2800usb.eeprom import IqCalibration
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    iq = IqCalibration(
        tx0_gain_2g=0, tx0_phase_2g=0, tx1_gain_2g=0, tx1_phase_2g=0,
        tx0_gain_5g_lo=0x10, tx0_phase_5g_lo=0x11,
        tx1_gain_5g_lo=0x12, tx1_phase_5g_lo=0x13,
        tx0_gain_5g_mid=0, tx0_phase_5g_mid=0, tx1_gain_5g_mid=0, tx1_phase_5g_mid=0,
        tx0_gain_5g_hi=0, tx0_phase_5g_hi=0, tx1_gain_5g_hi=0, tx1_phase_5g_hi=0,
        rf_iq_comp=0x77, rf_iq_imbal=0x88,
    )
    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 36,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
        iq_cal=iq,
    )
    # Final BBP158/159 pair is the RF IQ imbalance indirect write.
    assert bbp_read(t, 158) == 0x03
    assert bbp_read(t, 159) == 0x88


def test_set_channel_5592_2g_iq_cal_still_runs(monkeypatch):
    """2 GHz path now also calls iq_calibrate (was deferred in M-B1).
    Final BBP158/159 = 0x03 / rf_iq_imbal."""
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.bbp import bbp_read
    from wifit3.chips.rt2800usb.eeprom import IqCalibration
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(chan_mod, "freq_cal_mode1_usb", lambda *_a, **_kw: None)

    iq = IqCalibration(
        tx0_gain_2g=0x21, tx0_phase_2g=0x22, tx1_gain_2g=0, tx1_phase_2g=0,
        tx0_gain_5g_lo=0, tx0_phase_5g_lo=0, tx1_gain_5g_lo=0, tx1_phase_5g_lo=0,
        tx0_gain_5g_mid=0, tx0_phase_5g_mid=0, tx1_gain_5g_mid=0, tx1_phase_5g_mid=0,
        tx0_gain_5g_hi=0, tx0_phase_5g_hi=0, tx1_gain_5g_hi=0, tx1_phase_5g_hi=0,
        rf_iq_comp=0, rf_iq_imbal=0x99,
    )
    t = RfcsrFakeTransport()
    chan_mod.set_channel(
        t, RT_RT5592, 1,
        xtal_40mhz=True, tx_chain_num=2, rx_chain_num=2,
        iq_cal=iq,
    )
    assert bbp_read(t, 158) == 0x03
    assert bbp_read(t, 159) == 0x99


def test_is_chip_warm_distinguishes_cold_pre_init_from_warm():
    """[WIRE M1] freshly-plugged PAU05 reads PBF_SYS_CTRL=0x00002080
    (READY bit 7 set + 'pre-init' bit 13 set). Kernel
    `rt2800usb_init_registers` clears bit 13 as part of init. So:

        cold = bit 13 set
        warm = bit 13 cleared AND bit 7 set
    """
    t = FakeTransport()

    # Fresh-plug pattern: READY + pre-init both set → COLD
    t.write_bytes(PBF_SYS_CTRL, [PBF_SYS_CTRL_READY, 0x20, 0, 0])
    assert is_chip_warm(t) is False

    # Post-init (FW running, init_registers cleared bit 13) → WARM
    t.write_bytes(PBF_SYS_CTRL, [PBF_SYS_CTRL_READY, 0, 0, 0])
    assert is_chip_warm(t) is True

    # Truly cold (no FW boot) → not warm
    t.write_bytes(PBF_SYS_CTRL, [0, 0, 0, 0])
    assert is_chip_warm(t) is False


# ----------------------------------------------------------------------
# EEPROM-derived TX power (M-B TX-power fix): the RF55xx analog PA
# (RFCSR49/50) + per-rate TX_PWR_CFG are decoded from the burned EEPROM's
# TXPOWER_BG/A + BYRATE tables and clamped — not hardcoded / left at 0.
# ----------------------------------------------------------------------
def _burned_eeprom(byte_overrides=None):
    """Build an EepromValues from a synthetic BURNED EFUSE dump."""
    from wifit3.chips.rt2800usb.eeprom import parse_eeprom
    buf = bytearray(0x200)
    buf[0x34] = 0x22          # NIC_CONF0 (word 0x1a): txpath=2 rxpath=2 -> burned
    buf[0x3a] = 0x11          # FREQ (word 0x1d) non-FF so the unburned default is skipped
    buf[0x4e] = 0x50          # EIRP_MAX 2GHz low byte >= 0x50 -> power_limit False
    for off, val in (byte_overrides or {}).items():
        buf[off] = val
    return parse_eeprom(bytes(buf))


def test_txpower_to_dev_clamps_per_band():
    """rt2800_txpower_to_dev: 2.4 GHz clamps to [0, 31], 5 GHz to [-7, 15]."""
    from wifit3.chips.rt2800usb.eeprom import txpower_to_dev
    assert txpower_to_dev(1, 17) == 17
    assert txpower_to_dev(1, 40) == 31       # over MAX_G -> 31
    assert txpower_to_dev(1, -1) == 0        # unburned 0xFF(-1) -> 0, NOT a fallback value
    assert txpower_to_dev(36, 23) == 15      # over MAX_A -> 15 (the RT5572 5 GHz case)
    assert txpower_to_dev(36, 8) == 8
    assert txpower_to_dev(36, -20) == -7     # under MIN_A -> -7


def test_default_power_2g_from_bg_tables():
    """2.4 GHz (power1, power2) = TXPOWER_BG1/BG2[ch-1], clamped."""
    import wifit3.chips.rt2800usb.chan as chan
    ev = _burned_eeprom({0x52: 17, 0x57: 13, 0x60: 15})  # BG1 ch1/ch6, BG2 ch1
    assert chan.default_power(ev, RT_RT5592, 1) == (17, 15)
    assert chan.default_power(ev, RT_RT5592, 6)[0] == 13


def test_default_power_5g_index_is_per_silicon():
    """5 GHz uses TXPOWER_A1/A2 indexed by the channel's position in THIS
    silicon's RF table — RF5592 and RF3052 give different indices."""
    import wifit3.chips.rt2800usb.chan as chan
    from wifit3.chips.rt2800usb.constants import RT_RT3572
    ev = _burned_eeprom({0x78: 23})        # A1[0] -> ch36 -> clamp 23 to 15
    assert chan.default_power(ev, RT_RT5592, 36, xtal_40mhz=True)[0] == 15
    assert chan.txpower_5g_index(RT_RT5592, 149) != chan.txpower_5g_index(RT_RT3572, 149)


def test_config_txpower_byrate_min_0xc():
    """config_txpower (RF55xx, delta=0): each TX_PWR_CFG rate nibble = min(BYRATE, 0xC)."""
    import wifit3.chips.rt2800usb.chan as chan
    from wifit3.chips.rt2800usb.bbp import bbp_read
    from wifit3.chips.rt2800usb.constants import TX_PWR_CFG_0
    # BYRATE word0 (0xde) = 0x6666 -> rate0..3 = 6; word1 (0xe0) = 0xaaaa -> 0xa.
    ev = _burned_eeprom({0xde: 0x66, 0xdf: 0x66, 0xe0: 0xaa, 0xe1: 0xaa})
    assert ev.power_limit is False
    t = RfcsrFakeTransport()
    chan.config_txpower(t, ev, is_2g=True)
    assert t.read32(TX_PWR_CFG_0) == 0xAAAA6666   # low4=6, high4=0xa
    assert (bbp_read(t, 1) & 0x03) == 0            # BBP1.TX_POWER_CTRL = 0 (no backoff)
