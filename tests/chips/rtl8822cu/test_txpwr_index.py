"""RTL8822CU TX power index, computed from a synthetic logical EFUSE map, no hardware.

The headline cross check runs ``hal_com_get_txpwr_idx`` over every channel, both paths and every
rate, and asserts it reproduces what the recorded tables yield. Those literals were
transcribed off a pcap of the vendor driver driving one adapter; these come from the adapter's
EFUSE PG bytes plus the compiled in vendor tables, through the vendor C. Two independent origins,
so agreement is evidence, not a tautology.

Nothing here reads a capture, a dump file, the MAC or the VID/PID.
"""
import pytest

from wifit3.chips.rtl8822cu import txpwr_index
from wifit3.chips.rtl8822cu.constants import (
    DIS_DPD_RATE_ALL,
    DIS_DPD_RATE_NONE,
    HAL_SPEC_PG_TXGI_DIFF_FACTOR,
    HAL_SPEC_TXGI_MAX,
    HAL_SPEC_TXGI_PDBM,
)
from wifit3.chips.rtl8822cu.efuse import EfuseInfo, hal_rfpath_init
from wifit3.chips.rtl8822cu.txpower import BAND_ON_2_4G, BAND_ON_5G
from wifit3.chips.rtl8822cu.txpwr_index import (
    CHANNEL_WIDTH_20,
    CHANNEL_WIDTH_40,
    CHANNEL_WIDTH_80,
    RF_1TX,
    RF_2TX,
    hal_com_get_txpwr_idx,
    hal_txpath_num_nss,
    phy_get_current_tx_num,
    phy_get_pg_txpwr_idx,
    phy_get_txpwr_amends,
    phy_get_txpwr_target,
    rate_idx_to_rs,
    txpwr_idx_state,
)
from wifit3.chips.rtl8822cu.txpwr_tables import CCK, HT_1SS, HT_2SS, OFDM, VHT_1SS, VHT_2SS

from .recorded_txagc import (
    BYRATE_OFFSET,
    DIFF_GROUP_DWORD,
    SECTION_REF_2G,
    SECTION_REF_5G_OFDM,
)

PG_SADDR = 0x10
# The recorded D-Link AC13U PG TX power region, logical 0x10..0x63: path A 2G then 5G, then path B
# 2G and 5G, 18 + 24 bytes each. Only this region.
PG_RECORDED = bytes.fromhex(
    "49484b494c483f3f424243000000ffffffff"
    "484849494a4b4a474444454646450000ffff00ff0000ffff"
    "565858585856464748494a000000ffffffff"
    "494a4b4a484847444342424543460000ffff00ff0000ffff"
)
EEPROM_RF_BOARD_OPTION_RECORDED = 0x01       # 0xC1: EEPROMRegulatory 1, no 1TX bit, no BT coex
TPT_MODE_THERMAL = 0x00                      # 0xC8[7:4] <= 3 selects TXPWR_PG_WITH_PWR_IDX
TPT_MODE_TSSI = 0x40                         # 0xC8[7:4] == 4 selects TXPWR_PG_WITH_TSSI_OFFSET

# DESC_RATE spans of the six rate sections an 8822C stores [SRC include/hal_com.h:33-100].
SECTION_RATES = {
    CCK: range(0x00, 0x04),
    OFDM: range(0x04, 0x0C),
    HT_1SS: range(0x0C, 0x14),
    HT_2SS: range(0x14, 0x1C),
    VHT_1SS: range(0x2C, 0x36),
    VHT_2SS: range(0x36, 0x40),
}
NON_CCK_SECTIONS = {rs: rates for rs, rates in SECTION_RATES.items() if rs != CCK}
DESC_RATE11M, DESC_RATEMCS7 = 0x03, 0x13


