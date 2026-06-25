"""eeprom set_board_values for the AR9271 (4k map), ported from eeprom_4k.c.

ath9k_hw_4k_set_board_values programs the antenna/switch config, the per-chain gain
(set_gain), the EEPROM-driven antenna-diversity settings, the OB/DB analog bias (AR9271
RF2G3/RF2G4 shift registers), and the RF-control / settling / CCA timing from the modal
header. Single chain, 2.4 GHz; the multi-chain and antenna-diversity-combining branches are
ported behind their guards but not exercised on this card.
"""
from __future__ import annotations

from . import reg as R
from .chan import Channel
from .eeprom_4k import Map4k
from .hw import AthHw

_TXRXATTEN_DEFAULT = 23                  # txRxAttenLocal seed [SRC] eeprom_4k.c:709


def _set_gain(hw: AthHw, eep: Map4k) -> None:
    """ath9k_hw_4k_set_gain [SRC] eeprom_4k.c:702 — switch/iqcal/gain for the single chain,
    mirrored into block 1 (chain offset 0x1000)."""
    blk1 = R.CHAIN_BLOCK1_OFFSET
    hw.enable_rmw_buffer()
    hw.rmw(R.AR_PHY_SWITCH_CHAIN_0, eep.antCtrlChain0, 0)
    hw.rmw(R.AR_PHY_TIMING_CTRL4_0,
           R.SM(eep.iqCalICh0, R.AR_PHY_TIMING_CTRL4_IQCORR_Q_I_COFF)
           | R.SM(eep.iqCalQCh0, R.AR_PHY_TIMING_CTRL4_IQCORR_Q_Q_COFF),
           R.AR_PHY_TIMING_CTRL4_IQCORR_Q_Q_COFF | R.AR_PHY_TIMING_CTRL4_IQCORR_Q_I_COFF)

    txrxatten = _TXRXATTEN_DEFAULT
    if eep.eeprom_rev >= R.AR5416_EEP_MINOR_VER_3:
        txrxatten = eep.txRxAttenCh0
        for off in (0, blk1):
            hw.rmw_field(R.AR_PHY_GAIN_2GHZ + off, R.AR_PHY_GAIN_2GHZ_XATTEN1_MARGIN, eep.bswMargin0)
            hw.rmw_field(R.AR_PHY_GAIN_2GHZ + off, R.AR_PHY_GAIN_2GHZ_XATTEN1_DB, eep.bswAtten0)
            hw.rmw_field(R.AR_PHY_GAIN_2GHZ + off, R.AR_PHY_GAIN_2GHZ_XATTEN2_MARGIN, eep.xatten2Margin0)
            hw.rmw_field(R.AR_PHY_GAIN_2GHZ + off, R.AR_PHY_GAIN_2GHZ_XATTEN2_DB, eep.xatten2Db0)

    for off in (0, blk1):
        hw.rmw_field(R.AR_PHY_RXGAIN + off, R.AR9280_PHY_RXGAIN_TXRX_ATTEN, txrxatten)
        hw.rmw_field(R.AR_PHY_RXGAIN + off, R.AR9280_PHY_RXGAIN_TXRX_MARGIN, eep.rxTxMarginCh0)
    hw.rmw_buffer_flush()


def _set_ant_diversity(hw: AthHw, eep: Map4k) -> None:
    """The pModal->version >= 3 antenna-diversity programming [SRC] eeprom_4k.c:617. The
    ANT_DIV_COMB cap branch is not set on the 9271, so the final read/modify is skipped."""
    c1, c2 = eep.antdiv_ctl1, eep.antdiv_ctl2
    val = hw.read(R.AR_PHY_MULTICHAIN_GAIN_CTL) & ~R.AR_PHY_9285_ANT_DIV_CTL_ALL
    val |= R.SM(c1, R.AR_PHY_9285_ANT_DIV_CTL)
    val |= R.SM(c2, R.AR_PHY_9285_ANT_DIV_ALT_LNACONF)
    val |= R.SM(c2 >> 2, R.AR_PHY_9285_ANT_DIV_MAIN_LNACONF)
    val |= R.SM(c1 >> 1, R.AR_PHY_9285_ANT_DIV_ALT_GAINTB)
    val |= R.SM(c1 >> 2, R.AR_PHY_9285_ANT_DIV_MAIN_GAINTB)
    hw.write(R.AR_PHY_MULTICHAIN_GAIN_CTL, val)
    hw.read(R.AR_PHY_MULTICHAIN_GAIN_CTL)

    val = hw.read(R.AR_PHY_CCK_DETECT) & ~R.AR_PHY_CCK_DETECT_BB_ENABLE_ANT_FAST_DIV
    val |= R.SM(c1 >> 3, R.AR_PHY_CCK_DETECT_BB_ENABLE_ANT_FAST_DIV)
    hw.write(R.AR_PHY_CCK_DETECT, val)
    hw.read(R.AR_PHY_CCK_DETECT)


