"""RTL8822CU power by rate: the compiled in phy_reg_pg array and the tables it fills.

Power by rate is never stored in EFUSE. Its only source on this chip is the header array
``array_mp_8822c_phy_reg_pg`` [SRC hal/phydm/rtl8822c/halhwimg8822c_bb.c:3639-3686], read by
``phy_load_tx_power_by_rate`` [SRC hal/hal_com_phycfg.c:4349-4393] into
``hal_data->TxPwrByRate[band][path][rate_idx]``, which then seeds the per section
``target_txpwr_2g`` / ``target_txpwr_5g``.

Rate currency: the C carries ``enum MGN_RATE`` and converts to the dense ``TxPwrByRate`` index at
``PHY_StoreTxPowerByRateNew`` [SRC hal/hal_com_phycfg.c:2264] through
``phy_get_rate_idx_of_txpwr_by_rate`` [SRC hal/hal_com_phycfg.c:2725-2736]. That index is the same
number as ``MRateToHwRate``'s ``DESC_RATE`` [SRC hal/hal_com_phycfg.c:2724 comment], which this port
checked entry by entry over both tables [SRC hal/hal_com_phycfg.c:2637-2722, hal/hal_com.c:446-531]:
all 84 agree. This module therefore merges the two steps and carries the ``DESC_RATE`` index
[SRC include/hal_com.h:33-120] everywhere the C would carry an ``MGN_RATE``. That index is also what
phydm's txagc buffer is subscripted by, so no second rate namespace is needed.
"""
from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    HAL_SPEC_RFPATH_NUM_2G,
    HAL_SPEC_RFPATH_NUM_5G,
    HAL_SPEC_TX_NSS_NUM,
    HAL_SPEC_TXGI_MAX,
)

BAND_ON_2_4G = 0                             # [SRC include/rtw_rf.h:81-82]
BAND_ON_5G = 1
BAND_CAP_2G_5G = (BAND_ON_2_4G, BAND_ON_5G)  # hal_spec->band_cap [SRC rtl8822c_halinit.c:51]

# RATE_SECTION [SRC include/ieee80211.h:976-996]
CCK, OFDM, HT_1SS, HT_2SS, HT_3SS, HT_4SS = 0, 1, 2, 3, 4, 5
VHT_1SS, VHT_2SS, VHT_3SS, VHT_4SS = 6, 7, 8, 9
RATE_SECTION_NUM = 10

# The tx_num column of rates_by_sections, in RF_TX_NUM units where RF_1TX is 0
# [SRC core/rtw_ieee80211.c:168-179, include/hal_com_phycfg.h:27-34]. Cluster C owns the rate lists.
_RATE_SECTION_TX_NUM = (0, 0, 0, 1, 2, 3, 0, 1, 2, 3)

# rate_sec_base [SRC hal/hal_com_phycfg.c:59-70], as DESC_RATE indices: MGN_11M, MGN_54M, MGN_MCS7,
# MGN_MCS15, MGN_MCS23, MGN_MCS31, MGN_VHT1SS_MCS7, MGN_VHT2SS_MCS7, VHT3SS_MCS7, VHT4SS_MCS7.
_RATE_SEC_BASE = (0x03, 0x0B, 0x13, 0x1B, 0x23, 0x2B, 0x33, 0x3D, 0x47, 0x51)

TX_PWR_BY_RATE_NUM_BAND = 2                  # [SRC include/hal_pg.h:967-969]
TX_PWR_BY_RATE_NUM_RF = 4
TX_PWR_BY_RATE_NUM_RATE = 84

PHY_REG_PG_VERSION = 2                       # [SRC halhwimg8822c_bb.c:3712-3713]
PHY_REG_PG_EXACT_VALUE = 1                   # [SRC hal/phydm/phydm.h:687-688]

# registrypriv.RegEnableTxPowerByRate: Makefile:100 CONFIG_TXPWR_BY_RATE_EN = y becomes
# -DCONFIG_TXPWR_BY_RATE_EN=1 (Makefile:1230-1231), carried by os_dep/linux/os_intfs.c:753,1390.
REG_ENABLE_TX_POWER_BY_RATE = 1

DIS_DPD_RATE_DIFF_DB = 3                     # [SRC hal/rtl8822c/rtl8822c_phy.c:717]

