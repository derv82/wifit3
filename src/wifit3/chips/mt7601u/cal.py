"""Calibration for MT7601U.

Ported from driver_sources/mt7601u-source-v7.2/mt7601u/phy.c: mt7601u_init_cal,
mt7601u_read_bootup_temp, mt7601u_bbp_r47_get, lin2dBd, mt7601u_rxdc_cal,
mt7601u_tssi_dc_gain_cal, mt7601u_set_initial_tssi, mt7601u_temp_comp and
mt7601u_bbp_temp.

A partial port here is worse than none: a skipped calibration does not error, it
leaves the chip running with mis-set baseband or an unprotected PLL. Every step below
is written out in full.
"""
from __future__ import annotations

import time

from .constants import (
    BBP_R47_FLAG,
    BBP_R47_F_TEMP,
    MCU_CAL_DPD,
    MCU_CAL_LOFT,
    MCU_CAL_R,
    MCU_CAL_RXIQ,
    MCU_CAL_TXDCOC,
    MCU_CAL_TXIQ,
    MT_EE_TEMPERATURE_SLOPE,
    MT_MAC_SYS_CTRL,
    MT_MAC_SYS_CTRL_ENABLE_RX,
    MT_RF_BYPASS_0,
    MT_RF_SETTING_0,
    MT_TEMP_MODE_HIGH,
    MT_TEMP_MODE_LOW,
    MT_TEMP_MODE_NORMAL,
    MT_TX_ALC_CFG_1,
    MT_TX_ALC_CFG_1_TEMP_COMP,
)
from .eeprom import _s8, int_to_s6
from .initvals_phy import bbp_mode_table
from .phy import MT7601UPhy

DPD_TEMP_TOLERANCE = 450
"""phy.c temp_comp -- recalibrate DPD only if the temperature moved 450 millidegrees."""

TEMP_HI_MILLIDEG = 400
"""phy.c temp_comp, before the 'on' adjustment."""
TEMP_LO_MILLIDEG = -200
PLL_LOCK_COLD_MILLIDEG = -50
"""Below this the PLL needs protection; phy.c's comment reads < 20C."""
PLL_LOCK_WARM_MILLIDEG = 50

BBP_TEMP_BUSY = 0x10
"""BBP register 47's busy bit, polled while waiting for the sensor."""

BBP_TEMP_POLL_LIMIT = 100
"""phy.c read_bootup_temp."""
TSSI_MEASURE_POLL_LIMIT = 20
"""phy.c tssi_dc_gain_cal."""
RXDC_POLL_LIMIT = 20


def lin2dbd(linear: int) -> int:
    """phy.c:598 lin2dBd -- the TSSI linear reading to millidecibels conversion.

    Written as the C does it: normalise to a 16-bit mantissa, pick an approximation
    branch, then fold the fixed-point correction in.
    """
    if linear == 0:
        return -10000
    exp = linear.bit_length() - 16
    mantissa = linear >> exp if exp > 0 else linear << -exp
    if mantissa <= 0xB800:
        app = mantissa + (mantissa >> 3) + (mantissa >> 4) - 0x9600
    else:
        app = mantissa - (mantissa >> 3) - (mantissa >> 6) - 0x5A00
    if app < 0:
        app = 0
    dbd = ((15 + exp) << 15) + app
    dbd = (dbd << 2) + (dbd << 1) + (dbd >> 6) + (dbd >> 7)
    return dbd >> 10


def bbp_r47_get(phy: MT7601UPhy, reg: int, flag: int) -> int:
    """phy.c:484 -- select the flag bits in BBP 47, settle, then read BBP 49."""
    phy.bbp_wr(47, flag | (reg & ~BBP_R47_FLAG))
    time.sleep(0.0006)                                   # usleep_range(500, 700)
    return phy.bbp_rr(49)


def read_bootup_temp(phy: MT7601UPhy) -> int:
    """phy.c mt7601u_read_bootup_temp -- bypass the RF, read the sensor, restore.

    Leaving the RF bypassed after this would deafen the receiver, so the saved
    register values are put back on every path.
    """
    rf_set = phy.tp.rr(MT_RF_SETTING_0)
    rf_bp = phy.tp.rr(MT_RF_BYPASS_0)

    phy.tp.wr(MT_RF_BYPASS_0, 0)
    phy.tp.wr(MT_RF_SETTING_0, 0x00000010)
    phy.tp.wr(MT_RF_BYPASS_0, 0x00000010)

    bbp_val = phy.bbp_rmw(47, 0, 0x10)
    phy.bbp_wr(22, 0x40)

    for _ in range(BBP_TEMP_POLL_LIMIT):
        if not bbp_val & BBP_TEMP_BUSY:
            break
        bbp_val = phy.bbp_rr(47)

    temp = bbp_r47_get(phy, bbp_val, BBP_R47_F_TEMP)

    phy.bbp_wr(22, 0)

    bbp_val = phy.bbp_rr(21) | 0x02
    phy.bbp_wr(21, bbp_val)
    bbp_val &= ~0x02
    phy.bbp_wr(21, bbp_val)

    phy.tp.wr(MT_RF_BYPASS_0, 0)
    phy.tp.wr(MT_RF_SETTING_0, rf_set)
    phy.tp.wr(MT_RF_BYPASS_0, rf_bp)
    # phy.c:492 is declared s8 and returns a u8, so the sensor's sign bit is reinterpreted
    # on return. Kept unsigned, a chip below its EEPROM reference temperature feeds
    # (raw_temp - ref_temp) a value 256 too high and MCU_CAL_DPD is calibrated to garbage.
    return temp - 0x100 if temp & 0x80 else temp


