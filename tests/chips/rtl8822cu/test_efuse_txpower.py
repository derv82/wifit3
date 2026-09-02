"""RTL8822CU EFUSE TX power fields: synthetic logical maps, no hardware."""
from unittest.mock import MagicMock

import pytest

import wifit3.chips.rtl8822cu.constants as constants_mod
import wifit3.chips.rtl8822cu.efuse as efuse_mod
from wifit3.chips.rtl8822cu.constants import (
    DIS_DPD_RATE_ALL,
    DIS_DPD_RATE_NONE,
    EEPROM_DEFAULT_CRYSTAL_CAP_110_8822C,
    EEPROM_DEFAULT_CRYSTAL_CAP_B9,
    HALMAC_RF_1T1R,
    HALMAC_RF_1T2R,
    HALMAC_RF_2T2R,
    HALMAC_RF_MAX_TYPE,
    TXPWR_PG_WITH_PWR_IDX,
    TXPWR_PG_WITH_TSSI_OFFSET,
)
from wifit3.chips.rtl8822cu.efuse import (
    EfuseInfo,
    RfPath,
    hal_rfpath_init,
    is_pg_txpwr_base_invalid,
    is_pg_txpwr_diff_invalid,
    pg_txpwr_lsb_diff_to_s8bit,
    pg_txpwr_msb_diff_to_s8bit,
    read_efuse,
)


def _efuse(scalars: dict[int, int], map_valid: bool = True) -> EfuseInfo:
    """A blank logical map with the given byte offsets set, e.g. ``_efuse({0xC8: 0x40})``.
    0xC8 defaults to a programmed thermal nibble so unrelated fields stay reachable."""
    logical = bytearray(b"\xff" * 768)
    logical[0x00:0x02] = b"\x29\x81"
    logical[0xC8] = 0x00
    for offset, value in scalars.items():
        logical[offset] = value
    return EfuseInfo(True, map_valid, bytes(logical), b"\xff" * 512)


def _efuse_invalid(scalars: dict[int, int]) -> EfuseInfo:
    return _efuse(scalars, map_valid=False)


def _rfpath(efuse: EfuseInfo, ant_num: int = 2, hw_stype: int = 0x0, rf_2t2r: bool = True):
    return hal_rfpath_init(efuse, ant_num=ant_num, hw_stype=hw_stype, rf_2t2r=rf_2t2r)


def test_recorded_device_reproduces_the_vendor_driver_log():
    """The 2001:3329 scalars 0xC1=0x01, 0xC8=0x00, 0xC9=0x4F against the values the vendor driver
    printed for the same adapter [SRC capture-1_logs/driver.log:1860-1861,1903-1909]."""
    info = _efuse({0xC1: 0x01, 0xC8: 0x00, 0xC9: 0x4F})

    assert info.tpt_mode == 0                              # "tpt_mode=0, PG with PWR_IDX"
    assert info.txpwr_pg_mode == TXPWR_PG_WITH_PWR_IDX
    assert info.eeprom_regulatory == 1                     # "EEPROM Regulatory=0x01"
    assert _rfpath(info).max_tx_cnt == 2                   # "max_tx_cnt:2"
    assert _rfpath(info).trx_path_bmp == 0x33              # "RF_TYPE:RF_2T2R"


@pytest.mark.parametrize("byte, mode", [
    (0x00, TXPWR_PG_WITH_PWR_IDX), (0x30, TXPWR_PG_WITH_PWR_IDX),
    (0x40, TXPWR_PG_WITH_TSSI_OFFSET), (0x70, TXPWR_PG_WITH_TSSI_OFFSET),
])
def test_tpt_mode_nibble_selects_the_pg_mode(byte, mode):
    assert _efuse({0xC8: byte}).txpwr_pg_mode == mode


@pytest.mark.parametrize("byte", [0x80, 0xF0, 0xFF])
def test_out_of_range_tpt_mode_assumes_pwr_idx_and_logs(byte, caplog):
    """wifit3 never aborts bring up on EFUSE contents: an unburned 0xC8 (0xFF -> 15) keeps the
    adapter for RX, resolving to PWR_IDX [SRC rtl8822c_ops.c:273-274, hal_com_phycfg.h:36-38]."""
    with caplog.at_level("ERROR"):
        assert _efuse({0xC8: byte}).txpwr_pg_mode == TXPWR_PG_WITH_PWR_IDX
    assert "0xC8" in caplog.text and f"0x{byte:02x}" in caplog.text


def test_tpt_mode_ignores_map_validity():
    assert _efuse_invalid({0xC8: 0x40}).txpwr_pg_mode == TXPWR_PG_WITH_TSSI_OFFSET