def _set_analog_bias(hw: AthHw, eep: Map4k) -> None:
    """The AR9271 OB/DB shift-register writes [SRC] eeprom_4k.c (AR_SREV_9271 branch)."""
    ob, db1, db2 = eep.ob, eep.db1, eep.db2
    hw.enable_rmw_buffer()
    hw.rmw_field(R.AR9285_AN_RF2G3, R.AR9271_AN_RF2G3_OB_cck, ob[0])
    hw.rmw_field(R.AR9285_AN_RF2G3, R.AR9271_AN_RF2G3_OB_psk, ob[1])
    hw.rmw_field(R.AR9285_AN_RF2G3, R.AR9271_AN_RF2G3_OB_qam, ob[2])
    hw.rmw_field(R.AR9285_AN_RF2G3, R.AR9271_AN_RF2G3_DB_1, db1[0])
    hw.rmw_field(R.AR9285_AN_RF2G4, R.AR9271_AN_RF2G4_DB_2, db2[0])
    hw.rmw_buffer_flush()


def set_board_values(hw: AthHw, chan: Channel) -> None:
    """ath9k_hw_4k_set_board_values [SRC] eeprom_4k.c:380."""
    eep = Map4k(hw.eeprom)

    hw.write(R.AR_PHY_SWITCH_COM, eep.antCtrlCommon)
    _set_gain(hw, eep)

    if eep.modal_version >= 3:
        _set_ant_diversity(hw, eep)
    if eep.modal_version < 2:                     # untested (the ob/db remap differs pre-v2)
        raise NotImplementedError("ar9271_v2: modal header version < 2 not ported (unseen)")
    _set_analog_bias(hw, eep)

    hw.enable_rmw_buffer()
    hw.rmw_field(R.AR_PHY_SETTLING, R.AR_PHY_SETTLING_SWITCH, eep.switchSettling)
    hw.rmw_field(R.AR_PHY_DESIRED_SZ, R.AR_PHY_DESIRED_SZ_ADC, eep.adcDesiredSize)
    hw.rmw(R.AR_PHY_RF_CTL4,
           R.SM(eep.txEndToXpaOff, R.AR_PHY_RF_CTL4_TX_END_XPAA_OFF)
           | R.SM(eep.txEndToXpaOff, R.AR_PHY_RF_CTL4_TX_END_XPAB_OFF)
           | R.SM(eep.txFrameToXpaOn, R.AR_PHY_RF_CTL4_FRAME_XPAA_ON)
           | R.SM(eep.txFrameToXpaOn, R.AR_PHY_RF_CTL4_FRAME_XPAB_ON), 0)
    hw.rmw_field(R.AR_PHY_RF_CTL3, R.AR_PHY_TX_END_TO_A2_RX_ON, eep.txEndToRxOn)
    hw.rmw_field(R.AR_PHY_CCA, R.AR9280_PHY_CCA_THRESH62, eep.thresh62)
    hw.rmw_field(R.AR_PHY_EXT_CCA0, R.AR_PHY_EXT_CCA0_THRESH62, eep.thresh62)
    if eep.eeprom_rev >= R.AR5416_EEP_MINOR_VER_2:
        hw.rmw_field(R.AR_PHY_RF_CTL2, R.AR_PHY_TX_END_DATA_START, eep.txFrameToDataStart)
        hw.rmw_field(R.AR_PHY_RF_CTL2, R.AR_PHY_TX_END_PA_ON, eep.txFrameToPaOn)
    # rev >= 3 swSettleHt40 only applies to HT40 (this card runs 20 MHz).
    hw.rmw_buffer_flush()

    bb_scale = eep.bb_scale_smrt_antenna & R.EEP_4K_BB_DESIRED_SCALE_MASK
    if eep.raw[31] == 0 and bb_scale != 0:        # txGainType == 0
        raise NotImplementedError("ar9271_v2: bb_desired_scale TX-pwrctrl block not ported (unseen)")