# rtl8822c_get_dis_dpd_by_rate_diff's ten cases, DESC_RATE index -> dis_dpd_rate bit
# [SRC hal/rtl8822c/rtl8822c_phy.c:715-749]: 6M, 9M, MCS0, MCS1, MCS8, MCS9,
# VHT1SS MCS0, VHT1SS MCS1, VHT2SS MCS0, VHT2SS MCS1.
_DIS_DPD_RATE_BIT = {0x04: 0, 0x05: 1, 0x0C: 2, 0x0D: 3, 0x14: 4,
                     0x15: 5, 0x2C: 6, 0x2D: 7, 0x36: 8, 0x37: 9}

# PHY_GetRateValuesOfTxPowerByRate: the four rates each phy_reg_pg register carries, byte 0 lowest,
# as DESC_RATE indices. The 0xExx block repeats the 0xCxx cases [SRC hal/hal_com_phycfg.c:1969-2123].
_REG_ADDR_RATES = {
    0xC20: range(0x00, 0x04), 0xE20: range(0x00, 0x04),   # 1M 2M 5.5M 11M          [SRC :1969-1980]
    0xC24: range(0x04, 0x08), 0xE24: range(0x04, 0x08),   # 6M 9M 12M 18M           [SRC :1982-1993]
    0xC28: range(0x08, 0x0C), 0xE28: range(0x08, 0x0C),   # 24M 36M 48M 54M         [SRC :1995-2006]
    0xC2C: range(0x0C, 0x10), 0xE2C: range(0x0C, 0x10),   # MCS0..3                 [SRC :2008-2019]
    0xC30: range(0x10, 0x14), 0xE30: range(0x10, 0x14),   # MCS4..7                 [SRC :2021-2032]
    0xC34: range(0x14, 0x18), 0xE34: range(0x14, 0x18),   # MCS8..11                [SRC :2034-2045]
    0xC38: range(0x18, 0x1C), 0xE38: range(0x18, 0x1C),   # MCS12..15               [SRC :2047-2058]
    0xC3C: range(0x2C, 0x30), 0xE3C: range(0x2C, 0x30),   # VHT1SS MCS0..3          [SRC :2060-2071]
    0xC40: range(0x30, 0x34), 0xE40: range(0x30, 0x34),   # VHT1SS MCS4..7          [SRC :2073-2084]
    0xC44: range(0x34, 0x38), 0xE44: range(0x34, 0x38),   # VHT1SS MCS8/9, 2SS 0/1  [SRC :2086-2097]
    0xC48: range(0x38, 0x3C), 0xE48: range(0x38, 0x3C),   # VHT2SS MCS2..5          [SRC :2099-2110]
    0xC4C: range(0x3C, 0x40), 0xE4C: range(0x3C, 0x40),   # VHT2SS MCS6..9          [SRC :2112-2123]
}