def test_out_of_range_tpt_mode_still_yields_a_regulatory():
    """The old eager guard is gone: continuing as PWR_IDX runs the code past the label, so
    regulatory reads 0xC1 [SRC rtl8822c_ops.c:280-283]."""
    assert _efuse({0xC8: 0x80, 0xC1: 0x01}).eeprom_regulatory == 1


def _read_efuse_with(logical: bytes, monkeypatch) -> EfuseInfo:
    monkeypatch.setattr(efuse_mod, "read_physical_map", lambda transport: b"\xff" * 512)
    monkeypatch.setattr(efuse_mod, "decode_logical_map", lambda physical: logical)
    transport = MagicMock()
    transport.read8.return_value = 0x20                    # BIT_AUTOLOAD_SUS set
    return read_efuse(transport)


def _blank_logical(tpt_byte: int) -> bytes:
    logical = bytearray(b"\xff" * 768)
    logical[0x00:0x02] = b"\x29\x81"
    logical[0xC8] = tpt_byte
    return bytes(logical)


def test_read_efuse_succeeds_on_a_fully_unburned_map(monkeypatch, caplog):
    """0xC8 = 0xFF must not abort the probe: read_efuse returns a usable map in PWR_IDX so the
    adapter stays available for RX [SRC rtl8822c_ops.c:834-835 reversed by policy]."""
    with caplog.at_level("ERROR"):
        info = _read_efuse_with(_blank_logical(0xFF), monkeypatch)
    assert info.txpwr_pg_mode == TXPWR_PG_WITH_PWR_IDX
    assert "0xC8" in caplog.text


def test_read_efuse_returns_a_usable_map_for_both_pg_modes(monkeypatch):
    assert _read_efuse_with(_blank_logical(0x00), monkeypatch).txpwr_pg_mode == TXPWR_PG_WITH_PWR_IDX
    assert _read_efuse_with(_blank_logical(0x40), monkeypatch).txpwr_pg_mode == TXPWR_PG_WITH_TSSI_OFFSET


def test_dis_dpd_rate_follows_the_pg_mode():
    assert _efuse({0xC8: 0x00}).dis_dpd_rate == DIS_DPD_RATE_ALL
    assert _efuse({0xC8: 0x40}).dis_dpd_rate == DIS_DPD_RATE_NONE


@pytest.mark.parametrize("byte, regulatory", [(0x00, 0), (0x01, 1), (0x02, 2), (0x03, 3), (0xFE, 2)])
def test_board_option_low_bits_are_the_regulatory(byte, regulatory):
    assert _efuse({0xC1: byte}).eeprom_regulatory == regulatory


def test_unprogrammed_or_invalid_board_option_takes_the_default_regulatory():
    assert _efuse({0xC1: 0xFF}).eeprom_regulatory == 0
    assert _efuse_invalid({0xC1: 0x03}).eeprom_regulatory == 0


@pytest.mark.parametrize("byte, bmp", [
    (0x33, 0x33), (0x13, 0x13), (0x23, 0x23), (0x11, 0x11), (0x22, 0x22),
    (0x4F, 0x00), (0xFF, 0x00),
])
def test_only_the_five_known_antenna_options_specify_a_path_bitmap(byte, bmp):
    assert _efuse({0xC9: byte}).eeprom_trx_path_bmp == bmp


def test_invalid_map_specifies_no_path_bitmap():
    assert _efuse_invalid({0xC9: 0x33}).eeprom_trx_path_bmp == 0x00


def test_the_one_tx_board_option_bit_needs_a_valid_map():
    assert _efuse_invalid({0xC1: 0x05}).eeprom_max_tx_cnt == 0
    assert _efuse({0xC1: 0x05}).eeprom_max_tx_cnt == 1


def test_an_unprogrammed_board_option_byte_still_de_rates_to_one_tx():
    """Hal_EfuseParseBoardType tests 0xC1[2] without the 0xFF screen its InterfaceSel branch uses,
    so a blank 0xC1 on a valid map reads as the 1 Tx/stream feature bit."""
    assert _efuse({0xC1: 0xFF}).eeprom_max_tx_cnt == 1
    assert _rfpath(_efuse({0xC9: 0x33, 0xC1: 0xFF})).max_tx_cnt == 1


@pytest.mark.parametrize("antenna_opt, board_option, bmp, max_tx_cnt", [
    (0x33, 0x01, 0x33, 2),      # 2T2R
    (0x13, 0x01, 0x13, 1),      # 1T2R, TX path A
    (0x23, 0x01, 0x23, 1),      # 1T2R, TX path B
    (0x11, 0x01, 0x11, 1),      # 1T1R path A
    (0x22, 0x01, 0x22, 1),      # 1T1R path B
    (0x4F, 0x01, 0x33, 2),      # unknown antenna option keeps the hal_spec 0x33
    (0x33, 0x05, 0x33, 1),      # 0xC1[2] limits a 2T2R board to 1 TX
    (0x11, 0x05, 0x11, 1),
])
def test_efuse_path_fields_narrow_the_rf_path(antenna_opt, board_option, bmp, max_tx_cnt):
    path = _rfpath(_efuse({0xC9: antenna_opt, 0xC1: board_option}))
    assert (path.trx_path_bmp, path.max_tx_cnt) == (bmp, max_tx_cnt)


