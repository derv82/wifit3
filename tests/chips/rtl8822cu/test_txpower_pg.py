"""RTL8822CU PG TX power base tables, computed from synthetic logical EFUSE maps, no hardware.

The 2001:3329 PG bytes are inlined below; nothing here reads a capture or a dump file.
"""
import pytest

from wifit3.chips.rtl8822cu.constants import PG_TXPWR_INVALID_DIFF
from wifit3.chips.rtl8822cu.efuse import EfuseInfo
from wifit3.chips.rtl8822cu.txpower import (
    BAND_MAX,
    BAND_ON_2_4G,
    BAND_ON_5G,
    hal_load_pg_txpwr_info,
    hal_load_txpwr_info,
    phy_get_ch_idx,
    rtw_get_ch_group,
)

from .recorded_txagc import SECTION_REF_2G, SECTION_REF_5G_OFDM

PG_SADDR = 0x10
# The recorded D-Link AC13U (2001:3329) PG TX power region, logical 0x10..0x63: path A 2G then
# 5G, then path B 2G and 5G, 18 + 24 bytes each. Only this region, no MAC and no VID/PID.
PG_2001_3329 = bytes.fromhex(
    "49484b494c483f3f424243000000ffffffff"
    "484849494a4b4a474444454646450000ffff00ff0000ffff"
    "565858585856464748494a000000ffffffff"
    "494a4b4a484847444342424543460000ffff00ff0000ffff"
)
# Path A 2.4 GHz byte offsets [SRC hal/hal_com_phycfg.c:746-873].
A2G_CCK_G0 = PG_SADDR + 0
A2G_DIFF_1S = PG_SADDR + 11          # [7:4] BW20 1S, [3:0] OFDM 1T
A2G_DIFF_2S = PG_SADDR + 12          # [7:4] BW40 2S, [3:0] BW20 2S
A2G_DIFF_2T = PG_SADDR + 13          # [7:4] OFDM 2T, [3:0] CCK 2T
# Path A 5 GHz byte offsets [SRC hal/hal_com_phycfg.c:875-1033].
A5G_BASE = PG_SADDR + 18
A5G_DIFF_3S = A5G_BASE + 16          # read only when max_tx_cnt >= 3
A5G_DIFF_4S = A5G_BASE + 17
A5G_OFDM_2T = A5G_BASE + 18          # [7:4] OFDM 2T, four bytes past the other 2T diffs


def _efuse(pg: bytes = PG_2001_3329, tpt_byte: int = 0x00) -> EfuseInfo:
    """A blank logical map carrying the given PG region at 0x10, with 0xC8 selecting PWR_IDX."""
    logical = bytearray(b"\xff" * 768)
    logical[0x00:0x02] = b"\x29\x81"
    logical[0xC8] = tpt_byte
    logical[PG_SADDR:PG_SADDR + len(pg)] = pg
    return EfuseInfo(True, True, bytes(logical), b"\xff" * 512)


def _mutated(edits: dict[int, int]) -> EfuseInfo:
    pg = bytearray(PG_2001_3329)
    for offset, value in edits.items():
        pg[offset - PG_SADDR] = value
    return _efuse(bytes(pg))


def _hal(efuse: EfuseInfo | None = None, max_tx_cnt: int = 2):
    return hal_load_txpwr_info(_efuse() if efuse is None else efuse, max_tx_cnt)


@pytest.mark.parametrize("channel", sorted(SECTION_REF_2G))
def test_recorded_pg_base_reproduces_the_captured_2g_section_reference(channel):
    """The frozen table came off this adapter's wire; these values come from its EFUSE PG bytes
    through the vendor C. Two independent derivations of the same four numbers per channel."""
    hal = _hal()
    cck_a, cck_b, ofdm_a, ofdm_b = SECTION_REF_2G[channel]

    assert hal.Index24G_CCK_Base[0][channel - 1] == cck_a
    assert hal.Index24G_CCK_Base[1][channel - 1] == cck_b
    assert hal.Index24G_BW40_Base[0][channel - 1] == ofdm_a
    assert hal.Index24G_BW40_Base[1][channel - 1] == ofdm_b


@pytest.mark.parametrize("channel", sorted(SECTION_REF_5G_OFDM))
def test_recorded_pg_base_reproduces_the_captured_5g_section_reference(channel):
    hal = _hal()
    ch_idx, in_24g = phy_get_ch_idx(channel)
    ofdm_a, ofdm_b = SECTION_REF_5G_OFDM[channel]

    assert not in_24g
    assert hal.Index5G_BW40_Base[0][ch_idx] == ofdm_a
    assert hal.Index5G_BW40_Base[1][ch_idx] == ofdm_b


