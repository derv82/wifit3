"""RTL8822C TX power index: the PG base, the per rate target diff and the amends on top of it.

``power_idx = phy_get_pg_txpwr_idx + (rate_target - rs_target) + rate_amends``, clamped to
0..``txgi_max`` [SRC hal_com_get_txpwr_idx hal/hal_com_phycfg.c:6265-6353]. This is the
``TXPWR_PG_WITH_PWR_IDX`` arm, selected at runtime by EFUSE 0xC8's high nibble
[SRC hal/rtl8822c/rtl8822c_ops.c:251-263]; the ``TXPWR_PG_WITH_TSSI_OFFSET`` arm
[SRC hal/hal_com_phycfg.c:6312-6333] is not ported, and ``txpwr_idx_state`` returns None there.

THE REGULATORY LIMIT IS ABSENT ON PURPOSE, do not "fix" it by porting the limit table.
``Makefile:102`` sets ``CONFIG_TXPWR_LIMIT_EN = n``, which ``Makefile:1241-1242`` turns into
``-DCONFIG_TXPWR_LIMIT_EN=0`` and ``os_dep/linux/os_intfs.c:758,1388`` carries into
``registrypriv.RegEnableTxPowerLimit = 0``. Every limit term in ``phy_get_txpwr_target`` then
short circuits to ``hal_spec->txgi_max``, the vendor's "unspecified" sentinel:
``phy_get_txpwr_regd_lmt`` [SRC :6002-6003], ``phy_get_txpwr_lmt_sub_chs`` [SRC :3165-3170].
``phy_is_tx_power_limit_needed`` is likewise _FALSE [SRC :4323-4335], so
``phy_load_tx_power_ext_info`` never loads the table [SRC :4442-4460] and the 2976 row
``array_mp_8822c_txpwr_lmt`` [SRC hal/phydm/halrf/rtl8822c/halhwimg8822c_rf.c:37115] is compiled
and never read. Adding it would LOWER TX power below what the vendor driver transmits: a
deliberate deviation for the maintainer to decide, not a porting omission.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .constants import (
    BB_PATH_A,
    BB_PATH_AB,
    HAL_SPEC_TXGI_MAX,
    HAL_SPEC_TXGI_PDBM,
    RF_PATH_MAX,
)
from .efuse import EfuseInfo, RfPath
from .txpower import (
    BAND_ON_2_4G,
    BAND_ON_5G,
    CENTER_CH_5G_80M_NUM,
    HalTxPwrInfo,
    center_ch_5g_80m,
    hal_load_txpwr_info,
    phy_get_ch_idx,
)
from .txpwr_tables import (
    CCK,
    OFDM,
    RATE_SECTION_NUM,
    TxPwrByRate,
    _RATE_SECTION_TX_NUM,
    hal_tx_nss,
    phy_load_tx_power_by_rate,
    rtl8822c_get_dis_dpd_by_rate_diff,
)

logger = logging.getLogger(__name__)

# enum channel_width [SRC include/cmn_info/rtw_sta_info.h:68-77]
CHANNEL_WIDTH_20 = 0
CHANNEL_WIDTH_40 = 1
CHANNEL_WIDTH_80 = 2
CHANNEL_WIDTH_160 = 3
CHANNEL_WIDTH_80_80 = 4
CHANNEL_WIDTH_5 = 5
CHANNEL_WIDTH_10 = 6

# enum RF_TX_NUM [SRC include/hal_com_phycfg.h:27-34]
RF_1TX, RF_2TX, RF_3TX, RF_4TX = 0, 1, 2, 3

DESC_RATE11M = 0x03                          # [SRC include/hal_com.h:36]

# registrypriv.tx_npath. CONFIG_RTW_TX_NPATH_EN is defined (Makefile:22) so the branch compiles,
# and rtw_tx_npath_enable defaults to 0 [SRC os_dep/linux/os_intfs.c:811,1288]. CONFIG_RTW_PATH_DIV
# is never defined, so the BB_PATH_AUTO arms next to it are compiled out.
REG_TX_NPATH = 0

# The rate list column of rates_by_sections [SRC core/rtw_ieee80211.c:101-118,168-179], as
# DESC_RATE spans: each MGN_RATE list is contiguous in DESC_RATE [SRC include/hal_com.h:33-120].
# The tx_num column is txpwr_tables._RATE_SECTION_TX_NUM, kept single sourced.
RATE_SECTION_RATES = (
    range(0x00, 0x04),   # CCK      1M .. 11M
    range(0x04, 0x0C),   # OFDM     6M .. 54M
    range(0x0C, 0x14),   # HT_1SS   MCS0 .. MCS7
    range(0x14, 0x1C),   # HT_2SS   MCS8 .. MCS15
    range(0x1C, 0x24),   # HT_3SS   MCS16 .. MCS23
    range(0x24, 0x2C),   # HT_4SS   MCS24 .. MCS31
    range(0x2C, 0x36),   # VHT_1SS  MCS0 .. MCS9
    range(0x36, 0x40),   # VHT_2SS  MCS0 .. MCS9
    range(0x40, 0x4A),   # VHT_3SS  MCS0 .. MCS9
    range(0x4A, 0x54),   # VHT_4SS  MCS0 .. MCS9
)


def _s8(value: int) -> int:
    return value - 0x100 if value & 0x80 else value


def is_cck_rate(rate_idx: int) -> bool:
    """IS_CCK_RATE in the DESC_RATE currency. [SRC IS_CCK_HRATE include/hal_com.h:123]"""
    return rate_idx <= DESC_RATE11M


def rate_idx_to_rs(rate_idx: int) -> int:
    """mgn_rate_to_rs over DESC_RATE spans. [SRC core/rtw_ieee80211.c:120-146]"""
    for rs, rates in enumerate(RATE_SECTION_RATES):
        if rate_idx in rates:
            return rs
    raise ValueError(f"RTL8822CU DESC_RATE 0x{rate_idx:02X} is in no rate section")


def rate_section_to_tx_num(rs: int) -> int:
    """[SRC include/ieee80211.h:1031]"""
    return _RATE_SECTION_TX_NUM[rs]


def tx_path_nss_set_default(txpath: int) -> tuple[list[int], list[int]]:
    """(txpath_nss, txpath_num_nss); path with the lower index preferred.
    [SRC core/rtw_rf.c:2193-2210]"""
    txpath_nss = [0] * 4
    txpath_num_nss = [0] * 4
    for i in range(4, 0, -1):
        cnt = 0
        txpath_nss[i - 1] = 0
        for j in range(RF_PATH_MAX):
            if txpath & (1 << j):
                txpath_nss[i - 1] |= 1 << j
                cnt += 1
                if cnt == i:
                    break
        txpath_num_nss[i - 1] = i
    return txpath_nss, txpath_num_nss


def rf_type_to_default_trx_bmp(path_num: int) -> int:
    """The contiguous low bit path mask an rf_type falls back to.
    [SRC core/rtw_rf.c:2077-2095]"""
    return (1 << path_num) - 1


def hal_txpath_num_nss(trx_path_bmp: int, max_tx_cnt: int, *,
                       rf_2t2r: bool = True) -> tuple[int, ...]:
    """hal_data->txpath_num_nss, the TX path count per NSS that phy_get_current_tx_num reads.

    rf_2t2r is read only when the whole bitmap is empty, which hal_rfpath_init never produces; the
    default matches hal_spec's 0x33. [SRC rtw_hal_runtime_trx_path_decision hal/hal_dm.c:1466-1538,
    GET_HAL_TX_PATH_BMP include/hal_data.h:883]"""
    txpath = (trx_path_bmp & 0xF0) >> 4
    rxpath = trx_path_bmp & 0x0F
    if not txpath and not rxpath:
        # BOTH nibbles empty is not the abort: rtw_hal_get_trx_path substitutes the rf_type default
        # [SRC hal/hal_com.c:17571-17572]. 8822C's rf_type is only 2T2R or 1T1R
        # [SRC hal/rtl8822c/rtl8822c_ops.c:197].
        txpath = rxpath = rf_type_to_default_trx_bmp(2 if rf_2t2r else 1)
    elif not txpath or not rxpath:
        # Exactly one empty aborts before tx_path_nss_set_default runs [SRC hal/hal_dm.c:1475-1484],
        # leaving the zvmalloc'd zeros [SRC hal/hal_intf.c:147-148]; phy_get_current_tx_num's
        # tx_num == 0 fallback then puts every rate on RF_1TX [SRC hal/hal_com.c:17638].
        logger.error("RTL8822CU trx_path_bmp 0x%02X has no TX path or no RX path", trx_path_bmp)
        return (0, 0, 0, 0)

    txpath_nss, txpath_num_nss = tx_path_nss_set_default(txpath)
    if txpath == BB_PATH_AB:
        if max_tx_cnt == 2:
            # CONFIG_RTW_PATH_DIV is never defined, so the BB_PATH_AUTO arm at :1502-1506 is out.
            txpath_1ss = BB_PATH_AB if REG_TX_NPATH == 1 else BB_PATH_A   # [SRC :1496-1508]
        elif max_tx_cnt == 1:
            txpath_1ss = BB_PATH_A                   # tx_npath is not consulted [SRC :1509-1516]
        else:
            logger.error("RTL8822CU invalid max_tx_cnt %u for a 2 TX path bitmap",  # [SRC :1517-1521]
                         max_tx_cnt)
            return tuple(txpath_num_nss)
    else:
        txpath_1ss = txpath                                               # [SRC :1523-1524]

    if txpath_nss[0] != txpath_1ss:
        txpath_nss[0] = txpath_1ss
        txpath_num_nss[0] = bin(txpath_1ss).count("1")
    return tuple(txpath_num_nss)


def phy_get_current_tx_num(txpath_num_nss: tuple[int, ...], rate_idx: int) -> int:
    """RF_1TX..RF_4TX for the rate's stream count. IS_1T_RATE..IS_4T_RATE
    [SRC include/ieee80211.h:969-972] pick the same stream count as the rate's section tx_num.
    [SRC hal/hal_com.c:17622-17639]"""
    nss = rate_section_to_tx_num(rate_idx_to_rs(rate_idx)) + 1
    tx_num = txpath_num_nss[nss - 1]
    return RF_1TX if tx_num == 0 else tx_num - 1


def _apply_ntx_diff(tx_power: int, diff: list[int], ntx_idx: int) -> int:
    """The RF_1TX diff plus one per extra TX, the shape every branch of phy_get_pg_txpwr_idx
    repeats. [SRC hal/hal_com_phycfg.c:2475-2481]"""
    tx_power += diff[RF_1TX]
    for n in (RF_2TX, RF_3TX, RF_4TX):
        if ntx_idx >= n:
            tx_power += diff[n]
    return tx_power


def phy_get_pg_txpwr_idx(hal: HalTxPwrInfo, rfpath: int, rs: int, ntx_idx: int, bw: int,
                         band: int, channel: int, opch: int, *, cch_20: int) -> int:
    """The PG base index of one rate section, as the C's u8 return.

    HAL_IsLegalChannel's false arm [SRC hal/hal_com_phycfg.c:2453-2455] is unreachable here:
    registrypriv.wireless_mode defaults to WIRELESS_MODE_MAX [SRC os_dep/linux/os_intfs.c:45], so
    it is false only for channel 0 [SRC hal/hal_com.c:428-441], which phy_get_ch_idx rejects.
    cch_20 is hal_data->cch_20, the operating channel at BW20 [SRC hal/hal_intf.c:1132,1166].
    [SRC hal/hal_com_phycfg.c:2444-2608]"""
    if rs in (CCK, OFDM):                                     # [SRC :2457-2463]
        if opch:
            # rtw_get_scch_by_cch_opch (core/rtw_rf.c:398-408) is unported; the C's while loop is a
            # no op at BW20, which is the only width the monitor path tunes.
            if bw > CHANNEL_WIDTH_20:
                raise ValueError("RTL8822CU opch subchannel walk is unported; monitor runs BW20")
        else:
            channel = cch_20
    chnl_idx, _in_24g = phy_get_ch_idx(channel)

    tx_power = 0
    if band == BAND_ON_2_4G:
        if rs == CCK:
            tx_power = hal.Index24G_CCK_Base[rfpath][chnl_idx]
            tx_power = _apply_ntx_diff(tx_power, hal.CCK_24G_Diff[rfpath], ntx_idx)
            return tx_power & 0xFF

        tx_power = hal.Index24G_BW40_Base[rfpath][chnl_idx]
        if rs == OFDM:
            tx_power = _apply_ntx_diff(tx_power, hal.OFDM_24G_Diff[rfpath], ntx_idx)
        elif bw in (CHANNEL_WIDTH_5, CHANNEL_WIDTH_10, CHANNEL_WIDTH_20):
            tx_power = _apply_ntx_diff(tx_power, hal.BW20_24G_Diff[rfpath],
                                       rate_section_to_tx_num(rs))
        elif bw in (CHANNEL_WIDTH_40, CHANNEL_WIDTH_80):
            # BW80 deliberately reuses the BW40 index on 2.4 GHz [SRC :2512-2515]
            tx_power = _apply_ntx_diff(tx_power, hal.BW40_24G_Diff[rfpath],
                                       rate_section_to_tx_num(rs))
        return tx_power & 0xFF

    if band == BAND_ON_5G:
        if rs == CCK:
            logger.warning("RTL8822CU CCK on 5 GHz has no PG base [SRC RTW_WARN :2528-2531]")
            return 0

        tx_power = hal.Index5G_BW40_Base[rfpath][chnl_idx]
        if rs == OFDM:
            tx_power = _apply_ntx_diff(tx_power, hal.OFDM_5G_Diff[rfpath], ntx_idx)
        elif bw in (CHANNEL_WIDTH_5, CHANNEL_WIDTH_10, CHANNEL_WIDTH_20):
            tx_power = _apply_ntx_diff(tx_power, hal.BW20_5G_Diff[rfpath],
                                       rate_section_to_tx_num(rs))
        elif bw == CHANNEL_WIDTH_40:
            tx_power = _apply_ntx_diff(tx_power, hal.BW40_5G_Diff[rfpath],
                                       rate_section_to_tx_num(rs))
        elif bw == CHANNEL_WIDTH_80:
            bw80_idx = next((i for i in range(CENTER_CH_5G_80M_NUM)
                             if center_ch_5g_80m[i] == channel), None)
            if bw80_idx is None:
                logger.warning("RTL8822CU channel %u is no 80 MHz center channel [SRC :2580-2587]",
                               channel)
                return 0
            tx_power = hal.Index5G_BW80_Base[rfpath][bw80_idx]
            tx_power = _apply_ntx_diff(tx_power, hal.BW80_5G_Diff[rfpath],
                                       rate_section_to_tx_num(rs))
        else:
            logger.warning("RTL8822CU 5 GHz BW %u has no PG base [SRC rtw_warn_on :2601-2602]", bw)
        return tx_power & 0xFF

    return tx_power & 0xFF


def phy_get_txpwr_target(by_rate: TxPwrByRate, rfpath: int, rs: int, rate_idx: int, ntx_idx: int,
                         bw: int, band: int, cch: int, opch: int) -> int:
    """The absolute per rate target in TX gain index units.

    reg_max is 0 from hal_com_get_txpwr_idx [SRC :6277] and tic is debug only, so both are dropped.
    ntx_idx, bw, cch and opch reach only the four terms this build compiles out, and are kept so
    the shape still matches the C: phy_get_txpwr_user_target [SRC :6130] and phy_get_txpwr_user_lmt
    [SRC :6158] need CONFIG_IOCTL_CFG80211 and there is no cfg80211 here, and both limit terms are
    txgi_max (see the module docstring). btc_diff stays 0: wifit3 has no BT coex, and on the
    recorded adapter 0xC1[7:5] != 1 leaves EEPROMBluetoothCoexist false anyway
    [SRC hal/rtl8822c/rtl8822c_ops.c:323-330]. extra is 0 because no chip in the vendor tree hooks
    get_txpwr_target_extra_bias [SRC hal/hal_intf.c:2060-2063]. tpc is 0: rfctl->tpc_mode is
    TPC_MODE_DISABLE until a proc write [SRC :6099-6102]. [SRC hal/hal_com_phycfg.c:6115-6194]"""
    btc_diff = 0
    extra = 0
    tpc = 0
    rlmt = lmt = ulmt = HAL_SPEC_TXGI_MAX

    by_rate_val = 0
    if band == BAND_ON_2_4G or not is_cck_rate(rate_idx):     # [SRC :6126-6127]
        by_rate_val = by_rate.phy_get_txpwr_by_rate(band, rfpath, rs, rate_idx)
        if by_rate_val == HAL_SPEC_TXGI_MAX:
            by_rate_val = 0                          # [SRC :6142-6143]

    # utgt is also txgi_max, so the C's user target branch [SRC :6165-6166] never fires.
    target = by_rate_val + btc_diff + extra          # [SRC :6168]
    target = min(target, rlmt, lmt, ulmt)            # [SRC :6170-6175]
    return target + tpc                              # [SRC :6177]


def phy_get_txpwr_amends(dis_dpd_rate: int, rfpath: int, rs: int, rate_idx: int, ntx_idx: int,
                         bw: int, band: int, cch: int) -> int:
    """tpt_diff + dpd_diff, in TX gain index units.

    tpt_diff stays 0: PHY_GetTxPowerTrackingOffset is called only for the eleven older parts listed
    at [SRC :6217-6222], and 8822C is not among them. dpd_diff is the DPD amend, negated and scaled
    by txgi_pdbm [SRC :6226-6227]. rfpath, rs, ntx_idx, bw and cch reach only tpt_diff and tic.
    [SRC hal/hal_com_phycfg.c:6208-6242]"""
    tpt_diff = 0
    dpd_diff = 0
    if band == BAND_ON_2_4G or not is_cck_rate(rate_idx):     # [SRC :6214-6215]
        dpd_diff = -(rtl8822c_get_dis_dpd_by_rate_diff(dis_dpd_rate, rate_idx)
                     * HAL_SPEC_TXGI_PDBM)
    return tpt_diff + dpd_diff


@dataclass(frozen=True)
class TxPwrIdxState:
    """Everything hal_com_get_txpwr_idx reads that a channel switch does not change."""
    hal_txpwr: HalTxPwrInfo
    by_rate: TxPwrByRate
    dis_dpd_rate: int
    txpath_num_nss: tuple[int, ...]


def txpwr_idx_state(efuse: EfuseInfo, rf_path: RfPath) -> TxPwrIdxState | None:
    """None means the chip is in TSSI mode and TX power is not ours to compute: hal_load_txpwr_info
    has no PG bases to give, exactly as rtw_hal_dm_init never calls it there
    [SRC hal/hal_intf.c:199-201]. A None caller must skip the TX power write, not substitute a
    value. [SRC hal/hal_intf.c:190-205]"""
    hal_txpwr = hal_load_txpwr_info(efuse, rf_path.max_tx_cnt)
    if hal_txpwr is None:
        return None
    return TxPwrIdxState(
        hal_txpwr,
        phy_load_tx_power_by_rate(eeprom_regulatory=efuse.eeprom_regulatory,
                                  tx_nss=hal_tx_nss(rf_path.max_tx_cnt)),
        efuse.dis_dpd_rate,
        hal_txpath_num_nss(rf_path.trx_path_bmp, rf_path.max_tx_cnt),
    )


def hal_com_get_txpwr_idx(state: TxPwrIdxState, rfpath: int, rs: int, rate_idx: int, bw: int,
                          band: int, cch: int, opch: int = 0, *, cch_20: int) -> int:
    """The TX gain index for one (path, rate) on one channel, 0..txgi_max.

    The base is the C's u8 read back into an s8 [SRC :6272,6293]. Only the TXPWR_PG_WITH_PWR_IDX
    arm exists: state is None in TSSI mode and the caller never gets here.
    [SRC hal/hal_com_phycfg.c:6265-6353]"""
    if rs >= RATE_SECTION_NUM:
        # The C screens this one level up, in the section loop [SRC :2337-2341].
        raise ValueError(f"RTL8822CU invalid rate section {rs}")
    ntx_idx = phy_get_current_tx_num(state.txpath_num_nss, rate_idx)
    rate_target = phy_get_txpwr_target(state.by_rate, rfpath, rs, rate_idx, ntx_idx,
                                       bw, band, cch, opch)
    rate_amends = phy_get_txpwr_amends(state.dis_dpd_rate, rfpath, rs, rate_idx, ntx_idx,
                                       bw, band, cch)

    base = _s8(phy_get_pg_txpwr_idx(state.hal_txpwr, rfpath, rs, ntx_idx, bw, band, cch, opch,
                                    cch_20=cch_20))
    rs_target = state.by_rate.phy_get_target_txpwr(band, rfpath, rs)
    power_idx = base + (rate_target - rs_target) + rate_amends

    return min(max(power_idx, 0), HAL_SPEC_TXGI_MAX)
