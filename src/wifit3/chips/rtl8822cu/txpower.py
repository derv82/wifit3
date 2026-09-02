"""RTL8822C PG TX power base tables: the EFUSE 0x10..0x63 walk and its per channel transform.

Every base comes from an EFUSE byte, or from the IC default PG map where the EFUSE holds no
valid one; every diff comes from an EFUSE nibble. Nothing here is keyed by channel number.
[SRC hal_load_pg_txpwr_info hal/hal_com_phycfg.c:1035, hal_load_txpwr_info hal/hal_com_phycfg.c:1276]
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .constants import (
    HAL_SPEC_PG_TXGI_DIFF_FACTOR,
    HAL_SPEC_PG_TXPWR_SADDR,
    HAL_SPEC_RFPATH_NUM_2G,
    HAL_SPEC_RFPATH_NUM_5G,
    PG_TXPWR_1PATH_BYTE_NUM_2G,
    PG_TXPWR_1PATH_BYTE_NUM_5G,
    PG_TXPWR_INVALID_BASE,
    PG_TXPWR_INVALID_DIFF,
    RF_PATH_MAX,
    TXPWR_PG_WITH_PWR_IDX,
)
from .efuse import (
    EfuseInfo,
    is_pg_txpwr_base_invalid,
    is_pg_txpwr_diff_invalid,
    pg_txpwr_lsb_diff_to_s8bit,
    pg_txpwr_msb_diff_to_s8bit,
)

logger = logging.getLogger(__name__)

# [SRC include/hal_pg.h:1009-1010,1013]
MAX_CHNL_GROUP_24G = 6
MAX_CHNL_GROUP_5G = 14
MAX_TX_COUNT = 4

# CENTER_CH_5G_ALL_NUM is 20M + 40M + 80M = 28 + 14 + 7 [SRC include/rtw_rf.h:28,31-33,35]
CENTER_CH_2G_NUM = 14
CENTER_CH_5G_ALL_NUM = 49
CENTER_CH_5G_80M_NUM = 7

# enum band_type, with the 6 GHz entry compiled out
# [SRC include/rtw_rf.h:80-87, include/drv_conf.h:452-453]
BAND_ON_2_4G = 0
BAND_ON_5G = 1
BAND_MAX = 2

# [SRC hal/hal_com_phycfg.c:39-42]
PG_TXPWR_SRC_PG_DATA = 0
PG_TXPWR_SRC_IC_DEF = 1
PG_TXPWR_SRC_DEF = 2
PG_TXPWR_SRC_NUM = 3

PG_DATA_MAP_LEN = 184                            # [SRC hal/hal_com_phycfg.c:1047]

# [SRC core/rtw_rf.c:56-81]
center_ch_5g_all = (
    36, 38, 40, 42, 44, 46, 48, 52, 54, 56, 58, 60, 62, 64,
    100, 102, 104, 106, 108, 110, 112, 116, 118, 120, 122, 124, 126, 128,
    132, 134, 136, 138, 140, 142, 144, 149, 151, 153, 155, 157, 159, 161,
    165, 167, 169, 171, 173, 175, 177,
)
# [SRC core/rtw_rf.c:141-149]
center_ch_5g_80m = (42, 58, 106, 122, 138, 155, 171)


@dataclass(frozen=True)
class PgTxPwrMap:
    """map_t with one segment: an offset past map->len, or outside the segment, reads init_value.
    All three PG sources declare seg_num 1.
    [SRC include/osdep_service.h:889-909, map_read8 os_dep/osdep_service.c:3446-3468]"""
    map_len: int
    init_value: int
    seg_start: int
    seg: bytes

    def read8(self, offset: int) -> int:
        if offset + 1 > self.map_len:
            return self.init_value
        if self.seg_start <= offset < self.seg_start + len(self.seg):
            return self.seg[offset - self.seg_start]
        return self.init_value


# [SRC hal/hal_com_phycfg.c:401-409, selected for RTL8822C at :578-579]
RTL8822C_PG_TXPWR_DEF_INFO = PgTxPwrMap(0xB8, 0xFF, 0x10, bytes((
    0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x02, 0x00, 0x00, 0xFF, 0xFF,
    0xFF, 0xFF, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33,
    0x02, 0x00, 0xFF, 0xFF, 0x00, 0xFF, 0x00, 0x00, 0xFF, 0xFF, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33,
    0x33, 0x33, 0x33, 0x33, 0x33, 0x02, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0x33, 0x33, 0x33, 0x33,
    0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x33, 0x02, 0x00, 0xFF, 0xFF, 0x00, 0xFF,
    0x00, 0x00,
)))

# [SRC hal/hal_com_phycfg.c:267-280]
PG_TXPWR_DEF_INFO = PgTxPwrMap(0xB8, 0xFF, 0x10, bytes((
    0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x24, 0xEE, 0xEE, 0xEE, 0xEE,
    0xEE, 0xEE, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A,
    0x04, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE, 0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x2D,
    0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x24, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE, 0x2A, 0x2A, 0x2A, 0x2A,
    0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x04, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE,
    0xEE, 0xEE, 0xEE, 0xEE, 0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x24,
    0xEE, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A,
    0x2A, 0x2A, 0x2A, 0x2A, 0x04, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE, 0x2D, 0x2D,
    0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x2D, 0x24, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE,
    0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x2A, 0x04, 0xEE,
    0xEE, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE,
)))


# The hal_chk_band_cap(BAND_CAP_2G/5G) screens the C wraps each walk and check in are constant
# true here: band_cap is BAND_CAP_2G | BAND_CAP_5G [SRC hal/rtl8822c/rtl8822c_halinit.c:51,
# hal_chk_band_cap hal/hal_com.c:17037-17040], so both pwr_info structs always exist.
def _chk_rf_path_2g(path: int) -> bool:
    """[SRC HAL_SPEC_CHK_RF_PATH_2G include/hal_data.h:272]"""
    return HAL_SPEC_RFPATH_NUM_2G > path


def _chk_rf_path_5g(path: int) -> bool:
    """[SRC HAL_SPEC_CHK_RF_PATH_5G include/hal_data.h:273]"""
    return HAL_SPEC_RFPATH_NUM_5G > path


def _bases() -> list[list[int]]:
    return [[PG_TXPWR_INVALID_BASE] * MAX_CHNL_GROUP_24G for _ in range(RF_PATH_MAX)]


def _bases_5g() -> list[list[int]]:
    return [[PG_TXPWR_INVALID_BASE] * MAX_CHNL_GROUP_5G for _ in range(RF_PATH_MAX)]


def _diffs() -> list[list[int]]:
    return [[PG_TXPWR_INVALID_DIFF] * MAX_TX_COUNT for _ in range(RF_PATH_MAX)]


@dataclass
class TxPowerInfo24G:
    """[SRC hal/hal_com_phycfg.c:73-81]"""
    IndexCCK_Base: list[list[int]] = field(default_factory=_bases)
    IndexBW40_Base: list[list[int]] = field(default_factory=_bases)
    CCK_Diff: list[list[int]] = field(default_factory=_diffs)
    OFDM_Diff: list[list[int]] = field(default_factory=_diffs)
    BW20_Diff: list[list[int]] = field(default_factory=_diffs)
    BW40_Diff: list[list[int]] = field(default_factory=_diffs)


@dataclass
class TxPowerInfo5G:
    """[SRC hal/hal_com_phycfg.c:83-91]"""
    IndexBW40_Base: list[list[int]] = field(default_factory=_bases_5g)
    OFDM_Diff: list[list[int]] = field(default_factory=_diffs)
    BW20_Diff: list[list[int]] = field(default_factory=_diffs)
    BW40_Diff: list[list[int]] = field(default_factory=_diffs)
    BW80_Diff: list[list[int]] = field(default_factory=_diffs)
    BW160_Diff: list[list[int]] = field(default_factory=_diffs)


def _fill_base(table: list[list[int]], path: int, group: int, tmp_base: int) -> None:
    """The per field merge: a later PG source only fills a field still holding its sentinel.
    [SRC hal/hal_com_phycfg.c:773-776]"""
    if not is_pg_txpwr_base_invalid(tmp_base) and is_pg_txpwr_base_invalid(table[path][group]):
        table[path][group] = tmp_base


def _fill_diff(table: list[list[int]], path: int, tx_idx: int, tmp_diff: int) -> None:
    """[SRC hal/hal_com_phycfg.c:803-806]"""
    if not is_pg_txpwr_diff_invalid(tmp_diff) and is_pg_txpwr_diff_invalid(table[path][tx_idx]):
        table[path][tx_idx] = tmp_diff


def hal_init_pg_txpwr_info_2g() -> TxPowerInfo24G:
    """Invalid sentinels, then the three dummies no PG map carries: the 6th BW40 group and the
    1T CCK / 1S BW40 diffs. [SRC hal/hal_com_phycfg.c:670-705]"""
    pwr_info = TxPowerInfo24G()
    for path in range(RF_PATH_MAX):
        if not _chk_rf_path_2g(path):
            break
        pwr_info.IndexBW40_Base[path][MAX_CHNL_GROUP_24G - 1] = 0
        pwr_info.CCK_Diff[path][0] = 0
        pwr_info.BW40_Diff[path][0] = 0
    return pwr_info


def hal_init_pg_txpwr_info_5g() -> TxPowerInfo5G:
    """[SRC hal/hal_com_phycfg.c:707-738]"""
    pwr_info = TxPowerInfo5G()
    for path in range(RF_PATH_MAX):
        if not _chk_rf_path_5g(path):
            break
        pwr_info.BW40_Diff[path][0] = 0
    return pwr_info


def hal_chk_pg_txpwr_info_2g(pwr_info: TxPowerInfo24G, max_tx_cnt: int) -> bool:
    """[SRC hal/hal_com_phycfg.c:608-637]"""
    for path in range(RF_PATH_MAX):
        if not _chk_rf_path_2g(path):
            continue
        for group in range(MAX_CHNL_GROUP_24G):
            if (is_pg_txpwr_base_invalid(pwr_info.IndexCCK_Base[path][group])
                    or is_pg_txpwr_base_invalid(pwr_info.IndexBW40_Base[path][group])):
                return False
        for tx_idx in range(MAX_TX_COUNT):
            if tx_idx + 1 > max_tx_cnt:
                continue
            if any(is_pg_txpwr_diff_invalid(table[path][tx_idx])
                   for table in (pwr_info.CCK_Diff, pwr_info.OFDM_Diff,
                                 pwr_info.BW20_Diff, pwr_info.BW40_Diff)):
                return False
    return True


def hal_chk_pg_txpwr_info_5g(pwr_info: TxPowerInfo5G, max_tx_cnt: int) -> bool:
    """[SRC hal/hal_com_phycfg.c:639-668]"""
    for path in range(RF_PATH_MAX):
        if not _chk_rf_path_5g(path):
            continue
        for group in range(MAX_CHNL_GROUP_5G):
            if is_pg_txpwr_base_invalid(pwr_info.IndexBW40_Base[path][group]):
                return False
        for tx_idx in range(MAX_TX_COUNT):
            if tx_idx + 1 > max_tx_cnt:
                continue
            if any(is_pg_txpwr_diff_invalid(table[path][tx_idx])
                   for table in (pwr_info.OFDM_Diff, pwr_info.BW20_Diff, pwr_info.BW40_Diff,
                                 pwr_info.BW80_Diff, pwr_info.BW160_Diff)):
                return False
    return True


def hal_load_pg_txpwr_info_path_2g(pwr_info: TxPowerInfo24G, path: int, txpwr_map: PgTxPwrMap,
                                   pg_offset: int, max_tx_cnt: int) -> int:
    """18 bytes from pg_offset; returns the offset of the next path's 5 GHz block. The 3T/4T diff
    bytes are skipped but still consumed. [SRC hal/hal_com_phycfg.c:746-873]"""
    offset = pg_offset
    for group in range(MAX_CHNL_GROUP_24G):
        if _chk_rf_path_2g(path):
            _fill_base(pwr_info.IndexCCK_Base, path, group, txpwr_map.read8(offset))
        offset += 1
    for group in range(MAX_CHNL_GROUP_24G - 1):
        if _chk_rf_path_2g(path):
            _fill_base(pwr_info.IndexBW40_Base, path, group, txpwr_map.read8(offset))
        offset += 1
    for tx_idx in range(MAX_TX_COUNT):
        if tx_idx == 0:
            if _chk_rf_path_2g(path):
                val = txpwr_map.read8(offset)
                _fill_diff(pwr_info.BW20_Diff, path, tx_idx, pg_txpwr_msb_diff_to_s8bit(val))
                _fill_diff(pwr_info.OFDM_Diff, path, tx_idx, pg_txpwr_lsb_diff_to_s8bit(val))
            offset += 1
        else:
            if _chk_rf_path_2g(path) and tx_idx + 1 <= max_tx_cnt:
                val = txpwr_map.read8(offset)
                _fill_diff(pwr_info.BW40_Diff, path, tx_idx, pg_txpwr_msb_diff_to_s8bit(val))
                _fill_diff(pwr_info.BW20_Diff, path, tx_idx, pg_txpwr_lsb_diff_to_s8bit(val))
            offset += 1
            if _chk_rf_path_2g(path) and tx_idx + 1 <= max_tx_cnt:
                val = txpwr_map.read8(offset)
                _fill_diff(pwr_info.OFDM_Diff, path, tx_idx, pg_txpwr_msb_diff_to_s8bit(val))
                _fill_diff(pwr_info.CCK_Diff, path, tx_idx, pg_txpwr_lsb_diff_to_s8bit(val))
            offset += 1
    if offset != pg_offset + PG_TXPWR_1PATH_BYTE_NUM_2G:
        # Fixed trip counts make this unreachable; the C RTW_ERRs and continues [SRC :866-869].
        logger.error("RTL8822CU 2G PG walk consumed %d bytes, expected %d",
                     offset - pg_offset, PG_TXPWR_1PATH_BYTE_NUM_2G)
    return offset


def hal_load_pg_txpwr_info_path_5g(pwr_info: TxPowerInfo5G, path: int, txpwr_map: PgTxPwrMap,
                                   pg_offset: int, max_tx_cnt: int) -> int:
    """24 bytes from pg_offset. OFDM_Diff[2T] does not sit with the other 2T diffs: it is the high
    nibble of T+18, four bytes past them. [SRC hal/hal_com_phycfg.c:875-1033]"""
    offset = pg_offset
    for group in range(MAX_CHNL_GROUP_5G):
        if _chk_rf_path_5g(path):
            _fill_base(pwr_info.IndexBW40_Base, path, group, txpwr_map.read8(offset))
        offset += 1
    for tx_idx in range(MAX_TX_COUNT):
        if tx_idx == 0:
            if _chk_rf_path_5g(path):
                val = txpwr_map.read8(offset)
                _fill_diff(pwr_info.BW20_Diff, path, tx_idx, pg_txpwr_msb_diff_to_s8bit(val))
                _fill_diff(pwr_info.OFDM_Diff, path, tx_idx, pg_txpwr_lsb_diff_to_s8bit(val))
            offset += 1
        else:
            if _chk_rf_path_5g(path) and tx_idx + 1 <= max_tx_cnt:
                val = txpwr_map.read8(offset)
                _fill_diff(pwr_info.BW40_Diff, path, tx_idx, pg_txpwr_msb_diff_to_s8bit(val))
                _fill_diff(pwr_info.BW20_Diff, path, tx_idx, pg_txpwr_lsb_diff_to_s8bit(val))
            offset += 1

    # OFDM diff 2T ~ 3T [SRC hal/hal_com_phycfg.c:963-985]
    if _chk_rf_path_5g(path) and max_tx_cnt > 1:
        val = txpwr_map.read8(offset)
        _fill_diff(pwr_info.OFDM_Diff, path, 1, pg_txpwr_msb_diff_to_s8bit(val))
        if max_tx_cnt > 2:
            _fill_diff(pwr_info.OFDM_Diff, path, 2, pg_txpwr_lsb_diff_to_s8bit(val))
    offset += 1

    # OFDM diff 4T [SRC hal/hal_com_phycfg.c:987-999]
    if _chk_rf_path_5g(path) and max_tx_cnt > 3:
        val = txpwr_map.read8(offset)
        _fill_diff(pwr_info.OFDM_Diff, path, 3, pg_txpwr_lsb_diff_to_s8bit(val))
    offset += 1

    for tx_idx in range(MAX_TX_COUNT):
        if _chk_rf_path_5g(path) and tx_idx + 1 <= max_tx_cnt:
            val = txpwr_map.read8(offset)
            _fill_diff(pwr_info.BW80_Diff, path, tx_idx, pg_txpwr_msb_diff_to_s8bit(val))
            _fill_diff(pwr_info.BW160_Diff, path, tx_idx, pg_txpwr_lsb_diff_to_s8bit(val))
        offset += 1

    if offset != pg_offset + PG_TXPWR_1PATH_BYTE_NUM_5G:
        # Fixed trip counts make this unreachable; the C RTW_ERRs and continues [SRC :1024-1027].
        logger.error("RTL8822CU 5G PG walk consumed %d bytes, expected %d",
                     offset - pg_offset, PG_TXPWR_1PATH_BYTE_NUM_5G)
    return offset


def hal_load_pg_txpwr_info(pg_data: bytes,
                           max_tx_cnt: int) -> tuple[TxPowerInfo24G, TxPowerInfo5G]:
    """Walk the PG region from each source in turn, filling only fields still invalid, and stop as
    soon as both bands check out. The AutoLoadFail argument the C takes is never read, and
    hal_load_txpwr_info passes a literal _FALSE, so a blank map does not by itself select a
    default. [SRC hal/hal_com_phycfg.c:1035-1102, :1294]"""
    pwr_info_2g = hal_init_pg_txpwr_info_2g()
    pwr_info_5g = hal_init_pg_txpwr_info_5g()
    for txpwr_src in range(PG_TXPWR_SRC_NUM):
        if txpwr_src == PG_TXPWR_SRC_PG_DATA:
            txpwr_map = PgTxPwrMap(PG_DATA_MAP_LEN, 0xFF, 0x00, pg_data[:PG_DATA_MAP_LEN])
        elif txpwr_src == PG_TXPWR_SRC_IC_DEF:
            txpwr_map = RTL8822C_PG_TXPWR_DEF_INFO
        else:
            txpwr_map = PG_TXPWR_DEF_INFO
        pg_offset = HAL_SPEC_PG_TXPWR_SADDR
        for path in range(RF_PATH_MAX):
            if not _chk_rf_path_2g(path) and not _chk_rf_path_5g(path):
                break
            pg_offset = hal_load_pg_txpwr_info_path_2g(pwr_info_2g, path, txpwr_map, pg_offset,
                                                       max_tx_cnt)
            pg_offset = hal_load_pg_txpwr_info_path_5g(pwr_info_5g, path, txpwr_map, pg_offset,
                                                       max_tx_cnt)
        if (hal_chk_pg_txpwr_info_2g(pwr_info_2g, max_tx_cnt)
                and hal_chk_pg_txpwr_info_5g(pwr_info_5g, max_tx_cnt)):
            return pwr_info_2g, pwr_info_5g
    # Unreachable on 8822C: the IC default fills every base and diff the checks read. If it ever
    # is, the C warns and goes on with the 255 sentinels [SRC hal/hal_com_phycfg.c:1089-1091].
    logger.error("RTL8822CU PG TX power still invalid after PG data, IC default and generic default")
    return pwr_info_2g, pwr_info_5g


def rtw_get_ch_group(ch: int) -> tuple[int, int, int]:
    """(band, group, cck_group); group is -1 unless band is 2.4 or 5 GHz, cck_group -1 off 2.4 GHz.
    Channel 14 takes CCK group 5, which has no BW40 counterpart.
    [SRC hal/hal_com_phycfg.c:1201-1274]"""
    gp, cck_gp = -1, -1
    if ch <= 14:
        band = BAND_ON_2_4G
        if 1 <= ch <= 2:
            gp = 0
        elif 3 <= ch <= 5:
            gp = 1
        elif 6 <= ch <= 8:
            gp = 2
        elif 9 <= ch <= 11:
            gp = 3
        elif 12 <= ch <= 14:
            gp = 4
        else:
            band = BAND_MAX
        cck_gp = 5 if ch == 14 else gp
    else:
        band = BAND_ON_5G
        if 36 <= ch <= 42:
            gp = 0
        elif 44 <= ch <= 48:
            gp = 1
        elif 50 <= ch <= 58:
            gp = 2
        elif 60 <= ch <= 64:
            gp = 3
        elif 100 <= ch <= 106:
            gp = 4
        elif 108 <= ch <= 114:
            gp = 5
        elif 116 <= ch <= 122:
            gp = 6
        elif 124 <= ch <= 130:
            gp = 7
        elif 132 <= ch <= 138:
            gp = 8
        elif 140 <= ch <= 144:
            gp = 9
        elif 149 <= ch <= 155:
            gp = 10
        elif 157 <= ch <= 161:
            gp = 11
        elif 165 <= ch <= 171:
            gp = 12
        elif 173 <= ch <= 177:
            gp = 13
        else:
            band = BAND_MAX
    if band == BAND_MAX:
        return BAND_MAX, -1, -1
    return band, gp, (cck_gp if band == BAND_ON_2_4G else -1)


def phy_get_ch_idx(ch: int) -> tuple[int, bool]:
    """(ch_idx, in_24g), the index into the Index24G_* / Index5G_BW40_Base rows. This raises, not
    logs: it is a caller argument, not EFUSE content. All nine driver SUPPORTED_CHANNELS are in
    center_ch_5g_all, and it is off the bring up path, so a miss (5 GHz 50/114/163) is a caller
    bug. A 5 GHz channel absent from the list leaves the C's chnlIdx unwritten, and
    phy_get_pg_txpwr_idx then indexes with it; if this ever must continue, the C's own illegal
    channel branch sets chnlIdx = 0 [SRC hal/hal_com_phycfg.c:2453-2454].
    [SRC hal/hal_com_phycfg.c:2371-2392, chnlIdx hal/hal_com_phycfg.c:2451,2466]"""
    if 0 < ch <= 14:
        return ch - 1, True
    for i in range(CENTER_CH_5G_ALL_NUM):
        if center_ch_5g_all[i] == ch:
            return i, False
    raise ValueError(f"RTL8822CU channel {ch} is not in center_ch_5g_all")


def _zeros(width: int) -> list[list[int]]:
    return [[0] * width for _ in range(RF_PATH_MAX)]


@dataclass
class HalTxPwrInfo:
    """The hal_data half: bases indexed [path][ch_idx], diffs [path][tx_idx] and already scaled by
    pg_txgi_diff_factor. [SRC include/hal_data.h:488-502]"""
    Index24G_CCK_Base: list[list[int]] = field(default_factory=lambda: _zeros(CENTER_CH_2G_NUM))
    Index24G_BW40_Base: list[list[int]] = field(default_factory=lambda: _zeros(CENTER_CH_2G_NUM))
    CCK_24G_Diff: list[list[int]] = field(default_factory=lambda: _zeros(MAX_TX_COUNT))
    OFDM_24G_Diff: list[list[int]] = field(default_factory=lambda: _zeros(MAX_TX_COUNT))
    BW20_24G_Diff: list[list[int]] = field(default_factory=lambda: _zeros(MAX_TX_COUNT))
    BW40_24G_Diff: list[list[int]] = field(default_factory=lambda: _zeros(MAX_TX_COUNT))
    Index5G_BW40_Base: list[list[int]] = field(default_factory=lambda: _zeros(CENTER_CH_5G_ALL_NUM))
    Index5G_BW80_Base: list[list[int]] = field(default_factory=lambda: _zeros(CENTER_CH_5G_80M_NUM))
    OFDM_5G_Diff: list[list[int]] = field(default_factory=lambda: _zeros(MAX_TX_COUNT))
    BW20_5G_Diff: list[list[int]] = field(default_factory=lambda: _zeros(MAX_TX_COUNT))
    BW40_5G_Diff: list[list[int]] = field(default_factory=lambda: _zeros(MAX_TX_COUNT))
    BW80_5G_Diff: list[list[int]] = field(default_factory=lambda: _zeros(MAX_TX_COUNT))


def hal_load_txpwr_info(efuse: EfuseInfo, max_tx_cnt: int) -> HalTxPwrInfo | None:
    """The PG group bases spread over every channel index, and the diffs scaled to txgi units.
    Returns None in TSSI mode: the same 0x10..0x63 bytes are TSSI offsets there, not power indices,
    and reading them as bases is meaningless (any byte > 127 would silently become IC_DEF 0x33). A
    None is a screen, not a failure; rtw_hal_dm_init likewise never calls this in TSSI mode
    [SRC hal/hal_intf.c:200-201]. The caller must handle None by leaving TX power to the TSSI path.
    BW160_Diff is parsed but has no hal_data field, so it stops here.
    [SRC hal/hal_com_phycfg.c:1276-1367]"""
    if efuse.txpwr_pg_mode != TXPWR_PG_WITH_PWR_IDX:
        return None
    pwr_info_2g, pwr_info_5g = hal_load_pg_txpwr_info(efuse.logical_map, max_tx_cnt)
    hal = HalTxPwrInfo()

    for rfpath in range(RF_PATH_MAX):
        if _chk_rf_path_2g(rfpath):
            for ch_idx in range(CENTER_CH_2G_NUM):
                band, group, cck_group = rtw_get_ch_group(ch_idx + 1)
                if band != BAND_ON_2_4G:
                    continue
                hal.Index24G_CCK_Base[rfpath][ch_idx] = \
                    pwr_info_2g.IndexCCK_Base[rfpath][cck_group]
                hal.Index24G_BW40_Base[rfpath][ch_idx] = \
                    pwr_info_2g.IndexBW40_Base[rfpath][group]
            for tx_idx in range(MAX_TX_COUNT):
                if tx_idx + 1 > max_tx_cnt:
                    break
                factor = HAL_SPEC_PG_TXGI_DIFF_FACTOR
                hal.CCK_24G_Diff[rfpath][tx_idx] = pwr_info_2g.CCK_Diff[rfpath][tx_idx] * factor
                hal.OFDM_24G_Diff[rfpath][tx_idx] = pwr_info_2g.OFDM_Diff[rfpath][tx_idx] * factor
                hal.BW20_24G_Diff[rfpath][tx_idx] = pwr_info_2g.BW20_Diff[rfpath][tx_idx] * factor
                hal.BW40_24G_Diff[rfpath][tx_idx] = pwr_info_2g.BW40_Diff[rfpath][tx_idx] * factor

        if _chk_rf_path_5g(rfpath):
            for ch_idx in range(CENTER_CH_5G_ALL_NUM):
                band, group, _cck = rtw_get_ch_group(center_ch_5g_all[ch_idx])
                if band != BAND_ON_5G:
                    continue
                hal.Index5G_BW40_Base[rfpath][ch_idx] = \
                    pwr_info_5g.IndexBW40_Base[rfpath][group]
            for ch_idx in range(CENTER_CH_5G_80M_NUM):
                band, group, _cck = rtw_get_ch_group(center_ch_5g_80m[ch_idx])
                if band != BAND_ON_5G:
                    continue
                upper = pwr_info_5g.IndexBW40_Base[rfpath][group]
                lower = pwr_info_5g.IndexBW40_Base[rfpath][group + 1]
                hal.Index5G_BW80_Base[rfpath][ch_idx] = (upper + lower) // 2
            for tx_idx in range(MAX_TX_COUNT):
                if tx_idx + 1 > max_tx_cnt:
                    break
                factor = HAL_SPEC_PG_TXGI_DIFF_FACTOR
                hal.OFDM_5G_Diff[rfpath][tx_idx] = pwr_info_5g.OFDM_Diff[rfpath][tx_idx] * factor
                hal.BW20_5G_Diff[rfpath][tx_idx] = pwr_info_5g.BW20_Diff[rfpath][tx_idx] * factor
                hal.BW40_5G_Diff[rfpath][tx_idx] = pwr_info_5g.BW40_Diff[rfpath][tx_idx] * factor
                hal.BW80_5G_Diff[rfpath][tx_idx] = pwr_info_5g.BW80_Diff[rfpath][tx_idx] * factor
    return hal
