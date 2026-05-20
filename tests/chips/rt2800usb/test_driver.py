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
    USB_PID_RT5372,
    USB_PID_RT5572,
    USB_VID_RALINK,
)
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

    def __init__(self) -> None:
        self.regs: dict[int, int] = {}

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


# Alias for tests that use the "RecordingTransport" name from the
# rtl8187 test suite shape. Same interface — naming difference only.
RecordingTransport = FakeTransport


def test_supported_ids_cover_all_three_variants():
    pids = {entry.pid for entry in RT2800USBDriver.SUPPORTED_IDS}
    assert pids == {USB_PID_RT5372, USB_PID_RT3572, USB_PID_RT5572}
    assert all(entry.vid == USB_VID_RALINK for entry in RT2800USBDriver.SUPPORTED_IDS)
    # chip_id hints are populated for downstream variant dispatch
    hints = {entry.extras["chip_id"] for entry in RT2800USBDriver.SUPPORTED_IDS}
    assert hints == {"rt5372", "rt3572", "rt5572"}


def test_supported_channels_are_2g_only():
    # 2.4 GHz only at M1 — RT3572/RT5572 will extend to 5 GHz later
    assert RT2800USBDriver.SUPPORTED_CHANNELS == list(range(1, 14))


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
    from wifit3.chips.rt2800usb.constants import RT_RT3572, RT_RT5390
    from wifit3.chips.rt2800usb.rfcsr import init_rfcsr
    t = RfcsrFakeTransport()
    # RT5390 path isn't ported yet (NotImplementedError).
    with pytest.raises(NotImplementedError):
        init_rfcsr(t, RT_RT5390)
    # RT3572 is a totally different family.
    with pytest.raises(NotImplementedError):
        init_rfcsr(t, RT_RT3572)


# ----------------------------------------------------------------------
# M3 RX descriptor tests
# ----------------------------------------------------------------------
def _build_rt2800_rx_urb(frame_body: bytes, *, rssi_byte: int = 40, crc_error: bool = False,
                          mcs: int = 4) -> bytes:
    """Build a synthetic RX URB matching the kernel layout for RT539x:

      [RXINFO 4B] [RXWI 16B] [802.11 frame + 4B FCS] [RXD 4B]
    """
    import struct
    fcs = b"\xaa\xbb\xcc\xdd"
    frame_with_fcs = frame_body + fcs
    mpdu_len = len(frame_with_fcs)

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

    return rxinfo + rxwi + frame_with_fcs + rxd


def test_parse_rx_urb_decodes_trailer_strips_fcs():
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
    import pytest
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import RT_RT5392
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    t = RfcsrFakeTransport()
    for bad in (0, 15, 100, -1, 36):  # 36 is 5 GHz, not supported at M4
        with pytest.raises(ValueError):
            chan_mod.set_channel(t, RT_RT5392, bad)


def test_set_channel_rejects_unsupported_silicon(monkeypatch):
    import pytest
    import wifit3.chips.rt2800usb.chan as chan_mod
    from wifit3.chips.rt2800usb.constants import RT_RT3572
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a, **_kw: None)
    t = RfcsrFakeTransport()
    with pytest.raises(NotImplementedError):
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
    """For a 26-byte deauth + use_no_ack=True + MCS=0/CCK, the
    descriptors should set MPDU byte count = 26, ACK = 0, NSEQ = 1,
    WCID = 0xFF, WIV = 1, QSEL = MGMT."""
    import struct
    from wifit3.chips.rt2800usb.tx import build_tx_descriptors
    desc = build_tx_descriptors(26, txwi_size=16, use_no_ack=True)
    assert len(desc) == 4 + 16  # TXINFO + TXWI

    txinfo_w0, txwi_w0, txwi_w1, txwi_w2, txwi_w3 = struct.unpack("<5I", desc)
    # TXINFO: pkt_len = TXWI(16) + aligned(26→28) = 44; WIV=1; QSEL=0(MGMT)
    assert (txinfo_w0 & 0xFFFF) == 44
    assert txinfo_w0 & (1 << 24), "WIV should be set"
    # MGMT qsel = 0 → bits[26:25] = 0
    assert ((txinfo_w0 >> 25) & 0x3) == 0
    # TXWI_W0: MCS=0, PHYMODE=CCK (0) — entire word should be 0
    assert txwi_w0 == 0
    # TXWI_W1: ACK=0, NSEQ=1, WCID=0xFF, MPDU=26, QID=2, ENTRY=1
    assert (txwi_w1 & 1) == 0, "ACK should be 0 for use_no_ack"
    assert (txwi_w1 >> 1) & 1 == 1, "NSEQ should be 1"
    assert ((txwi_w1 >> 8) & 0xFF) == 0xFF, "WCID should be 0xFF (broadcast)"
    assert ((txwi_w1 >> 16) & 0xFFF) == 26, "MPDU_TOTAL_BYTE_COUNT should be 26"
    # PACKETID_QUEUE=2, PACKETID_ENTRY=1
    assert ((txwi_w1 >> 28) & 0x3) == 2
    assert ((txwi_w1 >> 30) & 0x3) == 1
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
