"""RTL8822CU power by rate tables, derived from the embedded vendor phy_reg_pg array.

The headline test cross checks the derivation against the recorded ``BYRATE_OFFSET``.
Those literals were transcribed off a pcap of the vendor driver driving one adapter; these come
from ``array_mp_8822c_phy_reg_pg`` plus the vendor formula. Two independent origins, so agreement
is evidence, not a tautology.
"""
import pytest

from wifit3.chips.rtl8822cu.constants import (
    DIS_DPD_RATE_ALL,
    DIS_DPD_RATE_NONE,
    HAL_SPEC_TXGI_MAX,
    HAL_SPEC_TXGI_PDBM,
)
from wifit3.chips.rtl8822cu.txpwr_tables import (
    ARRAY_MP_8822C_PHY_REG_PG,
    BAND_ON_2_4G,
    BAND_ON_5G,
    CCK,
    HT_1SS,
    HT_2SS,
    HT_3SS,
    OFDM,
    PHY_REG_PG_ROW_LEN,
    TX_PWR_BY_RATE_NUM_RATE,
    VHT_1SS,
    VHT_2SS,
    _phy_get_rate_values_of_txpwr_by_rate,
    _phy_txpwr_by_rate_chk_for_path_dup,
    hal_tx_nss,
    phy_is_tx_power_by_rate_needed,
    phy_load_tx_power_by_rate,
    rtl8822c_get_dis_dpd_by_rate_diff,
)

from .recorded_txagc import BYRATE_OFFSET, DIFF_GROUP_DWORD

EEPROM_REGULATORY_RECORDED = 0x01     # EFUSE 0xC1 & 0x3 on the recorded 2001:3329

# DESC_RATE index ranges of the six rate sections the 8822C stores, from the DESC_RATE numbering
# [SRC include/hal_com.h:33-100] grouped as IS_CCK_RATE / IS_OFDM_RATE / IS_HT1SS_RATE /
# IS_HT2SS_RATE / IS_VHT1SS_RATE / IS_VHT2SS_RATE [SRC include/ieee80211.h:956-965].
SECTION_RATES = {
    CCK: range(0x00, 0x04),
    OFDM: range(0x04, 0x0C),
    HT_1SS: range(0x0C, 0x14),
    HT_2SS: range(0x14, 0x1C),
    VHT_1SS: range(0x2C, 0x36),
    VHT_2SS: range(0x36, 0x40),
}


def _tables(eeprom_regulatory=EEPROM_REGULATORY_RECORDED, tx_nss=2):
    return phy_load_tx_power_by_rate(eeprom_regulatory=eeprom_regulatory, tx_nss=tx_nss)


def _byrate_offset(tables, band, path, rs, rate_idx, dis_dpd_rate):
    """One slot of the power by rate offset the txagc diff table carries: the rate's target minus
    its section target, plus the amends. All limit terms are txgi_max and btc/extra/tpc are zero,
    so rate_target collapses to the by rate value [SRC hal/hal_com_phycfg.c:6115-6194], and the
    8822C's only amend is the DPD one [SRC :6208-6242]. [SRC hal_com_get_txpwr_idx :6295]"""
    rate_target = tables.phy_get_txpwr_by_rate(band, path, rs, rate_idx)
    rs_target = tables.phy_get_target_txpwr(band, path, rs)
    amends = -(rtl8822c_get_dis_dpd_by_rate_diff(dis_dpd_rate, rate_idx) * HAL_SPEC_TXGI_PDBM)
    return (rate_target - rs_target + amends) & 0x7F


def test_the_embedded_vendor_array_keeps_its_shape_and_checksum():
    rows = ARRAY_MP_8822C_PHY_REG_PG
    assert len(rows) == 276
    assert len(rows) % PHY_REG_PG_ROW_LEN == 0
    assert len(rows) // PHY_REG_PG_ROW_LEN == 46
    assert sum(rows) == 0x3B1DC83E37
    for i in range(0, len(rows), PHY_REG_PG_ROW_LEN):
        band, rf_path, tx_num, addr, bitmask, _data = rows[i:i + PHY_REG_PG_ROW_LEN]
        assert band in (0, 1)
        assert rf_path in (0, 1)
        assert tx_num in (0, 1)
        assert bitmask == 0xFFFFFFFF
        assert addr in range(0xC20, 0xC50, 4) or addr in range(0xE20, 0xE50, 4)


def test_a_register_row_unpacks_lowest_byte_first():
    assert _phy_get_rate_values_of_txpwr_by_rate(0xC20, 0x484C5054) == [
        (0x00, 0x54), (0x01, 0x50), (0x02, 0x4C), (0x03, 0x48)]