def _efuse(pg: bytes = PG_RECORDED, *, tpt_byte: int = TPT_MODE_THERMAL,
           board_option: int = EEPROM_RF_BOARD_OPTION_RECORDED) -> EfuseInfo:
    """A blank logical map carrying the given PG region at 0x10 plus the two scalars that steer it.
    0xC9 stays 0xFF, which Hal_EfuseParsePathSelection does not recognise, so the hal_spec 0x33
    survives, exactly as on the recorded adapter."""
    logical = bytearray(b"\xff" * 768)
    logical[0x00:0x02] = b"\x29\x81"
    logical[0xC1] = board_option
    logical[0xC8] = tpt_byte
    logical[PG_SADDR:PG_SADDR + len(pg)] = pg
    return EfuseInfo(True, True, bytes(logical), b"\xff" * 512)


def _state(efuse: EfuseInfo | None = None, *, ant_num: int = 2, hw_stype: int = 0x00,
           rf_2t2r: bool = True):
    efuse = _efuse() if efuse is None else efuse
    rf_path = hal_rfpath_init(efuse, ant_num=ant_num, hw_stype=hw_stype, rf_2t2r=rf_2t2r)
    return txpwr_idx_state(efuse, rf_path)


def _idx(state, path: int, rs: int, rate_idx: int, channel: int, bw: int = CHANNEL_WIDTH_20) -> int:
    band = BAND_ON_2_4G if channel <= 14 else BAND_ON_5G
    return hal_com_get_txpwr_idx(state, path, rs, rate_idx, bw, band, channel, cch_20=channel)


def _s7(offset: int) -> int:
    """The recorded offsets are unsigned bytes; the four negative ones are 7 bit two's complement,
    which is how config_phydm_write_txagc_diff_8822c writes them
    [SRC hal/phydm/rtl8822c/phydm_hal_api8822c.c:439-442]."""
    return offset - 0x80 if offset & 0x40 else offset


def _frozen_refs(channel: int) -> tuple[tuple[int, int] | None, tuple[int, int]]:
    """(cck_ref, ofdm_ref) per path, from the frozen tables. 5 GHz has no CCK section."""
    if channel <= 14:
        cck_a, cck_b, ofdm_a, ofdm_b = SECTION_REF_2G[channel]
        return (cck_a, cck_b), (ofdm_a, ofdm_b)
    return None, SECTION_REF_5G_OFDM[channel]


def _frozen_index(channel: int, path: int, rs: int, rate_idx: int) -> int:
    cck_ref, ofdm_ref = _frozen_refs(channel)
    ref = cck_ref[path] if rs == CCK else ofdm_ref[path]
    return ref + _s7(BYRATE_OFFSET[rate_idx])


def _sections_of(channel: int) -> dict[int, range]:
    return SECTION_RATES if channel <= 14 else NON_CCK_SECTIONS


@pytest.mark.parametrize("channel", sorted(SECTION_REF_2G) + sorted(SECTION_REF_5G_OFDM))
def test_the_computed_index_reproduces_the_frozen_txagc_value(channel):
    """Every (path, rate) on every channel the frozen tables cover: 39 channels x 2 paths x 48 or
    44 rates."""
    state = _state()
    for path in (0, 1):
        for rs, rates in _sections_of(channel).items():
            for rate_idx in rates:
                assert _idx(state, path, rs, rate_idx, channel) == \
                    _frozen_index(channel, path, rs, rate_idx), \
                    f"ch {channel} path {path} rs {rs} rate 0x{rate_idx:02X}"


@pytest.mark.parametrize("channel", sorted(SECTION_REF_2G) + sorted(SECTION_REF_5G_OFDM))
def test_the_section_reference_indices_reproduce_the_frozen_tables(channel):
    """11M and MCS7 are the section reference rates, where rate_target == rs_target and the amends
    are zero, so the index collapses to the PG base. These are the four values the wire carries in
    the TX AGC reference registers."""
    state = _state()
    cck_ref, ofdm_ref = _frozen_refs(channel)
    for path in (0, 1):
        if cck_ref is not None:
            assert _idx(state, path, CCK, DESC_RATE11M, channel) == cck_ref[path]
        assert _idx(state, path, HT_1SS, DESC_RATEMCS7, channel) == ofdm_ref[path]


