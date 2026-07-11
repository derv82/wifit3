"""Runtime EEPROM/strap discriminators for mt76x0u (cross-card generalization).

The captured reference is a 0x7650 dual-band die with no USB quirk, so every
gate defaults to it and its wire is byte-identical. The two live discriminators:

  - is_mt7630 (ASIC_VERSION >> 16 == 0x7630, the 2.4G+BT combo strap) gates:
      * eeprom decode: has_5ghz masked off        [SRC] mt76x0/eeprom.c:62-65
      * RF(5,2) patch value 0x1d (vs 0x0c)         [SRC] mt76x0/phy.c:1143-1146
      * phy_calibrate skipped entirely             [SRC] mt76x0/phy.c:866-867
  - no_2ghz (Archer T1U USB driver_info=1) gates:
      * eeprom decode: has_2ghz masked off         [SRC] mt76x0/eeprom.c:57-60
"""
from wifit3.chips.mt76x0u.constants import (
    MT76X0_EEPROM_SIZE,
    MT_EE_NIC_CONF_0,
    MT_MCU_MEMMAP_RF,
    MT_RF,
)
from wifit3.chips.mt76x0u.eeprom import EEPROMCache, decode_chip_cap
from wifit3.chips.mt76x0u.phy import (
    _apply_rf_patch_override,
    phy_calibrate,
    rf_patch_reg_array,
)


def _dual_band_cache() -> EEPROMCache:
    """512-byte EEPROM, NIC_CONF_0 = rx_path=1/tx_path=1/board_type=0 → dual-band."""
    data = bytearray(MT76X0_EEPROM_SIZE)
    data[MT_EE_NIC_CONF_0:MT_EE_NIC_CONF_0 + 2] = (0x0011).to_bytes(2, "little")
    return EEPROMCache(bytes(data))


# ---------------------------------------------------------------------------
# decode_chip_cap — band masks
# ---------------------------------------------------------------------------
def test_decode_cap_reference_is_dual_band():
    """Default (0x7650, no quirk): board-type fall-through keeps both bands."""
    cap = decode_chip_cap(_dual_band_cache())
    assert cap["has_2ghz"] is True
    assert cap["has_5ghz"] is True
    assert cap["tx_path"] == 1 and cap["rx_path"] == 1


def test_decode_cap_mt7630_masks_5ghz():
    """is_mt7630 combo strap: 5 GHz masked off, 2.4 GHz kept."""
    cap = decode_chip_cap(_dual_band_cache(), is_mt7630=True)
    assert cap["has_2ghz"] is True
    assert cap["has_5ghz"] is False


def test_decode_cap_no_2ghz_masks_2ghz():
    """Archer T1U no_2ghz quirk: 2.4 GHz masked off, 5 GHz kept."""
    cap = decode_chip_cap(_dual_band_cache(), no_2ghz=True)
    assert cap["has_2ghz"] is False
    assert cap["has_5ghz"] is True


# ---------------------------------------------------------------------------
# _apply_rf_patch_override — RF(5,2) is the one USB-live strap branch
# ---------------------------------------------------------------------------
def test_rf_patch_rf52_reference_is_0x0c():
    assert _apply_rf_patch_override(MT_RF(5, 2), 0xAB) == 0x0C
    assert _apply_rf_patch_override(MT_RF(5, 2), 0xAB, is_mt7630=False) == 0x0C


def test_rf_patch_rf52_mt7630_is_0x1d():
    assert _apply_rf_patch_override(MT_RF(5, 2), 0xAB, is_mt7630=True) == 0x1D


def test_rf_patch_mmio_only_regs_are_usb_constant():
    """RF(0,3) and RF(0,21) split on mmio-only straps → constant on USB for
    every chip incl. 0x7630 (0x73 / 0x12)."""
    for is7630 in (False, True):
        assert _apply_rf_patch_override(MT_RF(0, 3), 0xAB, is_mt7630=is7630) == 0x73
        assert _apply_rf_patch_override(MT_RF(0, 21), 0xAB, is_mt7630=is7630) == 0x12


class _FakeMCU:
    def __init__(self):
        self.writes: list[tuple[int, int]] = []
        self.calibrations: list[tuple[int, int]] = []

    def random_write(self, base, pairs):
        assert base == MT_MCU_MEMMAP_RF
        self.writes.extend(pairs)

    def calibrate(self, kind, val):
        self.calibrations.append((kind, val))


def test_rf_patch_reg_array_threads_strap():
    """rf_patch_reg_array flips the RF(5,2) byte on the wire per is_mt7630."""
    table = [(MT_RF(0, 3), 0x00), (MT_RF(5, 2), 0x00)]

    ref = _FakeMCU()
    rf_patch_reg_array(ref, table)
    assert (MT_RF(5, 2), 0x0C) in ref.writes

    combo = _FakeMCU()
    rf_patch_reg_array(combo, table, is_mt7630=True)
    assert (MT_RF(5, 2), 0x1D) in combo.writes


# ---------------------------------------------------------------------------
# phy_calibrate — is_mt7630 short-circuits the whole routine
# ---------------------------------------------------------------------------
class _FakeTransport:
    def __init__(self):
        self.reads: list[int] = []
        self.writes: list[tuple[int, int]] = []
        self.regs: dict[int, int] = {}

    def read32(self, addr):
        self.reads.append(addr)
        return self.regs.get(addr, 0)

    def write32(self, addr, val):
        self.writes.append((addr, val))
        self.regs[addr] = val


def test_phy_calibrate_reference_runs(monkeypatch):
    """Default (not 7630): the CAL_FULL/CAL_LC/CAL_RXDCOC chain executes."""
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    t, mcu = _FakeTransport(), _FakeMCU()
    phy_calibrate(t, mcu, channel=6, power_on=False)
    assert mcu.calibrations, "reference card must run calibration"
    assert t.writes, "reference card must touch ALC/IBI regs"


def test_phy_calibrate_mt7630_skips(monkeypatch):
    """is_mt7630: return before any register or MCU op."""
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    t, mcu = _FakeTransport(), _FakeMCU()
    phy_calibrate(t, mcu, channel=6, power_on=False, is_mt7630=True)
    assert mcu.calibrations == []
    assert t.writes == []
    assert t.reads == []
