"""ar9002 calibration — ar9002_hw_init_cal and its helpers.

Ported from ar9002_calib.c / calib.c. Run at the tail of ath9k_hw_reset, after the baseband
comes up (init_bb). The AR9271 / 2.4 GHz / legacy-20 path is:

    cl_cal (non-HT20 branch) -> ar9271_hw_pa_cal -> loadnf -> start_nfcal -> reset_calibration

reset_calibration only sets up the IQ-mismatch cal here: ADC-gain/ADC-dc cals are HT40-only
(ar9002_hw_is_cal_supported), and this channel is legacy-20.

The NF/PA loops are feedback-driven — the written values depend on register reads, so the
strict-cursor replay's recorded read responses drive them positionally.
"""
from __future__ import annotations

from . import reg as R
from .chan import Channel
from .hw import AthHw

# ar5416_cca_regs — ah->nf_regs [SRC] ar5008_phy.c:1346. Only index 0 is touched on this
# 1T1R non-HT40 path (chains 1/2 are masked out, the ext chain is HT40-only).
NF_REGS = [R.AR_PHY_CCA, 0xA864, 0xB864, R.AR_PHY_EXT_CCA0, 0xA9BC, 0xB9BC]


def cl_cal(hw: AthHw, chan: Channel) -> bool:
    """ar9285_hw_cl_cal [SRC] ar9002_calib.c:747 — carrier-leak calibration. The HT20
    parallel-cal branch is skipped on the legacy-20 channel."""
    hw.rmw(R.AR_PHY_CL_CAL_CTL, R.AR_PHY_CL_CAL_ENABLE, 0)
    if chan.is_ht20():                                   # not taken on the 2.4 GHz legacy path
        hw.rmw(R.AR_PHY_CL_CAL_CTL, R.AR_PHY_PARALLEL_CAL_ENABLE, 0)
        hw.rmw(R.AR_PHY_TURBO, R.AR_PHY_FC_DYN2040_EN, 0)
        hw.rmw(R.AR_PHY_AGC_CONTROL, 0, R.AR_PHY_AGC_CONTROL_FLTR_CAL)
        hw.rmw(R.AR_PHY_TPCRG1, 0, R.AR_PHY_TPCRG1_PD_CAL_ENABLE)
        hw.rmw(R.AR_PHY_AGC_CONTROL, R.AR_PHY_AGC_CONTROL_CAL, 0)
        if not hw.wait(R.AR_PHY_AGC_CONTROL, R.AR_PHY_AGC_CONTROL_CAL, 0):
            return False
        hw.rmw(R.AR_PHY_TURBO, 0, R.AR_PHY_FC_DYN2040_EN)
        hw.rmw(R.AR_PHY_CL_CAL_CTL, 0, R.AR_PHY_PARALLEL_CAL_ENABLE)
        hw.rmw(R.AR_PHY_CL_CAL_CTL, 0, R.AR_PHY_CL_CAL_ENABLE)
    hw.rmw(R.AR_PHY_ADC_CTL, 0, R.AR_PHY_ADC_CTL_OFF_PWDADC)
    hw.rmw(R.AR_PHY_AGC_CONTROL, R.AR_PHY_AGC_CONTROL_FLTR_CAL, 0)
    hw.rmw(R.AR_PHY_TPCRG1, R.AR_PHY_TPCRG1_PD_CAL_ENABLE, 0)
    hw.rmw(R.AR_PHY_AGC_CONTROL, R.AR_PHY_AGC_CONTROL_CAL, 0)
    if not hw.wait(R.AR_PHY_AGC_CONTROL, R.AR_PHY_AGC_CONTROL_CAL, 0):
        return False
    hw.rmw(R.AR_PHY_ADC_CTL, R.AR_PHY_ADC_CTL_OFF_PWDADC, 0)
    hw.rmw(R.AR_PHY_CL_CAL_CTL, 0, R.AR_PHY_CL_CAL_ENABLE)
    hw.rmw(R.AR_PHY_AGC_CONTROL, 0, R.AR_PHY_AGC_CONTROL_FLTR_CAL)
    return True