@pytest.mark.parametrize("channel", sorted(SECTION_REF_2G))
def test_the_wire_diff_dwords_reproduce_the_frozen_table(channel):
    """What config_phydm_set_txagc_to_hw_8822c actually writes: MIN over the two paths of
    (index - section reference), four 7 bit diffs per dword. 2.4 GHz only, because the CCK section
    never runs on 5 GHz and those four entries are a carry forward, not a computation."""
    state = _state()
    diff = {}
    for path in (0, 1):
        # The two references the hardware holds: 11M for the CCK rates, MCS7 for every other rate
        # including the legacy OFDM and both VHT sections [SRC phydm_hal_api8822c.c:481-484].
        cck_ref = _idx(state, path, CCK, DESC_RATE11M, channel)
        ofdm_ref = _idx(state, path, HT_1SS, DESC_RATEMCS7, channel)
        for rs, rates in SECTION_RATES.items():
            ref = cck_ref if rs == CCK else ofdm_ref
            for rate_idx in rates:
                diff.setdefault(rate_idx, []).append(_idx(state, path, rs, rate_idx, channel) - ref)

    for base, expected in DIFF_GROUP_DWORD.items():
        dword = 0
        for k in range(4):
            dword |= (min(diff[base + k]) & 0x7F) << (8 * k)
        assert dword == expected, f"ch {channel} dword 0x{base:02X}"


def test_a_tssi_unit_yields_no_state_at_all():
    """0xC8[7:4] == 4 puts the chip on the TSSI path: hal_load_txpwr_info has no PG bases to give
    and the caller must skip the TX power write rather than substitute a value."""
    assert _state(_efuse(tpt_byte=TPT_MODE_TSSI)) is None


def test_an_unburned_efuse_logs_and_still_computes(caplog):
    """An all 0xFF map cannot TX, but wifit3 keeps it for RX, so nothing may raise. The PG region
    falls through to rtl8822c_pg_txpwr_def_info, whose bases are all 0x33."""
    blank = EfuseInfo(True, False, b"\xff" * 768, b"\xff" * 512)
    with caplog.at_level("ERROR"):
        state = txpwr_idx_state(blank, hal_rfpath_init(blank, ant_num=2, hw_stype=0x00,
                                                       rf_2t2r=True))

    assert state is not None
    assert "0xC8=0xff" in caplog.text
    # Bases fall through to the IC default's 0x33; the diffs do not, because a 0xFF nibble sign
    # extends to a valid -1 and is scaled by pg_txgi_diff_factor to -2. CCK_Diff[1T] is one of the
    # three slots hal_init_pg_txpwr_info_2g hard zeroes before any source runs.
    assert _idx(state, 0, CCK, DESC_RATE11M, 1) == 0x33
    assert _idx(state, 0, HT_1SS, DESC_RATEMCS7, 1) == 0x33 - 2
    assert _idx(state, 0, OFDM, 0x0B, 1) == 0x33 - 2


def test_an_unburned_efuse_never_raises_on_any_channel_path_or_rate():
    blank = EfuseInfo(True, False, b"\xff" * 768, b"\xff" * 512)
    state = txpwr_idx_state(blank, hal_rfpath_init(blank, ant_num=2, hw_stype=0x00, rf_2t2r=True))

    for channel in sorted(SECTION_REF_2G) + sorted(SECTION_REF_5G_OFDM):
        for path in (0, 1):
            for rs, rates in _sections_of(channel).items():
                for rate_idx in rates:
                    assert 0 <= _idx(state, path, rs, rate_idx, channel) <= HAL_SPEC_TXGI_MAX