def test_a_single_antenna_report_forces_path_b_and_one_tx():
    path = _rfpath(_efuse({0xC9: 0x33, 0xC1: 0x01}), ant_num=1)
    assert (path.trx_path_bmp, path.max_tx_cnt) == (0x22, 1)


def test_hw_stype_0xe_caps_tx_at_one_without_changing_the_paths():
    path = _rfpath(_efuse({0xC9: 0x33, 0xC1: 0x01}), hw_stype=0xE)
    assert (path.trx_path_bmp, path.max_tx_cnt) == (0x33, 1)


def test_a_1t1r_chip_id_drops_the_high_paths():
    path = _rfpath(_efuse({0xC9: 0x33, 0xC1: 0x01}), rf_2t2r=False)
    assert (path.trx_path_bmp, path.max_tx_cnt) == (0x11, 1)


def test_an_unread_report_leaves_the_paths_unlimited():
    """ant_num 0 makes rf_reg_path_avail_num 0, which the C treats as no limit."""
    path = _rfpath(_efuse({0xC9: 0x33, 0xC1: 0x01}), ant_num=0)
    assert (path.trx_path_bmp, path.max_tx_cnt) == (0x33, 2)


@pytest.mark.parametrize("antenna_opt", [0x11, 0x13])
def test_an_efuse_bitmap_that_misses_the_reported_antenna_falls_back(antenna_opt, caplog):
    """Both C exits abort the probe: 0x22 & 0x11 is an empty AND (hal_intf.c:354-358); 0x22 & 0x13
    is 0x02, which survives that and fails the TX/RX count screen (:364-368). wifit3 keeps the
    adapter for RX with the pre AND state, so the 1 antenna report survives and max_tx_cnt stays 1;
    falling back to 0x33 / 2 would read the 2T diff bytes on a one TX path part."""
    with caplog.at_level("ERROR"):
        path = _rfpath(_efuse({0xC9: antenna_opt, 0xC1: 0x01}), ant_num=1)
    assert (path.trx_path_bmp, path.max_tx_cnt) == (0x22, 1)
    assert f"0x{antenna_opt:02x}" in caplog.text


@pytest.mark.parametrize("antenna_opt", [0x22, 0x23])
def test_an_efuse_bitmap_that_misses_a_1t1r_part_falls_back(antenna_opt, caplog):
    """The other family that reaches the same fallback: a 1T1R part restricts 0x33 down to 0x11
    (rtw_chip_rftype_to_hal_rftype hal_intf.c:217-231 -> hal_com.c:1494-1497), and a path B EFUSE
    bitmap then leaves 0x11 & 0x22 = 0x00 or 0x11 & 0x23 = 0x01, an empty TX nibble. The pre AND
    state keeps path A."""
    with caplog.at_level("ERROR"):
        path = _rfpath(_efuse({0xC9: antenna_opt, 0xC1: 0x01}), ant_num=2, rf_2t2r=False)
    assert (path.trx_path_bmp, path.max_tx_cnt) == (0x11, 1)
    assert f"0x{antenna_opt:02x}" in caplog.text


@pytest.mark.parametrize("pg_v, msb, lsb", [
    (0x00, 0, 0), (0x21, 2, 1), (0x77, 7, 7), (0x88, -8, -8),
    (0xF1, -1, 1), (0xFF, -1, -1), (0x1F, 1, -1),
])
def test_pg_diff_nibbles_sign_extend(pg_v, msb, lsb):
    assert pg_txpwr_msb_diff_to_s8bit(pg_v) == msb
    assert pg_txpwr_lsb_diff_to_s8bit(pg_v) == lsb


def test_pg_validity_screens_bases_but_not_blank_diff_nibbles():
    assert is_pg_txpwr_base_invalid(0xFF) is True
    assert is_pg_txpwr_base_invalid(128) is True
    assert is_pg_txpwr_base_invalid(127) is False
    assert is_pg_txpwr_diff_invalid(-1) is False           # a 0xFF diff byte stays in range
    assert is_pg_txpwr_diff_invalid(-8) is False
    assert is_pg_txpwr_diff_invalid(7) is False
    assert is_pg_txpwr_diff_invalid(8) is True
    assert is_pg_txpwr_diff_invalid(-9) is True


# --- rfe_type and the constants.RFE_TYPE override -----------------------------