def pa_cal(hw: AthHw) -> None:
    """ar9271_hw_pa_cal(is_reset=True) [SRC] ar9002_calib.c:437 — PA offset calibration:
    snapshot the 8 analog-shift regs, run the off_6_1 search loop, restore them.

    NOTE: this capture's firmware emits only the first 10 of the pre-loop buffer's RMWs. The
    v6.18 source has 5 more (RF2G1 PDPADRV2/PDPAOUT clear + RF2G8/RF2G7 PADRVGN2TAB0 + RF2G3
    CCOMP field writes) — all on registers this function restores at its tail, which this
    firmware revision does not issue. The wire is the gate, so they are omitted here.
    """
    reg_list = [R.AR9285_AN_TOP3, R.AR9285_AN_RXTXBB1, R.AR9285_AN_RF2G1, R.AR9285_AN_RF2G2,
                R.AR9285_AN_TOP2, R.AR9285_AN_RF2G8, R.AR9285_AN_RF2G7, R.AR9285_AN_RF2G3]
    saved = hw.multi_read(reg_list)

    hw.enable_rmw_buffer()
    hw.rmw(R.AR9285_AN_RF2G6, 0, 1 << 0)                            # 7834 b1=0
    hw.rmw(0x9808, 1 << 27, 0)                                      # 9808 b27=1
    hw.rmw(R.AR9285_AN_TOP3, R.AR9285_AN_TOP3_PWDDAC, 0)            # pwddac=1
    hw.rmw(R.AR9285_AN_RXTXBB1, R.AR9285_AN_RXTXBB1_PDRXTXBB1, 0)   # pdrxtxbb=1
    hw.rmw(R.AR9285_AN_RXTXBB1, R.AR9285_AN_RXTXBB1_PDV2I, 0)       # pdv2i=1
    hw.rmw(R.AR9285_AN_RXTXBB1, R.AR9285_AN_RXTXBB1_PDDACIF, 0)     # pddacinterface=1
    hw.rmw(R.AR9285_AN_RF2G2, 0, R.AR9285_AN_RF2G2_OFFCAL)          # offcal=0
    hw.rmw(R.AR9285_AN_RF2G7, 0, R.AR9285_AN_RF2G7_PWDDB)           # pwddb=0
    hw.rmw(R.AR9285_AN_RF2G1, 0, R.AR9285_AN_RF2G1_ENPACAL)         # enpacal=0
    hw.rmw(R.AR9285_AN_RF2G1, 0, R.AR9285_AN_RF2G1_PDPADRV1)        # pdpadrv1=0
    hw.rmw_buffer_flush()

    hw.write(R.AR9285_AN_TOP2, 0xCA0358A0)                          # localmode/synthon/...
    hw.rmw_field(R.AR9285_AN_RF2G6, R.AR9271_AN_RF2G6_OFFS, 0)

    # find off_6_1: per iteration, probe bit (20+i) and fold in RF2G9.spare9 [SRC] :499-510.
    reg_val = 0
    for i in range(6, 0, -1):
        reg_val = hw.read(R.AR9285_AN_RF2G6)
        reg_val |= (1 << (20 + i))
        hw.write(R.AR9285_AN_RF2G6, reg_val)
        reg_val &= ~(1 << (20 + i))
        reg_val |= (R.MS(hw.read(R.AR9285_AN_RF2G9), R.AR9285_AN_RXTXBB1_SPARE9) << (20 + i))
        hw.write(R.AR9285_AN_RF2G6, reg_val)

    hw.enable_rmw_buffer()
    hw.rmw(R.AR9285_AN_RF2G6, 1 << 0, 0)                            # 7834 b1=1
    hw.rmw(0x9808, 0, 1 << 27)                                      # 9808 b27=0
    hw.rmw_buffer_flush()

    hw.enable_write_buffer()
    for reg, val in zip(reg_list, saved):
        hw.write(reg, val)
    hw.write_flush()


def _nfval(default_nf: int) -> int:
    """Cold (no caldata): cal[i] is 0, which fails the (-127, -60) window, so the per-chain
    NF value is the band nominal [SRC] calib.c:264-269."""
    return default_nf