# array_mp_8822c_phy_reg_pg, 46 rows of {band, rf_path, tx_num, addr, bitmask, data}
# [SRC hal/phydm/rtl8822c/halhwimg8822c_bb.c:3639-3686].
ARRAY_MP_8822C_PHY_REG_PG = (
    0, 0, 0, 0x00000c20, 0xffffffff, 0x484c5054,
    0, 0, 0, 0x00000c24, 0xffffffff, 0x54585858,
    0, 0, 0, 0x00000c28, 0xffffffff, 0x44484c50,
    0, 0, 0, 0x00000c2c, 0xffffffff, 0x50545858,
    0, 0, 0, 0x00000c30, 0xffffffff, 0x4044484c,
    0, 0, 1, 0x00000c34, 0xffffffff, 0x50545858,
    0, 0, 1, 0x00000c38, 0xffffffff, 0x4044484c,
    0, 0, 0, 0x00000c3c, 0xffffffff, 0x50545858,
    0, 0, 0, 0x00000c40, 0xffffffff, 0x4044484c,
    0, 0, 0, 0x00000c44, 0xffffffff, 0x5858383c,
    0, 0, 1, 0x00000c48, 0xffffffff, 0x484c5054,
    0, 0, 1, 0x00000c4c, 0xffffffff, 0x383c4044,
    0, 1, 0, 0x00000e20, 0xffffffff, 0x484c5054,
    0, 1, 0, 0x00000e24, 0xffffffff, 0x54585858,
    0, 1, 0, 0x00000e28, 0xffffffff, 0x44484c50,
    0, 1, 0, 0x00000e2c, 0xffffffff, 0x50545858,
    0, 1, 0, 0x00000e30, 0xffffffff, 0x4044484c,
    0, 1, 1, 0x00000e34, 0xffffffff, 0x50545858,
    0, 1, 1, 0x00000e38, 0xffffffff, 0x4044484c,
    0, 1, 0, 0x00000e3c, 0xffffffff, 0x50545858,
    0, 1, 0, 0x00000e40, 0xffffffff, 0x4044484c,
    0, 1, 0, 0x00000e44, 0xffffffff, 0x5858383c,
    0, 1, 1, 0x00000e48, 0xffffffff, 0x484c5054,
    0, 1, 1, 0x00000e4c, 0xffffffff, 0x383c4044,
    1, 0, 0, 0x00000c24, 0xffffffff, 0x54585858,
    1, 0, 0, 0x00000c28, 0xffffffff, 0x44484c50,
    1, 0, 0, 0x00000c2c, 0xffffffff, 0x50545858,
    1, 0, 0, 0x00000c30, 0xffffffff, 0x4044484c,
    1, 0, 1, 0x00000c34, 0xffffffff, 0x50545858,
    1, 0, 1, 0x00000c38, 0xffffffff, 0x4044484c,
    1, 0, 0, 0x00000c3c, 0xffffffff, 0x50545858,
    1, 0, 0, 0x00000c40, 0xffffffff, 0x4044484c,
    1, 0, 0, 0x00000c44, 0xffffffff, 0x5858383c,
    1, 0, 1, 0x00000c48, 0xffffffff, 0x484c5054,
    1, 0, 1, 0x00000c4c, 0xffffffff, 0x383c4044,
    1, 1, 0, 0x00000e24, 0xffffffff, 0x54585858,
    1, 1, 0, 0x00000e28, 0xffffffff, 0x44484c50,
    1, 1, 0, 0x00000e2c, 0xffffffff, 0x50545858,
    1, 1, 0, 0x00000e30, 0xffffffff, 0x4044484c,
    1, 1, 1, 0x00000e34, 0xffffffff, 0x50545858,
    1, 1, 1, 0x00000e38, 0xffffffff, 0x4044484c,
    1, 1, 0, 0x00000e3c, 0xffffffff, 0x50545858,
    1, 1, 0, 0x00000e40, 0xffffffff, 0x4044484c,
    1, 1, 0, 0x00000e44, 0xffffffff, 0x5858383c,
    1, 1, 1, 0x00000e48, 0xffffffff, 0x484c5054,
    1, 1, 1, 0x00000e4c, 0xffffffff, 0x383c4044,
)
PHY_REG_PG_ROW_LEN = 6


def hal_spec_chk_rf_path(band: int, path: int) -> bool:
    """[SRC include/hal_data.h:272-276], with rfpath_num_2g/5g from rtl8822c_halinit.c:43-44."""
    if band == BAND_ON_2_4G:
        return path < HAL_SPEC_RFPATH_NUM_2G
    if band == BAND_ON_5G:
        return path < HAL_SPEC_RFPATH_NUM_5G
    return False


def hal_tx_nss(max_tx_cnt: int) -> int:
    """hal_data->tx_nss. registrypriv.tx_nss is 0, so NSS_VALID screens it out
    [SRC os_dep/linux/os_intfs.c:393,1321]. [SRC rtw_hal_trxnss_init hal/hal_intf.c:412-417]"""
    return min(HAL_SPEC_TX_NSS_NUM, max_tx_cnt)


def phy_is_tx_power_by_rate_needed(eeprom_regulatory: int) -> bool:
    """[SRC hal/hal_com_phycfg.c:4337-4347]"""
    return (REG_ENABLE_TX_POWER_BY_RATE == 1
            or (REG_ENABLE_TX_POWER_BY_RATE == 2 and eeprom_regulatory != 2))


def rtl8822c_get_dis_dpd_by_rate_diff(dis_dpd_rate: int, rate_idx: int) -> int:
    """3 dB on a rate whose dis_dpd_rate bit is set, else 0. dis_dpd_rate is
    phydm_get_dis_dpd_by_rate_8822c returning dm->dis_dpd_rate, which EfuseInfo.dis_dpd_rate holds.
    [SRC hal/rtl8822c/rtl8822c_phy.c:708-752, phydm_hal_api8822c.c:2186-2193]"""
    bit = _DIS_DPD_RATE_BIT.get(rate_idx)
    if bit is None:
        return 0
    return DIS_DPD_RATE_DIFF_DB if dis_dpd_rate & (1 << bit) else 0


def _get_val_from_hex(hex_value: int, i: int) -> int:
    """The phy_reg_pg_version 2 byte extractor. [SRC hal/hal_com_phycfg.c:1846-1849]"""
    return (hex_value >> (i * 8)) & 0xFF