def test_the_recorded_device_has_no_pg_diffs_at_all():
    """Why the base alone reproduces the wire: every diff this EFUSE programs is zero, so
    phy_get_pg_txpwr_idx adds nothing to the base in any section."""
    hal = _hal()

    for path in (0, 1):
        for tx_idx in (0, 1):
            assert hal.CCK_24G_Diff[path][tx_idx] == 0
            assert hal.OFDM_24G_Diff[path][tx_idx] == 0
            assert hal.BW20_24G_Diff[path][tx_idx] == 0
            assert hal.BW40_24G_Diff[path][tx_idx] == 0
            assert hal.OFDM_5G_Diff[path][tx_idx] == 0
            assert hal.BW20_5G_Diff[path][tx_idx] == 0
            assert hal.BW40_5G_Diff[path][tx_idx] == 0
            assert hal.BW80_5G_Diff[path][tx_idx] == 0


def test_channel_14_takes_cck_group_5_and_bw40_group_4():
    """The one channel whose CCK and BW40 bases come from different groups
    [SRC hal/hal_com_phycfg.c:1222-1225]."""
    hal = _hal()

    assert hal.Index24G_CCK_Base[0][13] == PG_2001_3329[5]        # CCK group 5
    assert hal.Index24G_BW40_Base[0][13] == PG_2001_3329[6 + 4]   # BW40 group 4
    assert hal.Index24G_CCK_Base[0][13] != hal.Index24G_CCK_Base[0][12]


def test_an_unprogrammed_pg_region_takes_every_base_from_the_ic_default():
    """All 0xFF bases are > txgi_max, so the second source fills them with 0x33
    [SRC rtl8822c_pg_txpwr_def_info hal/hal_com_phycfg.c:401-409]."""
    hal = _hal(_efuse(b"\xff" * 84))

    for path in (0, 1):
        assert set(hal.Index24G_CCK_Base[path]) == {0x33}
        assert set(hal.Index24G_BW40_Base[path]) == {0x33}
        assert set(hal.Index5G_BW40_Base[path]) == {0x33}
        assert set(hal.Index5G_BW80_Base[path]) == {0x33}


def test_an_unprogrammed_pg_region_still_takes_its_diffs_from_efuse():
    """A 0xFF diff byte sign extends to -1/-1, which is inside -8..7 and therefore VALID, so the
    IC default's diff nibbles are unreachable. Only bases ever fall through.
    [SRC IS_PG_TXPWR_DIFF_INVALID hal/hal_com_phycfg.c:31, PG_TXPWR_LSB_DIFF_TO_S8BIT :29]"""
    hal = _hal(_efuse(b"\xff" * 84))

    assert hal.OFDM_24G_Diff[0][0] == -2          # -1 nibble x pg_txgi_diff_factor
    assert hal.BW20_24G_Diff[0][0] == -2
    assert hal.OFDM_5G_Diff[0][0] == -2
    assert hal.CCK_24G_Diff[0][0] == 0            # dummy zero, pre set before any source runs
    assert hal.BW40_24G_Diff[0][0] == 0
    assert hal.BW40_5G_Diff[0][0] == 0


def test_a_blank_path_b_falls_back_while_path_a_keeps_its_efuse():
    hal = _hal(_efuse(PG_2001_3329[:42] + b"\xff" * 42))

    assert hal.Index24G_CCK_Base[0][0] == 0x49     # path A untouched
    assert hal.Index5G_BW40_Base[0][0] == 0x48
    assert set(hal.Index24G_CCK_Base[1]) == {0x33}
    assert set(hal.Index5G_BW40_Base[1]) == {0x33}
    assert hal.OFDM_24G_Diff[0][0] == 0            # path A's programmed 0x00 nibble
    assert hal.OFDM_24G_Diff[1][0] == -2           # path B's blank 0xFF nibble


def test_a_base_over_txgi_max_falls_back_group_by_group():
    """The merge is per field, not per map: one bad group takes the default, its neighbour does
    not. 0x80 is 128 > txgi_max 127 [SRC hal/hal_com_phycfg.c:30]."""
    hal = _hal(_mutated({A2G_CCK_G0: 0x80}))

    assert hal.Index24G_CCK_Base[0][0] == 0x33     # channels 1-2, group 0
    assert hal.Index24G_CCK_Base[0][2] == 0x48     # channel 3, group 1, still from EFUSE


def test_diff_nibbles_scale_and_sign_extend():
    """0x21/0xF1/0x87 across the three 2.4 GHz diff bytes, covering +1, +2, +7, -1 and the -8
    floor. [SRC hal/hal_com_phycfg.c:26-29, scaled into hal_data at :1318-1321]"""
    hal = _hal(_mutated({A2G_DIFF_1S: 0x21, A2G_DIFF_2S: 0xF1, A2G_DIFF_2T: 0x87}))

    assert hal.BW20_24G_Diff[0][0] == 4            # 0x21 MSB = 2
    assert hal.OFDM_24G_Diff[0][0] == 2            # 0x21 LSB = 1
    assert hal.BW40_24G_Diff[0][1] == -2           # 0xF1 MSB = -1
    assert hal.BW20_24G_Diff[0][1] == 2            # 0xF1 LSB = 1
    assert hal.OFDM_24G_Diff[0][1] == -16          # 0x87 MSB = -8
    assert hal.CCK_24G_Diff[0][1] == 14            # 0x87 LSB = 7