def test_a_one_tx_part_skips_the_two_stream_pg_diff():
    """0xC1[2] de rates the board to one TX. The PG walk then never reads the 2T/2S diff bytes, so
    a programmed 2S BW20 diff moves the 2SS PG base at max_tx_cnt 2 and not at 1."""
    pg = bytearray(PG_RECORDED)
    pg[12] = 0x03                            # 0x1C: [7:4] BW40 2S = 0, [3:0] BW20 2S = 3
    two_tx = _state(_efuse(bytes(pg)))
    one_tx = _state(_efuse(bytes(pg), board_option=EEPROM_RF_BOARD_OPTION_RECORDED | 0x04))

    def pg_base(state, rs):
        return phy_get_pg_txpwr_idx(state.hal_txpwr, 0, rs, RF_2TX, CHANNEL_WIDTH_20,
                                    BAND_ON_2_4G, 1, 0, cch_20=1)

    bw40_base = two_tx.hal_txpwr.Index24G_BW40_Base[0][0]
    assert pg_base(two_tx, HT_2SS) == bw40_base + 3 * HAL_SPEC_PG_TXGI_DIFF_FACTOR
    assert pg_base(one_tx, HT_2SS) == bw40_base
    # The BW20 diff keys off the section's tx_num, not ntx_idx, so a 1SS section skips it even when
    # the rate is being sent on two paths [SRC hal/hal_com_phycfg.c:2501-2503].
    assert pg_base(two_tx, HT_1SS) == bw40_base


def test_a_one_stream_part_stores_no_two_stream_section_target():
    """max_tx_cnt 1 takes hal_data->tx_nss to 1, so phy_store_target_tx_power skips HT_2SS and
    VHT_2SS. The C then subtracts a zero rs_target from those rates and the index saturates; the
    1SS sections are untouched."""
    one_tx = _state(_efuse(board_option=EEPROM_RF_BOARD_OPTION_RECORDED | 0x04))

    assert one_tx.by_rate.phy_get_target_txpwr(BAND_ON_2_4G, 0, HT_2SS) == 0
    assert _idx(one_tx, 0, HT_2SS, 0x14, 1) == HAL_SPEC_TXGI_MAX
    assert _idx(one_tx, 0, HT_1SS, 0x0C, 1) == _frozen_index(1, 0, HT_1SS, 0x0C)


def test_a_one_tx_part_still_reports_two_paths_for_two_stream_rates():
    """tx_path_nss_set_default writes txpath_num_nss[i - 1] = i outside its path count loop
    [SRC core/rtw_rf.c:2208], so the 2SS entry is 2 even on a one TX bitmap. Pinned because it
    looks like a bug and a porter would be tempted to 'fix' it."""
    assert hal_txpath_num_nss(0x22, 1) == (1, 2, 3, 4)
    assert hal_txpath_num_nss(0x33, 2) == (1, 2, 3, 4)


@pytest.mark.parametrize("trx_path_bmp", [0x30, 0x03])
def test_a_half_empty_path_bitmap_logs_and_puts_every_rate_on_one_tx(caplog, trx_path_bmp):
    """Exactly one empty nibble is the C's abort."""
    with caplog.at_level("ERROR"):
        assert hal_txpath_num_nss(trx_path_bmp, 2) == (0, 0, 0, 0)
    assert "no TX path or no RX path" in caplog.text
    assert phy_get_current_tx_num((0, 0, 0, 0), 0x14) == RF_1TX


@pytest.mark.parametrize("rf_2t2r", [True, False])
def test_a_wholly_empty_path_bitmap_falls_back_to_the_rf_type_default(caplog, rf_2t2r):
    """BOTH nibbles empty is NOT the abort: rtw_hal_get_trx_path substitutes the rf_type default
    first [SRC hal/hal_com.c:17571-17572], so the walk runs and yields the set_default counts."""
    with caplog.at_level("ERROR"):
        assert hal_txpath_num_nss(0x00, 2, rf_2t2r=rf_2t2r) == (1, 2, 3, 4)
    assert caplog.text == ""


def test_the_tx_npath_option_puts_one_stream_rates_on_two_paths(monkeypatch):
    """registrypriv.tx_npath defaults to 0, but CONFIG_RTW_TX_NPATH_EN is compiled in, so the
    branch is live [SRC hal/hal_dm.c:1497-1500]."""
    monkeypatch.setattr(txpwr_index, "REG_TX_NPATH", 1)
    assert hal_txpath_num_nss(0x33, 2) == (2, 2, 3, 4)