def _s8(value: int) -> int:
    """The (s8) cast PwrByRateVal takes. [SRC hal/hal_com_phycfg.c:2249,1880]"""
    return value - 0x100 if value & 0x80 else value


def _phy_get_rate_values_of_txpwr_by_rate(reg_addr: int, value: int) -> list[tuple[int, int]]:
    """(rate_idx, s8 power) for one phy_reg_pg row, the merge of
    PHY_GetRateValuesOfTxPowerByRate and the rate index conversion its caller applies.
    [SRC hal/hal_com_phycfg.c:1852-2235, :2263-2266]"""
    rates = _REG_ADDR_RATES.get(reg_addr)
    if rates is None:
        raise ValueError(f"RTL8822CU phy_reg_pg register 0x{reg_addr:X} has no rate mapping")
    return [(rate, _s8(_get_val_from_hex(value, i))) for i, rate in enumerate(rates)]


def _phy_init_tx_power_by_rate() -> list[list[list[int]]]:
    """[SRC PHY_InitTxPowerByRate hal/hal_com_phycfg.c:2271-2283]"""
    return [[[HAL_SPEC_TXGI_MAX] * TX_PWR_BY_RATE_NUM_RATE
             for _ in range(TX_PWR_BY_RATE_NUM_RF)]
            for _ in range(TX_PWR_BY_RATE_NUM_BAND)]


def _phy_store_tx_power_by_rate(by_rate: list[list[list[int]]], band: int, rf_path: int,
                                reg_addr: int, data: int) -> None:
    """phy_store_tx_power_by_rate -> PHY_StoreTxPowerByRateNew; phy_reg_pg_version is 2, so the
    version 0 reject never fires. [SRC hal/hal_com_phycfg.c:2286-2304, :2238-2268]"""
    if band not in BAND_CAP_2G_5G:
        raise ValueError(f"RTL8822CU phy_reg_pg band {band}")
    if rf_path >= TX_PWR_BY_RATE_NUM_RF:
        raise ValueError(f"RTL8822CU phy_reg_pg RF path {rf_path}")
    for rate_idx, power in _phy_get_rate_values_of_txpwr_by_rate(reg_addr, data):
        by_rate[band][rf_path][rate_idx] = power


def _odm_read_and_config_mp_8822c_phy_reg_pg(by_rate: list[list[list[int]]]) -> None:
    """The six column row walk. odm_config_bb_phy_reg_pg_8822c drops tx_num and bitmask on the way
    to phy_store_tx_power_by_rate; no row carries the 0xfe/0xffe delay address.
    [SRC halhwimg8822c_bb.c:3691-3733, phydm_regconfig8822c.c:152-173]"""
    rows = ARRAY_MP_8822C_PHY_REG_PG
    for i in range(0, len(rows), PHY_REG_PG_ROW_LEN):
        band, rf_path, _tx_num, reg_addr, _bitmask, data = rows[i:i + PHY_REG_PG_ROW_LEN]
        _phy_store_tx_power_by_rate(by_rate, band, rf_path, reg_addr, data)


def _phy_is_txpwr_by_rate_undefined_of_band_path(by_rate: list[list[list[int]]], band: int,
                                                 path: int) -> bool:
    """[SRC hal/hal_com_phycfg.c:1721-1734]"""
    return all(value == HAL_SPEC_TXGI_MAX for value in by_rate[band][path])


def _phy_txpwr_by_rate_chk_for_path_dup(by_rate: list[list[list[int]]]) -> None:
    """Copy the lowest defined path over every undefined one. [SRC hal/hal_com_phycfg.c:1745-1800]"""
    for band in BAND_CAP_2G_5G:
        paths = [p for p in range(TX_PWR_BY_RATE_NUM_RF) if hal_spec_chk_rf_path(band, p)]
        undefined = {p: _phy_is_txpwr_by_rate_undefined_of_band_path(by_rate, band, p)
                     for p in paths}
        src_path = next((p for p in paths if not undefined[p]), None)
        if src_path is None:
            raise RuntimeError(f"RTL8822CU power by rate undefined on every path of band {band}")
        for path in paths:
            if undefined[path]:
                by_rate[band][path] = list(by_rate[band][src_path])