def test_the_ofdm_and_ht_bases_separate_when_the_diff_nibbles_differ():
    """The gap a single per channel ofdm reference cannot express: phy_get_pg_txpwr_idx adds
    OFDM_Diff to the legacy OFDM section and BW20_Diff to HT/VHT at 20 MHz
    [SRC hal/hal_com_phycfg.c:2487-2508]."""
    hal = _hal(_mutated({A2G_DIFF_1S: 0x21}))
    base = hal.Index24G_BW40_Base[0][0]

    assert base + hal.OFDM_24G_Diff[0][0] == base + 2
    assert base + hal.BW20_24G_Diff[0][0] == base + 4


def test_the_five_ghz_ofdm_2t_diff_sits_four_bytes_past_the_other_2t_diffs():
    """5G OFDM_Diff[2T] is the high nibble of T+18, outside the tx_idx loop
    [SRC hal/hal_com_phycfg.c:963-970]."""
    hal = _hal(_mutated({A5G_OFDM_2T: 0x30, A5G_DIFF_3S: 0x50, A5G_DIFF_4S: 0x50}))

    assert hal.OFDM_5G_Diff[0][1] == 6             # T+18 MSB = 3
    assert hal.BW20_5G_Diff[0][1] == 0             # still T+15's low nibble, not the 3S bytes


def test_one_tx_never_reads_the_two_tx_diff_bytes():
    efuse = _mutated({A2G_DIFF_1S: 0x21, A2G_DIFF_2S: 0xF1, A2G_DIFF_2T: 0x87})
    pwr_info_2g, _pwr_info_5g = hal_load_pg_txpwr_info(efuse.logical_map, 1)
    hal = _hal(efuse, max_tx_cnt=1)

    assert pwr_info_2g.OFDM_Diff[0][1] == PG_TXPWR_INVALID_DIFF
    assert hal.OFDM_24G_Diff[0][1] == 0
    assert hal.CCK_24G_Diff[0][1] == 0
    assert hal.BW20_24G_Diff[0][0] == 4            # the 1S byte is still read


def test_bw80_base_is_the_truncated_average_of_two_adjacent_group_bases():
    """[SRC hal/hal_com_phycfg.c:1337-1346]"""
    hal = _hal(_mutated({A5G_BASE + 2: 0x40, A5G_BASE + 3: 0x43}))   # 5G groups 2 and 3

    assert hal.Index5G_BW80_Base[0][1] == 0x41     # channel 58: (0x40 + 0x43) // 2


@pytest.mark.parametrize("channel, group, cck_group", [
    (1, 0, 0), (2, 0, 0), (5, 1, 1), (8, 2, 2), (11, 3, 3), (13, 4, 4), (14, 4, 5),
])
def test_rtw_get_ch_group_2g(channel, group, cck_group):
    assert rtw_get_ch_group(channel) == (BAND_ON_2_4G, group, cck_group)


@pytest.mark.parametrize("channel, group", [
    (36, 0), (42, 0), (48, 1), (58, 2), (64, 3), (144, 9), (155, 10), (165, 12), (177, 13),
])
def test_rtw_get_ch_group_5g(channel, group):
    assert rtw_get_ch_group(channel) == (BAND_ON_5G, group, -1)


@pytest.mark.parametrize("channel", [0, 15, 34, 35, 200])
def test_rtw_get_ch_group_rejects_a_channel_in_no_group(channel):
    assert rtw_get_ch_group(channel)[0] == BAND_MAX


def test_phy_get_ch_idx_indexes_the_base_rows():
    assert phy_get_ch_idx(1) == (0, True)
    assert phy_get_ch_idx(14) == (13, True)
    assert phy_get_ch_idx(36) == (0, False)
    assert phy_get_ch_idx(177) == (48, False)


def test_phy_get_ch_idx_raises_where_the_vendor_reads_an_uninitialised_index():
    """Channel 50 is a group boundary center_ch_5g_all omits, leaving the C's chnlIdx unwritten
    [SRC hal/hal_com_phycfg.c:2451,2466]."""
    with pytest.raises(ValueError):
        phy_get_ch_idx(50)


def test_tssi_mode_returns_none_a_screen_not_a_failure():
    """In TSSI mode the 0x10..0x63 bytes are TSSI offsets, not power indices, so hal_load_txpwr_info
    returns None and the caller leaves TX power to the TSSI path. rtw_hal_dm_init likewise never
    runs this in TSSI mode [SRC hal/hal_intf.c:200-201]."""
    assert hal_load_txpwr_info(_efuse(tpt_byte=0x40), 2) is None


def test_an_unburned_tpt_mode_still_computes_pwr_idx_bases():
    """0xC8 = 0xFF resolves to PWR_IDX (site 1), so the transform runs and an all 0xFF PG region
    takes the IC default 0x33 bases rather than aborting."""
    hal = hal_load_txpwr_info(_efuse(bytes([0xFF]) * 84, tpt_byte=0xFF), 2)
    assert hal is not None
    assert set(hal.Index24G_CCK_Base[0]) == {0x33}