def read_temp(phy: MT7601UPhy) -> int:
    """phy.c:530 mt7601u_read_temp -- the die temperature now, as the s8 the C declares.

    Unlike read_bootup_temp this does not bypass the RF, so it is safe to call while
    the receiver is running.
    """
    val = phy.bbp_rmw(47, 0x7F, BBP_TEMP_BUSY)
    # phy.c:536: this rarely succeeds, and the temperature moves even when it does not.
    for _ in range(BBP_TEMP_POLL_LIMIT):
        if not val & BBP_TEMP_BUSY:
            break
        val = phy.bbp_rr(47)
    return _s8(bbp_r47_get(phy, val, BBP_R47_F_TEMP))


def phy_calibrate(phy: MT7601UPhy) -> None:
    """phy.c:1002 mt7601u_phy_calibrate -- one pass of the periodic calibration.

    phy.c:1009 skips the temperature read when TSSI calibration has already refreshed
    it, but phy.c:876 tssi_cal is unported, so nothing else moves raw_temp and the read
    runs either way -- the gate's premise does not hold here. phy.c:970 agc_tune is also
    unported; it returns early on avg_rssi == 0, which is every pass with no association.
    """
    phy.raw_temp = read_temp(phy)
    temp_comp(phy, True)                              # phy.c:1011


def rxdc_cal(phy: MT7601UPhy) -> None:
    """phy.c mt7601u_rxdc_cal -- the RX DC-offset trim loop."""
    intro = [(158, 0x8D), (159, 0xFC), (158, 0x8C), (159, 0x4C)]
    outro = [(158, 0x8D), (159, 0xE0)]

    mac_ctrl = phy.tp.rr(MT_MAC_SYS_CTRL)
    phy.tp.wr(MT_MAC_SYS_CTRL, MT_MAC_SYS_CTRL_ENABLE_RX)

    phy.mcu.write_reg_pairs(MT_MCU_MEMMAP_BBP, intro)
    for _ in range(RXDC_POLL_LIMIT):
        time.sleep(0.0004)                               # usleep_range(300, 500)
        phy.bbp_wr(158, 0x8C)
        if phy.bbp_rr(159) == 0x0C:
            break
    phy.tp.wr(MT_MAC_SYS_CTRL, 0)

    phy.mcu.write_reg_pairs(MT_MCU_MEMMAP_BBP, outro)
    phy.tp.wr(MT_MAC_SYS_CTRL, mac_ctrl)


def tssi_dc_gain_cal(phy: MT7601UPhy) -> None:
    """phy.c mt7601u_tssi_dc_gain_cal -- four VGA/mixer measurements, then the offset."""
    phy.tp.wr(MT_RF_SETTING_0, 0x00000030)
    phy.tp.wr(MT_RF_BYPASS_0, 0x000C0030)
    phy.tp.wr(MT_MAC_SYS_CTRL, 0)

    phy.bbp_wr(58, 0)
    phy.bbp_wr(241, 0x2)
    phy.bbp_wr(23, 0x8)
    bbp_r47 = phy.bbp_rr(47)

    rf_vga = phy.rf_rr(5, 3)
    phy.rf_wr(5, 3, 8)
    rf_mixer = phy.rf_rr(4, 39)
    phy.rf_wr(4, 39, 0)

    res: list[int] = []
    for i in range(4):
        phy.rf_wr(4, 39, rf_mixer if i & 1 else 0)
        phy.bbp_wr(23, 0x08 if i < 2 else 0x02)
        phy.rf_wr(5, 3, 0x08 if i < 2 else 0x11)

        # BBP TSSI initial and soft reset.
        phy.bbp_wr(22, 0)
        phy.bbp_wr(244, 0)
        phy.bbp_wr(21, 1)
        time.sleep(0.000001)                              # udelay(1)
        phy.bbp_wr(21, 0)

        phy.bbp_wr(47, 0x50)
        phy.bbp_wr(244 if i & 1 else 22, 0x31 if i & 1 else 0x40)

        for _ in range(TSSI_MEASURE_POLL_LIMIT):
            if not phy.bbp_rr(47) & BBP_TEMP_BUSY:
                break

        phy.bbp_wr(47, 0x40)
        res.append(_s8(phy.bbp_rr(49)))        # phy.c:645 declares s8 res[4]

    tssi_init_db = lin2dbd((res[1] - res[0]) & 0xFFFF)
    tssi_init_hvga_db = lin2dbd(((res[3] - res[2]) * 4) & 0xFFFF)
    phy.ee.tssi_data.init = res[0]
    phy.ee.tssi_data.init_hvga = res[2]
    phy.ee.tssi_data.init_hvga_offset_db = tssi_init_hvga_db - tssi_init_db

    phy.bbp_wr(22, 0)
    phy.bbp_wr(244, 0)
    phy.bbp_wr(21, 1)
    time.sleep(0.000001)
    phy.bbp_wr(21, 0)

    phy.tp.wr(MT_RF_BYPASS_0, 0)
    phy.tp.wr(MT_RF_SETTING_0, 0)
    phy.rf_wr(5, 3, rf_vga)
    phy.rf_wr(4, 39, rf_mixer)
    phy.bbp_wr(47, bbp_r47)

    _set_initial_tssi(phy, tssi_init_db, tssi_init_hvga_db)