def loadnf(hw: AthHw, chan: Channel) -> None:
    """ath9k_hw_loadnf [SRC] calib.c:240 — load the software-filtered noise floor into the
    baseband minCCApwr. 1T1R: chainmask selects chain 0 only (the ext chain is HT40-only)."""
    chainmask = (hw.rxchainmask << 3) | hw.rxchainmask
    default_nf = R.AR_PHY_CCA_NOM_VAL_9271_2GHZ
    bb_agc_ctl = hw.read(R.AR_PHY_AGC_CONTROL)

    hw.enable_rmw_buffer()
    for i in range(R.NUM_NF_READINGS):
        if chainmask & (1 << i):
            if i >= R.AR5416_MAX_CHAINS and not chan.is_ht40():
                continue
            nfval = _nfval(default_nf)
            hw.rmw(NF_REGS[i], ((nfval << 1) & 0x1FF), 0x1FF)

    if bb_agc_ctl & R.AR_PHY_AGC_CONTROL_NF:               # not set on cold reset
        hw.rmw(R.AR_PHY_AGC_CONTROL, 0, R.AR_PHY_AGC_CONTROL_NF)
        hw.rmw_buffer_flush()
        hw.enable_rmw_buffer()

    hw.rmw(R.AR_PHY_AGC_CONTROL, 0, R.AR_PHY_AGC_CONTROL_ENABLE_NF)
    hw.rmw(R.AR_PHY_AGC_CONTROL, 0, R.AR_PHY_AGC_CONTROL_NO_UPDATE_NF)
    hw.rmw(R.AR_PHY_AGC_CONTROL, R.AR_PHY_AGC_CONTROL_NF, 0)
    hw.rmw_buffer_flush()

    for _ in range(22200):                                # wait for the NF load to complete
        if (hw.read(R.AR_PHY_AGC_CONTROL) & R.AR_PHY_AGC_CONTROL_NF) == 0:
            break

    if bb_agc_ctl & R.AR_PHY_AGC_CONTROL_NF:              # restart NF if it had been running
        hw.enable_rmw_buffer()
        if bb_agc_ctl & R.AR_PHY_AGC_CONTROL_ENABLE_NF:
            hw.rmw(R.AR_PHY_AGC_CONTROL, R.AR_PHY_AGC_CONTROL_ENABLE_NF, 0)
        if bb_agc_ctl & R.AR_PHY_AGC_CONTROL_NO_UPDATE_NF:
            hw.rmw(R.AR_PHY_AGC_CONTROL, R.AR_PHY_AGC_CONTROL_NO_UPDATE_NF, 0)
        hw.rmw(R.AR_PHY_AGC_CONTROL, R.AR_PHY_AGC_CONTROL_NF, 0)
        hw.rmw_buffer_flush()

    # Restore maxCCAPower to -50 so the next baseband NF cal isn't capped [SRC] calib.c:347.
    hw.enable_rmw_buffer()
    for i in range(R.NUM_NF_READINGS):
        if chainmask & (1 << i):
            if i >= R.AR5416_MAX_CHAINS and not chan.is_ht40():
                continue
            hw.rmw(NF_REGS[i], (((-50) << 1) & 0x1FF), 0x1FF)
    hw.rmw_buffer_flush()


def start_nfcal(hw: AthHw, update: bool = True) -> None:
    """ath9k_hw_start_nfcal [SRC] calib.c:222 — kick off the baseband NF calibration."""
    hw.rmw(R.AR_PHY_AGC_CONTROL, R.AR_PHY_AGC_CONTROL_ENABLE_NF, 0)
    if update:
        hw.rmw(R.AR_PHY_AGC_CONTROL, 0, R.AR_PHY_AGC_CONTROL_NO_UPDATE_NF)
    else:
        hw.rmw(R.AR_PHY_AGC_CONTROL, R.AR_PHY_AGC_CONTROL_NO_UPDATE_NF, 0)
    hw.rmw(R.AR_PHY_AGC_CONTROL, R.AR_PHY_AGC_CONTROL_NF, 0)


def _setup_iq_calibration(hw: AthHw) -> None:
    """ar9002_hw_setup_calibration for the IQ-mismatch cal [SRC] ar9002_calib.c:50 — the only
    cal inserted on this non-HT40 path. calCountMax = iq_cal_single_sample.calCountMax."""
    hw.rmw_field(R.AR_PHY_TIMING_CTRL4_0, R.AR_PHY_TIMING_CTRL4_IQCAL_LOG_COUNT_MAX,
                 R.PER_MAX_LOG_COUNT)
    hw.write(R.AR_PHY_CALMODE, R.AR_PHY_CALMODE_IQ)
    hw.rmw(R.AR_PHY_TIMING_CTRL4_0, R.AR_PHY_TIMING_CTRL4_DO_CAL, 0)


def init_cal(hw: AthHw, chan: Channel) -> bool:
    """ar9002_hw_init_cal [SRC] ar9002_calib.c:845 — AR9271 path. Carrier-leak then PA cal,
    load + start the noise floor, then arm the IQ-mismatch per-calibration."""
    if not cl_cal(hw, chan):
        return False
    pa_cal(hw)
    loadnf(hw, chan)
    start_nfcal(hw, update=True)
    _setup_iq_calibration(hw)
    return True
