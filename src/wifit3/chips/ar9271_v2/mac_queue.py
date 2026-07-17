"""TX-queue (QCU/DCU) setup, ported from mac.c + the ath9k_htc queue allocation.

The ath9k_htc driver allocates a beacon queue, a CAB queue, and four data queues (one per WMM
AC); ath9k_hw_reset's ath9k_hw_init_queues then programs each active queue's DCU/QCU registers
via ath9k_hw_resettxqueue and the per-queue interrupt masks. This module reproduces that: the
queue-info model, the property normalisation (set_txq_props), the per-queue reset, and the
DQCUMASK seed loop.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import reg as R
from .hw import AthHw


@dataclass
class TxQueueInfo:
    tqi_type: int = R.TXQ_INACTIVE
    tqi_subtype: int = 0
    tqi_qflags: int = 0
    tqi_aifs: int = R.INIT_AIFS
    tqi_cwmin: int = R.TXQ_USEDEFAULT
    tqi_cwmax: int = R.INIT_CWMAX
    tqi_shretry: int = R.INIT_SH_RETRY
    tqi_lgretry: int = R.INIT_LG_RETRY
    tqi_cbrPeriod: int = 0
    tqi_cbrOverflowLimit: int = 0
    tqi_burstTime: int = 0
    tqi_readyTime: int = 0
    tqi_intFlags: int = 0
    tqi_physCompBuf: int = 0


def _ensure_txq(hw: AthHw) -> None:
    if not hw.txq:
        hw.txq = [TxQueueInfo() for _ in range(R.ATH9K_NUM_TX_QUEUES)]


def set_txq_props(hw: AthHw, q: int, qinfo: TxQueueInfo) -> None:
    """ath9k_hw_set_txq_props [SRC] mac.c:196 — normalise the requested properties into the
    queue slot (USEDEFAULT/zero -> the INIT_* defaults; cwmin/cwmax rounded up to 2^n-1)."""
    qi = hw.txq[q]
    qi.tqi_subtype = qinfo.tqi_subtype
    qi.tqi_qflags = qinfo.tqi_qflags
    qi.tqi_aifs = R.INIT_AIFS if qinfo.tqi_aifs == R.TXQ_USEDEFAULT else min(qinfo.tqi_aifs, 255)
    if qinfo.tqi_cwmin != R.TXQ_USEDEFAULT:
        cw = min(qinfo.tqi_cwmin, 1024)
        v = 1
        while v < cw:
            v = (v << 1) | 1
        qi.tqi_cwmin = v
    else:
        qi.tqi_cwmin = qinfo.tqi_cwmin            # stays USEDEFAULT (resolved in resettxqueue)
    if qinfo.tqi_cwmax != R.TXQ_USEDEFAULT:
        cw = min(qinfo.tqi_cwmax, 1024)
        v = 1
        while v < cw:
            v = (v << 1) | 1
        qi.tqi_cwmax = v
    else:
        qi.tqi_cwmax = R.INIT_CWMAX
    qi.tqi_shretry = min(qinfo.tqi_shretry, 15) if qinfo.tqi_shretry else R.INIT_SH_RETRY
    qi.tqi_lgretry = min(qinfo.tqi_lgretry, 15) if qinfo.tqi_lgretry else R.INIT_LG_RETRY
    qi.tqi_cbrPeriod = qinfo.tqi_cbrPeriod
    qi.tqi_cbrOverflowLimit = qinfo.tqi_cbrOverflowLimit
    qi.tqi_burstTime = qinfo.tqi_burstTime
    qi.tqi_readyTime = qinfo.tqi_readyTime


def setup_txqueue(hw: AthHw, qtype: int, qinfo: TxQueueInfo) -> int:
    """ath9k_hw_setuptxqueue [SRC] mac.c:301 — pick the hardware queue number for the type,
    claim the slot, and apply the properties. DATA uses tqi_subtype as the queue index."""
    _ensure_txq(hw)
    if qtype == R.TXQ_BEACON:
        q = R.ATH9K_NUM_TX_QUEUES - 1
    elif qtype == R.TXQ_CAB:
        q = R.ATH9K_NUM_TX_QUEUES - 2
    elif qtype == R.TXQ_DATA:
        q = qinfo.tqi_subtype
    else:
        raise NotImplementedError(f"ar9271_v2: TX queue type {qtype} not ported")
    hw.txq[q] = TxQueueInfo(tqi_type=qtype, tqi_physCompBuf=qinfo.tqi_physCompBuf)
    set_txq_props(hw, q, qinfo)
    return q


def _clear_queue_interrupts(hw: AthHw, q: int) -> None:
    bit = ~(1 << q)
    hw.txok_interrupt_mask &= bit
    hw.txerr_interrupt_mask &= bit
    hw.txdesc_interrupt_mask &= bit
    hw.txeol_interrupt_mask &= bit
    hw.txurn_interrupt_mask &= bit


def _set_txq_interrupts(hw: AthHw) -> None:
    """ath9k_hw_set_txq_interrupts [SRC] mac.c:20 — push the per-queue masks into IMR_S0/1/2."""
    hw.enable_write_buffer()
    hw.write(R.AR_IMR_S0, R.SM(hw.txok_interrupt_mask, R.AR_IMR_S0_QCU_TXOK)
             | R.SM(hw.txdesc_interrupt_mask, R.AR_IMR_S0_QCU_TXDESC))
    hw.write(R.AR_IMR_S1, R.SM(hw.txerr_interrupt_mask, R.AR_IMR_S1_QCU_TXERR)
             | R.SM(hw.txeol_interrupt_mask, R.AR_IMR_S1_QCU_TXEOL))
    hw.imrs2_reg &= ~R.AR_IMR_S2_QCU_TXURN
    hw.imrs2_reg |= hw.txurn_interrupt_mask & R.AR_IMR_S2_QCU_TXURN
    hw.write(R.AR_IMR_S2, hw.imrs2_reg)
    hw.write_flush()


def reset_txqueue(hw: AthHw, q: int) -> None:
    """ath9k_hw_resettxqueue [SRC] mac.c:367 — program one queue's DCU/QCU config and refresh
    its interrupt masks. DATA/CAB/BEACON differ only in the per-type SET_BIT tail."""
    qi = hw.txq[q]
    if qi.tqi_type == R.TXQ_INACTIVE:
        return

    if qi.tqi_cwmin == R.TXQ_USEDEFAULT:
        cwmin = 1
        while cwmin < R.INIT_CWMIN:
            cwmin = (cwmin << 1) | 1
    else:
        cwmin = qi.tqi_cwmin

    hw.enable_write_buffer()
    hw.write(R.AR_DLCL_IFS(q),
             R.SM(cwmin, R.AR_D_LCL_IFS_CWMIN) | R.SM(qi.tqi_cwmax, R.AR_D_LCL_IFS_CWMAX)
             | R.SM(qi.tqi_aifs, R.AR_D_LCL_IFS_AIFS))
    # STA short/long retry come from hw.sta_retry_limit (the driver's DEFAULT_HW_ACK_RETRIES; the
    # kernel default INIT_SSH_RETRY/INIT_SLG_RETRY is the AthHw fallback). Our injects are short
    # un-RTS frames, so this STA-retry count is what bounds their HW ACK-based retransmission.
    hw.write(R.AR_DRETRY_LIMIT(q),
             R.SM(hw.sta_retry_limit, R.AR_D_RETRY_LIMIT_STA_SH)
             | R.SM(hw.sta_retry_limit, R.AR_D_RETRY_LIMIT_STA_LG)
             | R.SM(qi.tqi_shretry, R.AR_D_RETRY_LIMIT_FR_SH))
    hw.write(R.AR_QMISC(q), R.AR_Q_MISC_DCU_EARLY_TERM_REQ)
    hw.write(R.AR_DMISC(q), R.AR_D_MISC_CW_BKOFF_EN | R.AR_D_MISC_FRAG_WAIT_EN | 0x2)
    if qi.tqi_readyTime and qi.tqi_type != R.TXQ_CAB:
        hw.write(R.AR_QRDYTIMECFG(q),
                 R.SM(qi.tqi_readyTime, R.AR_Q_RDYTIMECFG_DURATION) | R.AR_Q_RDYTIMECFG_EN)
    hw.write(R.AR_DCHNTIME(q),
             R.SM(qi.tqi_burstTime, R.AR_D_CHNTIME_DUR)
             | (R.AR_D_CHNTIME_EN if qi.tqi_burstTime else 0))
    hw.write_flush()

    if qi.tqi_type == R.TXQ_BEACON:
        hw.rmw(R.AR_QMISC(q), R.AR_Q_MISC_FSP_DBA_GATED | R.AR_Q_MISC_BEACON_USE
               | R.AR_Q_MISC_CBR_INCR_DIS1, 0)
        hw.rmw(R.AR_DMISC(q),
               (R.AR_D_MISC_ARB_LOCKOUT_CNTRL_GLOBAL << R.AR_D_MISC_ARB_LOCKOUT_CNTRL_S)
               | R.AR_D_MISC_BEACON_USE | R.AR_D_MISC_POST_FR_BKOFF_DIS, 0)
    elif qi.tqi_type == R.TXQ_CAB:
        hw.rmw(R.AR_QMISC(q), R.AR_Q_MISC_FSP_DBA_GATED | R.AR_Q_MISC_CBR_INCR_DIS1
               | R.AR_Q_MISC_CBR_INCR_DIS0, 0)
        value = (qi.tqi_readyTime
                 - (hw.sw_beacon_response_time - hw.dma_beacon_response_time)) * 1024
        hw.enable_write_buffer()
        hw.write(R.AR_QRDYTIMECFG(q), (value & 0xFFFFFFFF) | R.AR_Q_RDYTIMECFG_EN)
        hw.rmw(R.AR_DMISC(q),
               R.AR_D_MISC_ARB_LOCKOUT_CNTRL_GLOBAL << R.AR_D_MISC_ARB_LOCKOUT_CNTRL_S, 0)
        hw.write_flush()

    _clear_queue_interrupts(hw, q)
    if qi.tqi_qflags & R.TXQ_FLAG_TXINT_ENABLE:
        hw.txok_interrupt_mask |= 1 << q
        hw.txerr_interrupt_mask |= 1 << q
    if qi.tqi_qflags & R.TXQ_FLAG_TXDESCINT_ENABLE:
        hw.txdesc_interrupt_mask |= 1 << q
    if qi.tqi_qflags & R.TXQ_FLAG_TXEOLINT_ENABLE:
        hw.txeol_interrupt_mask |= 1 << q
    if qi.tqi_qflags & R.TXQ_FLAG_TXURNINT_ENABLE:
        hw.txurn_interrupt_mask |= 1 << q
    _set_txq_interrupts(hw)


def init_tx_queues(hw: AthHw) -> None:
    """ath9k_init_queues [SRC] htc_drv_init.c:543 — the htc driver's queue allocation: beacon,
    CAB, then one DATA queue per WMM AC (BE, BK, VI, VO -> hwq 1, 0, 2, 3)."""
    _ensure_txq(hw)
    beacon = TxQueueInfo(tqi_aifs=1, tqi_cwmin=0, tqi_cwmax=0)   # ath9k_hw_beaconq_setup
    setup_txqueue(hw, R.TXQ_BEACON, beacon)
    setup_txqueue(hw, R.TXQ_CAB, _htc_data_qinfo(0))            # ath9k_htc_cabq_setup
    for subtype in (1, 0, 2, 3):                                # BE, BK, VI, VO -> ATH_TXQ_AC_*
        setup_txqueue(hw, R.TXQ_DATA, _htc_data_qinfo(subtype))


def _htc_data_qinfo(subtype: int) -> TxQueueInfo:
    """ATH9K_HTC_INIT_TXQ [SRC] htc_drv_txrx.c:30 — the htc data/CAB queue template."""
    return TxQueueInfo(tqi_subtype=subtype, tqi_aifs=R.TXQ_USEDEFAULT,
                       tqi_cwmin=R.TXQ_USEDEFAULT, tqi_cwmax=R.TXQ_USEDEFAULT,
                       tqi_qflags=R.TXQ_FLAG_TXEOLINT_ENABLE | R.TXQ_FLAG_TXDESCINT_ENABLE)


def init_queues(hw: AthHw) -> None:
    """ath9k_hw_init_queues [SRC] hw.c — seed the per-DCU QCU mask then reset every queue."""
    hw.enable_write_buffer()
    for i in range(R.AR_NUM_DCU):
        hw.write(R.AR_DQCUMASK(i), 1 << i)
    hw.write_flush()
    hw.intr_txqs = 0
    for i in range(R.ATH9K_NUM_TX_QUEUES):
        reset_txqueue(hw, i)
