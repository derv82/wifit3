"""RTL8188EUS channel tune — PHY_SwChnl8188E + PHY_SetBWMode8188E (20 MHz).

This card is 2.4 GHz / 1T1R. A tune is [SRC] rtl8188e_phycfg.c:1745/1531:
  PHY_SwChnl8188E -> _PHY_SwChnl8188E:
    PHY_SetTxPowerLevel8188E(channel)               # s1 (tune TX power for the group)
    RfRegChnlVal[A] = (RfRegChnlVal[A] & ~0x3ff) | channel ; write RF_CHNLBW
  PHY_SetBWMode8188E(20 MHz) -> _PHY_SetBWMode88E:
    REG_BWOPMODE |= BW_OPMODE_20MHZ
    rFPGA0_RFMOD[0] = 0 ; rFPGA1_RFMOD[0] = 0
    rtl8188e_PHY_RF6052SetBandwidth(20M): RfRegChnlVal[A][11:10] = 0b11 ; write RF_CHNLBW

``RfRegChnlVal[A]`` is **stateful** across tunes (channel field [9:0], BW field [11:10]) —
seeded from the M4a read and threaded by the caller. The spur calibration in PHY_SwChnl/
PHY_SetBWMode is ``IS_VENDOR_8188E_I_CUT_SERIES``-only (``i_cut_series``).
"""
from __future__ import annotations

from . import bb, rf, txpower
from .dig import WatchdogState
from .constants import (
    BW_OPMODE_20MHZ,
    bRFMOD,
    NOTCH_FILTER_EN,
    NOTCH_INTF_TH,
    NOTCH_RXDSP_TH,
    REG_BWOPMODE,
    REG_RRSR_RSC,
    rFPGA0_RFMOD,
    rFPGA1_RFMOD,
    RF_BW_20M,
    RF_BW_MASK,
    RF_CHNL_MASK,
    RF_CHNLBW,
    RFREGOFFSETMASK,
    rOFDM0_RxDSP,
    rOFDM1_IntfDet,
    RTW_NOTCH_FILTER,
)

CHANNELS_2G = list(range(1, 15))   # 2.4 GHz channels 1-14 (20 MHz)


def _spur_calibration(t, channel: int, watchdog_state: WatchdogState) -> None:
    """``phy_SpurCalibration_8188E`` [SRC] rtl8188e_phycfg.c:1491, 20 MHz branches."""
    if channel in (13, 14):
        bb.set_bb_reg(t, rOFDM0_RxDSP, NOTCH_FILTER_EN, 0x1)
        bb.set_bb_reg(t, rOFDM1_IntfDet, NOTCH_INTF_TH, 0x2)
        bb.set_bb_reg(t, rOFDM0_RxDSP, NOTCH_RXDSP_TH, 0x1F)
        watchdog_state.is_receiver_blocking_en = False
    elif RTW_NOTCH_FILTER == 0:
        bb.set_bb_reg(t, rOFDM0_RxDSP, NOTCH_FILTER_EN, 0x0)


def set_channel(t, tx_pwr, rf_chnl_val: int, channel: int, i_cut_series: bool,
                remnant_cck_swing_idx: int, remnant_ofdm_swing_idx: int,
                watchdog_state: WatchdogState) -> int:
    """Tune to ``channel`` at 20 MHz. Returns the updated RfRegChnlVal[A]. ``watchdog_state``
    carries ``dm->is_receiver_blocking_en``, which the spur calibration clears."""
    # PHY_SwChnl8188E -> _PHY_SwChnl8188E
    txpower.set_tx_power(t, tx_pwr, channel,                 # s1: CmdID_SetTxPowerLevel
                         remnant_cck_swing_idx, remnant_ofdm_swing_idx)
    rf_chnl_val = (rf_chnl_val & RF_CHNL_MASK) | channel
    rf.set_rf_reg(t, 0, RF_CHNLBW, RFREGOFFSETMASK, rf_chnl_val)   # s2: RF_CHNLBW = channel
    if i_cut_series:
        _spur_calibration(t, channel, watchdog_state)

    # PHY_SetBWMode8188E(20 MHz) -> _PHY_SetBWMode88E
    bwop = t.read8(REG_BWOPMODE)
    t.read8(REG_RRSR_RSC)                                    # regRRSR_RSC (40 MHz only)
    t.write8(REG_BWOPMODE, bwop | BW_OPMODE_20MHZ)
    bb.set_bb_reg(t, rFPGA0_RFMOD, bRFMOD, 0)
    bb.set_bb_reg(t, rFPGA1_RFMOD, bRFMOD, 0)
    rf_chnl_val = (rf_chnl_val & RF_BW_MASK) | RF_BW_20M     # RF6052SetBandwidth(20M)
    rf.set_rf_reg(t, 0, RF_CHNLBW, RFREGOFFSETMASK, rf_chnl_val)
    if i_cut_series:
        _spur_calibration(t, channel, watchdog_state)
    return rf_chnl_val