def test_the_tx_npath_option_never_reaches_the_one_tx_arm(monkeypatch):
    """The C consults tx_npath only in case 2 of switch (max_tx_cnt); case 1 takes BB_PATH_A
    unconditionally [SRC hal/hal_dm.c:1509-1516]."""
    monkeypatch.setattr(txpwr_index, "REG_TX_NPATH", 1)
    assert hal_txpath_num_nss(0x33, 1) == (1, 2, 3, 4)


def test_the_wholly_empty_fallback_really_yields_the_two_path_bitmap(monkeypatch):
    """The rf_type default must be BB_PATH_AB for 2T2R, which only the tx_npath arm can reveal."""
    monkeypatch.setattr(txpwr_index, "REG_TX_NPATH", 1)
    assert hal_txpath_num_nss(0x00, 2, rf_2t2r=True) == (2, 2, 3, 4)
    assert hal_txpath_num_nss(0x00, 2, rf_2t2r=False) == (1, 2, 3, 4)


def test_an_out_of_range_max_tx_cnt_logs_and_keeps_the_default_counts(caplog):
    with caplog.at_level("ERROR"):
        assert hal_txpath_num_nss(0x33, 3) == (1, 2, 3, 4)
    assert "invalid max_tx_cnt" in caplog.text


@pytest.mark.parametrize("rate_idx,expected", [
    (0x00, RF_1TX), (0x0B, RF_1TX), (0x13, RF_1TX), (0x14, RF_2TX), (0x1B, RF_2TX),
    (0x2C, RF_1TX), (0x35, RF_1TX), (0x36, RF_2TX), (0x3F, RF_2TX),
])
def test_the_current_tx_num_follows_the_rate_stream_count(rate_idx, expected):
    assert phy_get_current_tx_num((1, 2, 3, 4), rate_idx) == expected


@pytest.mark.parametrize("rate_idx,rs", [
    (0x00, CCK), (0x03, CCK), (0x04, OFDM), (0x0B, OFDM), (0x0C, HT_1SS), (0x13, HT_1SS),
    (0x14, HT_2SS), (0x1B, HT_2SS), (0x2C, VHT_1SS), (0x35, VHT_1SS), (0x36, VHT_2SS),
    (0x3F, VHT_2SS), (0x1C, 4), (0x24, 5), (0x40, 8), (0x53, 9),
])
def test_the_rate_index_resolves_to_its_section(rate_idx, rs):
    assert rate_idx_to_rs(rate_idx) == rs


def test_an_out_of_range_rate_index_is_a_caller_bug():
    with pytest.raises(ValueError, match="no rate section"):
        rate_idx_to_rs(0x54)


def test_cck_outside_two_point_four_gigahertz_amends_nothing():
    """phy_get_txpwr_amends' first line exits before the DPD term when a CCK rate is asked for on
    5 GHz, and phy_get_txpwr_target exits before the by rate term."""
    state = _state()
    assert phy_get_txpwr_amends(DIS_DPD_RATE_ALL, 0, CCK, 0x00, RF_1TX,
                                CHANNEL_WIDTH_20, BAND_ON_5G, 36) == 0
    assert phy_get_txpwr_target(state.by_rate, 0, CCK, 0x00, RF_1TX,
                                CHANNEL_WIDTH_20, BAND_ON_5G, 36, 0) == 0
    assert _idx(state, 0, CCK, 0x00, 36) == 0