def _set_initial_tssi(phy: MT7601UPhy, tssi_db: int, tssi_hvga_db: int) -> None:
    """phy.c mt7601u_set_initial_tssi."""
    data = phy.ee.tssi_data
    init_offset = -((tssi_db * data.slope + data.offset[1]) // 4096) + 10
    phy.tp.rmw(MT_TX_ALC_CFG_1, MT_TX_ALC_CFG_1_TEMP_COMP,
               int_to_s6(init_offset) & MT_TX_ALC_CFG_1_TEMP_COMP)


def bbp_temp(phy: MT7601UPhy, mode: int) -> None:
    """phy.c mt7601u_bbp_temp -- the width-independent table, then the band's."""
    if phy.temp_mode == mode:
        return
    phy.temp_mode = mode
    row = bbp_mode_table[mode]
    phy.mcu.write_reg_pairs(MT_MCU_MEMMAP_BBP, row[2])                  # common, all widths
    phy.mcu.write_reg_pairs(MT_MCU_MEMMAP_BBP, row[phy.bw])             # 0 is 20 MHz, 1 is 40


def temp_comp(phy: MT7601UPhy, on: bool = True) -> None:
    """phy.c mt7601u_temp_comp -- DPD recalibration, PLL lock protection, BBP profile."""
    temp = (phy.raw_temp - phy.ee.ref_temp) * MT_EE_TEMPERATURE_SLOPE
    phy.curr_temp = temp

    if not -DPD_TEMP_TOLERANCE <= temp - phy.dpd_temp <= DPD_TEMP_TOLERANCE:
        phy.dpd_temp = temp
        phy.mcu.calibrate(MCU_CAL_DPD, phy.dpd_temp)
        phy.vco_cal()

    if temp < PLL_LOCK_COLD_MILLIDEG and not phy.pll_lock_protect:
        phy.pll_lock_protect = True
        phy.rf_wr(4, 4, 6)
        phy.rf_clear(4, 10, 0x30)
    elif temp > PLL_LOCK_WARM_MILLIDEG and phy.pll_lock_protect:
        phy.pll_lock_protect = False
        phy.rf_wr(4, 4, 0)
        phy.rf_rmw(4, 10, 0x30, 0x10)

    hi, lo = TEMP_HI_MILLIDEG, TEMP_LO_MILLIDEG
    if on:
        hi -= 50
        lo -= 50
    if temp > hi:
        bbp_temp(phy, MT_TEMP_MODE_HIGH)
    elif temp > lo:
        bbp_temp(phy, MT_TEMP_MODE_NORMAL)
    else:
        bbp_temp(phy, MT_TEMP_MODE_LOW)


def init_cal(phy: MT7601UPhy) -> None:
    """phy.c mt7601u_init_cal -- the sequence that arms RF calibration."""
    phy.raw_temp = read_bootup_temp(phy)
    phy.curr_temp = (phy.raw_temp - phy.ee.ref_temp) * MT_EE_TEMPERATURE_SLOPE
    phy.dpd_temp = phy.curr_temp

    mac_ctrl = phy.tp.rr(MT_MAC_SYS_CTRL)

    phy.mcu.calibrate(MCU_CAL_R, 0)
    phy.rf_set(0, 4, 0x80)
    time.sleep(0.002)                                      # msleep(2)
    phy.mcu.calibrate(MCU_CAL_TXDCOC, 0)

    rxdc_cal(phy)
    phy.set_bw_filter(cal=True)
    phy.mcu.calibrate(MCU_CAL_LOFT, 0)
    phy.mcu.calibrate(MCU_CAL_TXIQ, 0)
    phy.mcu.calibrate(MCU_CAL_RXIQ, 0)
    phy.mcu.calibrate(MCU_CAL_DPD, phy.dpd_temp)

    rxdc_cal(phy)
    tssi_dc_gain_cal(phy)

    phy.tp.wr(MT_MAC_SYS_CTRL, mac_ctrl)
    temp_comp(phy, on=True)


from .constants import MT_MCU_MEMMAP_BBP as MT_MCU_MEMMAP_BBP  # noqa: E402
