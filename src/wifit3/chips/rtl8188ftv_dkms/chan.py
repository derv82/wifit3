"""RTL8188FTV DKMS channel tune, 20 MHz (M5f).

Ported from ``PHY_HandleSwChnlAndSetBW8188F`` /
``phy_SwChnlAndSetBwMode8188F`` / ``phy_SwChnl8188F`` /
``phy_PostSetBwMode8188F`` / ``PHY_RF6052SetBandwidth8188F`` /
``phy_SpurCalibration_8188F``
(hal/rtl8188f/rtl8188f_phycfg.c:1179-1290,955-1105,60-100).
``RfRegChnlVal`` channel state threads through ``tune_20`` (it persists on
the HalData in the source). 40 MHz arms compile in but never run here.
"""
from __future__ import annotations

from . import bb, rf


def BIT(n: int) -> int:
    return 1 << n


SPUR_FREQS = (0xFCCD, 0xFC4D, 0xFFCD, 0xFF4D, 0xFCCD, 0xFF9A, 0xFDCD)


def spur_calibration(t, channel: int, threshold: int = 0x16) -> None:
    bb.set_bb_reg(t, 0xC40, BIT(28) | BIT(27) | BIT(26) | BIT(25) | BIT(24), 0x1F)
    bb.set_bb_reg(t, 0xC40, BIT(9), 0x1)
    if threshold <= 0:
        threshold = 0x16
    idx = {5: 0, 6: 1, 7: 2, 8: 3, 13: 4, 14: 5, 11: 6}.get(channel, 10)
    reg948 = bb.query_bb_reg(t, 0x948, 0xFFFFFFFF)
    hw_s1 = sw_s1 = False
    if reg948 & BIT(6):
        hw_s1 = bb.query_bb_reg(t, 0x864, BIT(5) | BIT(4) | BIT(3)) == 0x1
    else:
        sw_s1 = (reg948 & BIT(9)) == 0x0
    if (hw_s1 or sw_s1) and idx <= 6:
        # TODO: verify, untested here, needs a spur-channel tune (5-8,11,13,14)
        raise ValueError("spur PSD path untested here")
    bb.set_bb_reg(t, 0xD2C, BIT(28), 0x0)


def sw_chnl(t, channel: int, rf_chnl_val: int) -> int:
    rf_chnl_val = (rf_chnl_val & 0xFFFFF00) | channel
    rf.set_rf_reg(t, rf.RF_PATH_A, 0x18, 0x3FF, rf_chnl_val)
    spur_calibration(t, channel, 0x16)
    return rf_chnl_val


def post_set_bw_mode_20(t, rf_chnl_val: int) -> int:
    bb.set_bb_reg(t, 0x800, BIT(0), 0x0)
    bb.set_bb_reg(t, 0x900, BIT(0), 0x0)
    bb.set_bb_reg(t, 0x800, BIT(10) | BIT(9) | BIT(8), 0x7)
    bb.set_bb_reg(t, 0x800, BIT(14) | BIT(13) | BIT(12), 0x5)
    bb.set_bb_reg(t, 0xCE4, BIT(31) | BIT(30), 0x0)
    bb.set_bb_reg(t, 0xCE4, BIT(29) | BIT(28), 0x1)
    bb.set_bb_reg(t, 0xC10, BIT(29) | BIT(28), 0x1)
    bb.set_bb_reg(t, 0x954, BIT(19), 0x0)
    bb.set_bb_reg(t, 0x954, BIT(23) | BIT(22) | BIT(21) | BIT(20), 0x3)
    return rf_bandwidth_20(t, rf_chnl_val)


def rf_bandwidth_20(t, rf_chnl_val: int) -> int:
    rf_chnl_val = (rf_chnl_val & 0xFFFFF3FF) | BIT(10) | BIT(11)
    rf.set_rf_reg(t, rf.RF_PATH_A, 0x18, rf.RF_REG_OFFSET_MASK, rf_chnl_val)
    rf.set_rf_reg(t, rf.RF_PATH_A, 0x87, rf.RF_REG_OFFSET_MASK, 0x00065)
    rf.set_rf_reg(t, rf.RF_PATH_A, 0x1C, rf.RF_REG_OFFSET_MASK, 0x00000)
    rf.set_rf_reg(t, rf.RF_PATH_A, 0xDF, rf.RF_REG_OFFSET_MASK, 0x00140)
    rf.set_rf_reg(t, rf.RF_PATH_A, 0x1B, rf.RF_REG_OFFSET_MASK, 0x01C6C)
    return rf_chnl_val


def tune_20(t, channel: int, rf_chnl_val: int = 0) -> int:
    rf_chnl_val = sw_chnl(t, channel, rf_chnl_val)
    return post_set_bw_mode_20(t, rf_chnl_val)
