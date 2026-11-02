"""RTL8188EUS TX-power level (MISC11 stage).

``PHY_SetTxPowerLevel8188E`` -> ``phy_set_tx_power_level_by_path(RF_PATH_A)``
[SRC] rtl8188e_phycfg.c:1166, hal_com_phycfg.c:2737. This card is 1T1R / 2.4 GHz, so
only path A and the CCK, OFDM, and HT-MCS0-7 rate sections apply; each rate writes one
byte of a packed txagc register via a masked RMW (``PHY_SetTxPowerIndex_8188E``).

Per-rate power index [SRC] PHY_GetTxPowerIndex_8188E:
``idx = clamp(base + by_rate + tpt + extra_bias, 0, MAX_POWER_INDEX)``. This build
compiles ``CONFIG_TXPWR_BY_RATE_EN=0`` and ``CONFIG_TXPWR_LIMIT_EN=0`` (Makefile), so
``by_rate`` and the regulatory limit are dead code (0 / non-binding). ``tpt`` is the
power-track remnant (``PHY_GetTxPowerTrackingOffset``, hal_com_phycfg.c:2368): CCK rates take
``remnant_cck_swing_idx``, the rest ``remnant_ofdm_swing_idx``; both 0 until power tracking
clamps the BB swing. ``extra_bias`` is -9 for MGN_2M (``tx_power_extra_bias``). So
``idx = clamp(base + tpt + extra_bias, 0, 0x3F)``, where base = efuse PG index for the rate's
channel group [SRC] PHY_GetTxPowerIndexBase:
    CCK  -> Index24G_CCK_Base[cck_group]  + CCK_24G_Diff[1TX]
    OFDM -> Index24G_BW40_Base[group]     + OFDM_24G_Diff[1TX]
    HT   -> Index24G_BW40_Base[group]     + BW20_24G_Diff[1TX]   (BW20 only)
[WIRE] cap1 ops 1589-1628 (40 ops) on the init channel (6).
"""
from __future__ import annotations

from . import bb
from .constants import (
    bMaskByte0,
    bMaskByte1,
    bMaskByte2,
    bMaskByte3,
    MAX_POWER_INDEX,
    rTxAGC_A_CCK1_Mcs32,
    rTxAGC_A_Mcs03_Mcs00,
    rTxAGC_A_Mcs07_Mcs04,
    rTxAGC_A_Rate18_06,
    rTxAGC_A_Rate54_24,
    rTxAGC_B_CCK11_A_CCK2_11,
    TXPWR_2M_EXTRA_BIAS,
)

CCK, OFDM, HT_MCS0_MCS7 = 0, 1, 2     # rate sections (enum RATE_SECTION)

# Per-rate (section, reg, byte-mask, extra_bias), in the vendor rate-section order
# (mgn_rates_cck / _ofdm / _mcs0_7) and the path-A register/byte map from
# PHY_SetTxPowerIndex_8188E. Only MGN_2M carries an extra bias.
_RATES = (
    (CCK,  rTxAGC_A_CCK1_Mcs32,      bMaskByte1, 0),                    # 1M
    (CCK,  rTxAGC_B_CCK11_A_CCK2_11, bMaskByte1, TXPWR_2M_EXTRA_BIAS),  # 2M
    (CCK,  rTxAGC_B_CCK11_A_CCK2_11, bMaskByte2, 0),                    # 5.5M
    (CCK,  rTxAGC_B_CCK11_A_CCK2_11, bMaskByte3, 0),                    # 11M
    (OFDM, rTxAGC_A_Rate18_06, bMaskByte0, 0),   # 6M
    (OFDM, rTxAGC_A_Rate18_06, bMaskByte1, 0),   # 9M
    (OFDM, rTxAGC_A_Rate18_06, bMaskByte2, 0),   # 12M
    (OFDM, rTxAGC_A_Rate18_06, bMaskByte3, 0),   # 18M
    (OFDM, rTxAGC_A_Rate54_24, bMaskByte0, 0),   # 24M
    (OFDM, rTxAGC_A_Rate54_24, bMaskByte1, 0),   # 36M
    (OFDM, rTxAGC_A_Rate54_24, bMaskByte2, 0),   # 48M
    (OFDM, rTxAGC_A_Rate54_24, bMaskByte3, 0),   # 54M
    (HT_MCS0_MCS7, rTxAGC_A_Mcs03_Mcs00, bMaskByte0, 0),  # MCS0
    (HT_MCS0_MCS7, rTxAGC_A_Mcs03_Mcs00, bMaskByte1, 0),  # MCS1
    (HT_MCS0_MCS7, rTxAGC_A_Mcs03_Mcs00, bMaskByte2, 0),  # MCS2
    (HT_MCS0_MCS7, rTxAGC_A_Mcs03_Mcs00, bMaskByte3, 0),  # MCS3
    (HT_MCS0_MCS7, rTxAGC_A_Mcs07_Mcs04, bMaskByte0, 0),  # MCS4
    (HT_MCS0_MCS7, rTxAGC_A_Mcs07_Mcs04, bMaskByte1, 0),  # MCS5
    (HT_MCS0_MCS7, rTxAGC_A_Mcs07_Mcs04, bMaskByte2, 0),  # MCS6
    (HT_MCS0_MCS7, rTxAGC_A_Mcs07_Mcs04, bMaskByte3, 0),  # MCS7
)


def ch_group_2g(channel: int) -> tuple[int, int]:
    """``rtw_get_ch_group`` (2.4G) [SRC] rtw_rf.c:352 -> (bw40_group, cck_group)."""
    if channel <= 2:
        gp = 0
    elif channel <= 5:
        gp = 1
    elif channel <= 8:
        gp = 2
    elif channel <= 11:
        gp = 3
    else:                       # 12..14
        gp = 4
    cck_gp = 5 if channel == 14 else gp
    return gp, cck_gp


def _power_index(tx_pwr, gp: int, cck_gp: int, section: int, bias: int, tpt: int) -> int:
    if section == CCK:
        base = tx_pwr.cck_base[cck_gp] + tx_pwr.cck_diff
    elif section == OFDM:
        base = tx_pwr.bw40_base[gp] + tx_pwr.ofdm_diff
    else:                       # HT_MCS0_MCS7 (BW20 only on this card)
        base = tx_pwr.bw40_base[gp] + tx_pwr.bw20_diff
    return max(0, min(MAX_POWER_INDEX, base + tpt + bias))


def set_tx_power_by_rate_section(t, tx_pwr, channel: int, section: int,
                                 remnant_cck_swing_idx: int, remnant_ofdm_swing_idx: int) -> None:
    """``phy_set_tx_power_index_by_rate_section`` [SRC] hal_com_phycfg.c:2139 (path A)."""
    gp, cck_gp = ch_group_2g(channel)
    tpt = remnant_cck_swing_idx if section == CCK else remnant_ofdm_swing_idx
    for rate_section, reg, mask, bias in _RATES:
        if rate_section == section:
            bb.set_bb_reg(t, reg, mask, _power_index(tx_pwr, gp, cck_gp, section, bias, tpt))


def set_tx_power(t, tx_pwr, channel: int,
                 remnant_cck_swing_idx: int, remnant_ofdm_swing_idx: int) -> None:
    """Write the per-rate path-A txagc bytes for ``channel`` (each a masked RMW)."""
    for section in (CCK, OFDM, HT_MCS0_MCS7):
        set_tx_power_by_rate_section(t, tx_pwr, channel, section,
                                     remnant_cck_swing_idx, remnant_ofdm_swing_idx)