def _phy_store_target_tx_power(by_rate: list[list[list[int]]],
                               tx_nss: int) -> tuple[list[list[int]], list[list[int]]]:
    """target_txpwr_2g[path][rs] and target_txpwr_5g[path][rs - 1], each the by rate value of its
    section reference rate. regsty->target_tx_pwr_valid is _FALSE, so the module parameter override
    branch is dead [SRC hal/hal_com_phycfg.c:1609-1644]. 8822C is a jaguar part, so the VHT sections
    are kept. [SRC hal/hal_com_phycfg.c:1805-1839, phy_set_target_txpwr :1687-1719]"""
    target_2g = [[0] * RATE_SECTION_NUM for _ in range(TX_PWR_BY_RATE_NUM_RF)]
    target_5g = [[0] * (RATE_SECTION_NUM - 1) for _ in range(TX_PWR_BY_RATE_NUM_RF)]
    for band in BAND_CAP_2G_5G:
        for path in range(TX_PWR_BY_RATE_NUM_RF):
            if not hal_spec_chk_rf_path(band, path):
                break
            for rs in range(RATE_SECTION_NUM):
                if _RATE_SECTION_TX_NUM[rs] + 1 > tx_nss:
                    continue
                if band == BAND_ON_5G and rs == CCK:
                    continue
                base = by_rate[band][path][_RATE_SEC_BASE[rs]]
                if band == BAND_ON_2_4G:
                    target_2g[path][rs] = base
                else:
                    target_5g[path][rs - 1] = base
    return target_2g, target_5g


@dataclass(frozen=True)
class TxPwrByRate:
    """hal_data->TxPwrByRate[band][path][rate_idx] and the target_txpwr_2g/5g it seeds, both
    subscripted by DESC_RATE index and RATE_SECTION."""
    tx_pwr_by_rate: tuple[tuple[tuple[int, ...], ...], ...]
    target_txpwr_2g: tuple[tuple[int, ...], ...]
    target_txpwr_5g: tuple[tuple[int, ...], ...]
    by_rate_needed: bool

    def phy_get_target_txpwr(self, band: int, rfpath: int, rate_section: int) -> int:
        """[SRC hal/hal_com_phycfg.c:1651-1685]"""
        if rfpath >= TX_PWR_BY_RATE_NUM_RF or band not in BAND_CAP_2G_5G:
            return 0
        if rate_section >= RATE_SECTION_NUM or (band == BAND_ON_5G and rate_section == CCK):
            return 0
        if band == BAND_ON_2_4G:
            return self.target_txpwr_2g[rfpath][rate_section]
        return self.target_txpwr_5g[rfpath][rate_section - 1]

    def _phy_get_txpwr_by_rate(self, band: int, rfpath: int, rate_idx: int) -> int:
        """[SRC hal/hal_com_phycfg.c:2738-2762]"""
        if band not in BAND_CAP_2G_5G or rfpath >= TX_PWR_BY_RATE_NUM_RF:
            return 0
        if rate_idx >= TX_PWR_BY_RATE_NUM_RATE:
            return 0
        return self.tx_pwr_by_rate[band][rfpath][rate_idx]

    def phy_get_txpwr_by_rate(self, band: int, rfpath: int, rs: int, rate_idx: int) -> int:
        """The by rate value, or the flat section target when the by rate stage is off.
        [SRC hal/hal_com_phycfg.c:2767-2773]"""
        if self.by_rate_needed:
            return self._phy_get_txpwr_by_rate(band, rfpath, rate_idx)
        return self.phy_get_target_txpwr(band, rfpath, rs)


def phy_load_tx_power_by_rate(*, eeprom_regulatory: int, tx_nss: int) -> TxPwrByRate:
    """Fill TxPwrByRate from the header array, then run PHY_TxPowerByRateConfiguration.
    CONFIG_LOAD_PHY_PARA_FROM_FILE is on (Makefile:98) but wifit3 reads no PHY_REG_PG.txt, so only
    the CONFIG_EMBEDDED_FWIMG branch exists here. phy_reg_pg_value_type is PHY_REG_PG_EXACT_VALUE,
    which is the post handler's requirement. [SRC hal/hal_com_phycfg.c:4349-4393, :2311-2317]"""
    by_rate = _phy_init_tx_power_by_rate()
    _odm_read_and_config_mp_8822c_phy_reg_pg(by_rate)
    _phy_txpwr_by_rate_chk_for_path_dup(by_rate)
    target_2g, target_5g = _phy_store_target_tx_power(by_rate, tx_nss)
    return TxPwrByRate(
        tuple(tuple(tuple(rates) for rates in band) for band in by_rate),
        tuple(tuple(row) for row in target_2g),
        tuple(tuple(row) for row in target_5g),
        phy_is_tx_power_by_rate_needed(eeprom_regulatory),
    )