def test_a_power_byte_above_127_is_read_as_signed():
    assert _phy_get_rate_values_of_txpwr_by_rate(0xC20, 0x00000080) == [
        (0x00, -128), (0x01, 0), (0x02, 0), (0x03, 0)]


def test_an_unmapped_register_address_is_rejected():
    with pytest.raises(ValueError, match="0xC50"):
        _phy_get_rate_values_of_txpwr_by_rate(0xC50, 0)


def test_the_by_rate_offsets_reproduce_the_captured_diff_table():
    """The cross check: every 2.4 GHz slot of the recorded BYRATE_OFFSET, recomputed from the
    vendor array. dis_dpd_rate is 0x3FF because the recorded EFUSE 0xC8 nibble picks PWR_IDX."""
    tables = _tables()
    derived = {}
    for rs, rates in SECTION_RATES.items():
        for rate_idx in rates:
            derived[rate_idx] = _byrate_offset(tables, BAND_ON_2_4G, 0, rs, rate_idx,
                                               DIS_DPD_RATE_ALL)
    assert derived == BYRATE_OFFSET
    assert len(derived) == 48


def test_the_by_rate_offsets_pack_back_into_the_captured_diff_dwords():
    tables = _tables()
    section_of = {rate: rs for rs, rates in SECTION_RATES.items() for rate in rates}
    packed = {}
    for base in DIFF_GROUP_DWORD:
        dword = 0
        for k in range(4):
            rate_idx = base + k
            offset = _byrate_offset(tables, BAND_ON_2_4G, 0, section_of[rate_idx], rate_idx,
                                    DIS_DPD_RATE_ALL)
            dword |= offset << (8 * k)
        packed[base] = dword
    assert packed == DIFF_GROUP_DWORD


def test_both_paths_carry_the_same_offsets_which_is_why_the_min_over_paths_is_a_no_op():
    tables = _tables()
    for rs, rates in SECTION_RATES.items():
        for rate_idx in rates:
            a = _byrate_offset(tables, BAND_ON_2_4G, 0, rs, rate_idx, DIS_DPD_RATE_ALL)
            b = _byrate_offset(tables, BAND_ON_2_4G, 1, rs, rate_idx, DIS_DPD_RATE_ALL)
            assert a == b


def test_the_5ghz_band_reproduces_the_same_offsets_for_every_non_cck_rate():
    tables = _tables()
    for rs, rates in SECTION_RATES.items():
        if rs == CCK:
            continue
        for rate_idx in rates:
            for path in (0, 1):
                got = _byrate_offset(tables, BAND_ON_5G, path, rs, rate_idx, DIS_DPD_RATE_ALL)
                assert got == BYRATE_OFFSET[rate_idx]


def test_a_tssi_unit_gets_no_dpd_subtraction():
    """dis_dpd_rate is 0x0 when EFUSE 0xC8 selects TSSI, so the ten flagged rates sit 3 dB (12 txgi
    units) above the frozen thermal mode values and nothing else moves. Unreachable on the recorded
    adapter, and the reason this cluster computes rather than freezes."""
    tables = _tables()
    dpd_rates = {0x04, 0x05, 0x0C, 0x0D, 0x14, 0x15, 0x2C, 0x2D, 0x36, 0x37}
    moved = set()
    for rs, rates in SECTION_RATES.items():
        for rate_idx in rates:
            got = _byrate_offset(tables, BAND_ON_2_4G, 0, rs, rate_idx, DIS_DPD_RATE_NONE)
            frozen = BYRATE_OFFSET[rate_idx]
            if got != frozen:
                moved.add(rate_idx)
                assert got == (frozen + 3 * HAL_SPEC_TXGI_PDBM) & 0x7F
    assert moved == dpd_rates


def test_only_the_ten_flagged_rates_take_the_dpd_amend():
    flagged = {rate for rate in range(TX_PWR_BY_RATE_NUM_RATE)
               if rtl8822c_get_dis_dpd_by_rate_diff(DIS_DPD_RATE_ALL, rate)}
    assert flagged == {0x04, 0x05, 0x0C, 0x0D, 0x14, 0x15, 0x2C, 0x2D, 0x36, 0x37}
    assert all(rtl8822c_get_dis_dpd_by_rate_diff(DIS_DPD_RATE_NONE, rate) == 0
               for rate in range(TX_PWR_BY_RATE_NUM_RATE))


def test_a_single_dis_dpd_bit_moves_only_its_own_rate():
    assert rtl8822c_get_dis_dpd_by_rate_diff(1 << 6, 0x2C) == 3
    assert rtl8822c_get_dis_dpd_by_rate_diff(1 << 6, 0x2D) == 0