def test_the_dpd_amend_is_negative_and_scaled_by_txgi_pdbm():
    """The ten flagged rates lose 3 dB, which is 3 * txgi_pdbm gain index units."""
    flagged = 0x04                                   # 6M, dis_dpd_rate BIT(0)
    assert phy_get_txpwr_amends(DIS_DPD_RATE_ALL, 0, OFDM, flagged, RF_1TX,
                                CHANNEL_WIDTH_20, BAND_ON_2_4G, 1) == -3 * HAL_SPEC_TXGI_PDBM
    assert phy_get_txpwr_amends(DIS_DPD_RATE_NONE, 0, OFDM, flagged, RF_1TX,
                                CHANNEL_WIDTH_20, BAND_ON_2_4G, 1) == 0
    assert phy_get_txpwr_amends(DIS_DPD_RATE_ALL, 0, OFDM, 0x06, RF_1TX,
                                CHANNEL_WIDTH_20, BAND_ON_2_4G, 1) == 0


def test_a_tssi_unit_would_lose_the_dpd_amend_on_every_flagged_rate():
    """dis_dpd_rate is derived from txpwr_pg_mode, never written as a literal 0x3FF."""
    assert _efuse().dis_dpd_rate == DIS_DPD_RATE_ALL
    assert _efuse(tpt_byte=TPT_MODE_TSSI).dis_dpd_rate == DIS_DPD_RATE_NONE


def test_the_index_clamps_to_the_gain_index_range():
    """A PG base near the top plus a positive by rate offset must stop at txgi_max, and a base of
    zero plus a negative one must stop at 0."""
    high = bytearray(PG_RECORDED)
    high[6] = 0x7F                           # 0x16: path A 2G BW40 base, group 0
    state = _state(_efuse(bytes(high)))
    assert _idx(state, 0, HT_1SS, 0x0C, 1) == HAL_SPEC_TXGI_MAX

    low = bytearray(PG_RECORDED)
    low[6] = 0x00
    state = _state(_efuse(bytes(low)))
    assert _idx(state, 0, VHT_1SS, 0x35, 1) == 0     # VHT1SS MCS9 sits 8 units under its section


def test_the_pg_base_returns_zero_for_cck_on_five_gigahertz(caplog):
    state = _state()
    with caplog.at_level("WARNING"):
        base = phy_get_pg_txpwr_idx(state.hal_txpwr, 0, CCK, RF_1TX, CHANNEL_WIDTH_20,
                                    BAND_ON_5G, 36, 0, cch_20=36)
    assert base == 0
    assert "CCK on 5 GHz" in caplog.text


def test_the_ofdm_section_resolves_the_channel_to_cch_twenty():
    """phy_get_pg_txpwr_idx replaces the center channel with hal_data->cch_20 for the CCK and OFDM
    sections only, which is a no op at BW20 and a real substitution above it."""
    state = _state()
    at_ch6 = phy_get_pg_txpwr_idx(state.hal_txpwr, 0, OFDM, RF_1TX, CHANNEL_WIDTH_40,
                                  BAND_ON_2_4G, 3, 0, cch_20=6)
    assert at_ch6 == state.hal_txpwr.Index24G_BW40_Base[0][5]
    at_ch3 = phy_get_pg_txpwr_idx(state.hal_txpwr, 0, HT_1SS, RF_1TX, CHANNEL_WIDTH_40,
                                  BAND_ON_2_4G, 3, 0, cch_20=6)
    assert at_ch3 == state.hal_txpwr.Index24G_BW40_Base[0][2]


def test_the_eighty_megahertz_base_comes_from_its_own_table():
    state = _state()
    base = phy_get_pg_txpwr_idx(state.hal_txpwr, 0, VHT_1SS, RF_1TX, CHANNEL_WIDTH_80,
                                BAND_ON_5G, 42, 0, cch_20=36)
    assert base == state.hal_txpwr.Index5G_BW80_Base[0][0]


def test_a_channel_that_is_no_eighty_megahertz_center_logs_and_yields_zero(caplog):
    state = _state()
    with caplog.at_level("WARNING"):
        base = phy_get_pg_txpwr_idx(state.hal_txpwr, 0, VHT_1SS, RF_1TX, CHANNEL_WIDTH_80,
                                    BAND_ON_5G, 36, 0, cch_20=36)
    assert base == 0
    assert "no 80 MHz center channel" in caplog.text