def test_rfe_type_comes_from_the_efuse_when_no_override_is_set(monkeypatch):
    monkeypatch.setattr(constants_mod, "RFE_TYPE", None)
    assert _efuse({0xCA: 0x15}).rfe_type == 0x15


def test_rfe_type_override_is_checked_before_the_efuse(monkeypatch):
    """The vendor's registry check runs first too [SRC hal/rtl8822c/rtl8822c_ops.c:677-681]."""
    monkeypatch.setattr(constants_mod, "RFE_TYPE", 22)
    assert _efuse({0xCA: 0x15}).rfe_type == 22


def test_rfe_type_override_brings_up_an_unburned_efuse(monkeypatch):
    monkeypatch.setattr(constants_mod, "RFE_TYPE", 2)
    assert _efuse({0xCA: 0xFF}).rfe_type == 2
    assert _efuse_invalid({0xCA: 0x15}).rfe_type == 2


def test_rfe_type_raises_on_an_unburned_efuse_with_no_override(monkeypatch):
    """The one deliberate bring up raise: the vendor fails the probe rather than guess."""
    monkeypatch.setattr(constants_mod, "RFE_TYPE", None)
    with pytest.raises(RuntimeError):
        _efuse({0xCA: 0xFF}).rfe_type
    with pytest.raises(RuntimeError):
        _efuse_invalid({0xCA: 0x15}).rfe_type


def test_the_shipped_override_is_off():
    """A shipped value would force every user's adapter onto one board's front end."""
    assert constants_mod.RFE_TYPE is None


# --- crystal cap: which of the two vendor policies runs -----------------------

@pytest.mark.parametrize("board_option, combo", [
    (0x01, False), (0x21, True), (0x3F, True), (0x41, False), (0xFF, False),
])
def test_bluetooth_coexist_is_0xc1_bits_7_to_5(board_option, combo):
    assert _efuse({0xC1: board_option}).bluetooth_coexist is combo


def test_bluetooth_coexist_needs_a_valid_map():
    assert _efuse_invalid({0xC1: 0x21}).bluetooth_coexist is False


def test_a_wifi_only_board_reads_the_raw_0xb9():
    assert _efuse({0xC1: 0x01, 0xB9: 0x71}).crystal_cap == 0x71
    assert _efuse({0xC1: 0x01, 0xB9: 0xFF}).crystal_cap == EEPROM_DEFAULT_CRYSTAL_CAP_B9
    assert _efuse_invalid({0xB9: 0x71}).crystal_cap == EEPROM_DEFAULT_CRYSTAL_CAP_B9


def test_a_combo_module_takes_the_new_xtal_policy_and_masks_0xb9():
    info = _efuse({0xC1: 0x21, 0xB9: 0xF1, 0x110: 0x40, 0x111: 0x40})
    assert info.crystal_cap == 0x71             # the search hit, 7 bits of 0xB9, not the raw 0xF1


def test_the_new_xtal_policy_falls_back_to_0x110_when_0xb9_is_blank():
    assert _efuse({0xC1: 0x21, 0xB9: 0xFF, 0x110: 0x32, 0x111: 0x32}).crystal_cap == 0x32


@pytest.mark.parametrize("low, high", [(0x32, 0x40), (0x40, 0x32), (0xFF, 0xFF)])
def test_the_new_xtal_policy_defaults_when_either_0x110_half_is_blank(low, high):
    info = _efuse({0xC1: 0x21, 0xB9: 0xFF, 0x110: low, 0x111: high})
    assert info.crystal_cap == EEPROM_DEFAULT_CRYSTAL_CAP_110_8822C


# --- the general_info fields the RF path resolves ------------------------------

@pytest.mark.parametrize("antenna_opt, bmp, tx, rx, rf_type", [
    (0x33, 0x33, 0x3, 0x3, HALMAC_RF_2T2R),
    (0x13, 0x13, 0x1, 0x3, HALMAC_RF_1T2R),
    (0x23, 0x23, 0x2, 0x3, HALMAC_RF_1T2R),
    (0x11, 0x11, 0x1, 0x1, HALMAC_RF_1T1R),
    (0x22, 0x22, 0x2, 0x2, HALMAC_RF_1T1R),
])
def test_rfpath_carries_the_general_info_fields(antenna_opt, bmp, tx, rx, rf_type):
    path = _rfpath(_efuse({0xC9: antenna_opt}))
    assert (path.trx_path_bmp, path.tx_path, path.rx_path) == (bmp, tx, rx)
    assert path.halmac_rf_type == rf_type


def test_a_path_count_the_chip_cannot_reach_takes_the_vendors_invalid_marker():
    assert RfPath(0x70, 3).halmac_rf_type == HALMAC_RF_MAX_TYPE