def test_the_section_targets_are_the_reference_rate_by_rate_values():
    tables = _tables()
    for path in (0, 1):
        assert tables.phy_get_target_txpwr(BAND_ON_2_4G, path, CCK) == 0x48       # 11M
        assert tables.phy_get_target_txpwr(BAND_ON_2_4G, path, OFDM) == 0x44      # 54M
        assert tables.phy_get_target_txpwr(BAND_ON_2_4G, path, HT_1SS) == 0x40    # MCS7
        assert tables.phy_get_target_txpwr(BAND_ON_2_4G, path, HT_2SS) == 0x40    # MCS15
        assert tables.phy_get_target_txpwr(BAND_ON_2_4G, path, VHT_1SS) == 0x40
        assert tables.phy_get_target_txpwr(BAND_ON_2_4G, path, VHT_2SS) == 0x40
        assert tables.phy_get_target_txpwr(BAND_ON_5G, path, OFDM) == 0x44


def test_5ghz_has_no_cck_rows_so_those_slots_stay_at_txgi_max():
    tables = _tables()
    for path in (0, 1):
        for rate_idx in SECTION_RATES[CCK]:
            assert tables.tx_pwr_by_rate[BAND_ON_5G][path][rate_idx] == HAL_SPEC_TXGI_MAX
            assert tables.tx_pwr_by_rate[BAND_ON_2_4G][path][rate_idx] != HAL_SPEC_TXGI_MAX
        assert tables.phy_get_target_txpwr(BAND_ON_5G, path, CCK) == 0


def test_rates_the_8822c_never_transmits_stay_at_txgi_max():
    tables = _tables()
    for rate_idx in list(range(0x1C, 0x2C)) + list(range(0x40, TX_PWR_BY_RATE_NUM_RATE)):
        assert tables.tx_pwr_by_rate[BAND_ON_2_4G][0][rate_idx] == HAL_SPEC_TXGI_MAX


def test_a_one_stream_unit_stores_no_two_stream_section_target():
    tables = _tables(tx_nss=hal_tx_nss(max_tx_cnt=1))
    assert tables.phy_get_target_txpwr(BAND_ON_2_4G, 0, HT_1SS) == 0x40
    assert tables.phy_get_target_txpwr(BAND_ON_2_4G, 0, HT_2SS) == 0
    assert tables.phy_get_target_txpwr(BAND_ON_2_4G, 0, VHT_2SS) == 0


def test_a_two_stream_unit_still_skips_the_three_stream_sections():
    tables = _tables()
    assert hal_tx_nss(max_tx_cnt=2) == 2
    assert tables.phy_get_target_txpwr(BAND_ON_2_4G, 0, HT_3SS) == 0


def test_an_undefined_path_takes_a_copy_of_the_lowest_defined_one():
    by_rate = [[[HAL_SPEC_TXGI_MAX] * TX_PWR_BY_RATE_NUM_RATE for _ in range(4)] for _ in range(2)]
    for band in (BAND_ON_2_4G, BAND_ON_5G):
        by_rate[band][0][0] = 0x11
    _phy_txpwr_by_rate_chk_for_path_dup(by_rate)
    assert by_rate[BAND_ON_2_4G][1] == by_rate[BAND_ON_2_4G][0]
    assert by_rate[BAND_ON_5G][1][0] == 0x11


def test_a_band_with_no_defined_path_is_an_error():
    by_rate = [[[HAL_SPEC_TXGI_MAX] * TX_PWR_BY_RATE_NUM_RATE for _ in range(4)] for _ in range(2)]
    with pytest.raises(RuntimeError, match="undefined on every path"):
        _phy_txpwr_by_rate_chk_for_path_dup(by_rate)


@pytest.mark.parametrize("eeprom_regulatory", [0, 1, 2, 3])
def test_the_vendor_build_leaves_power_by_rate_on_for_every_regulatory_value(eeprom_regulatory):
    """RegEnableTxPowerByRate is 1, not the 'depend on efuse' 2, so EEPROMRegulatory never
    disables the stage. [SRC Makefile:100, hal/hal_com_phycfg.c:4337-4347]"""
    assert phy_is_tx_power_by_rate_needed(eeprom_regulatory)
    assert _tables(eeprom_regulatory=eeprom_regulatory).by_rate_needed


def test_with_the_by_rate_stage_off_every_rate_collapses_to_its_section_target():
    tables = _tables()
    off = type(tables)(tables.tx_pwr_by_rate, tables.target_txpwr_2g, tables.target_txpwr_5g,
                       by_rate_needed=False)
    for rs, rates in SECTION_RATES.items():
        for rate_idx in rates:
            assert off.phy_get_txpwr_by_rate(BAND_ON_2_4G, 0, rs, rate_idx) == \
                off.phy_get_target_txpwr(BAND_ON_2_4G, 0, rs)
