"""RTL8814AU runtime phydm watchdog tick (M3c) — vendor faithful, no-link monitor path.

The 2 s dynamic-check work [SRC rtw_cmd.c:3268 rtw_dynamic_chk_wk_hdl]:

    sreset xmit/linked status poll  [SRC rtl8814a_sreset.c]
    rtw_hal_dm_watchdog -> phydm_watchdog  [SRC phydm.c:2162]

This module ports the members of ``phydm_watchdog`` that touch the chip in the always-monitor
(never-linked) case, in wire order. Only ``cnt_all`` feeds the DIG decision, but the full FA/CCA
counter read set and the BB debug-port read are emitted to match the chip's runtime I/O. The
adaptivity (EDCCA) and env-monitor (NHM/CLM) members re-run every tick — seeding them once at
init and never updating is what freezes the 2.4 GHz thresholds (the dropout this port fixes).

The USB LED blink [SRC rtl8814au_led.c] is a SEPARATE periodic producer (``dm_DynamicUsbTxAgg``
is a no-op on 8814AU); ``led_blink`` reproduces it with a carried ON/OFF phase. The driver itself
need not blink the LED (cosmetic, no RX effect) — it is here so the capture gate accounts for it.

Scope — the no-link path only. wifit3 never associates, so rssi_min stays at the no-link default
and the linked / DFS / TDMA branches do not apply.
"""
from __future__ import annotations

from dataclasses import dataclass

from .bb import _set_reg_masked as _bb32
from .dig import (
    _IGI_MASK, _IGI_MAX, _IGI_MIN, _REG_IGI, _new_igi_by_fa, _reset_fa_cnt,
)
from .rf import set_rf_masked

WATCHDOG_PERIOD_S = 2.0        # kernel dynamic-check cadence

# sreset status polls [SRC rtl8814a_sreset.c rtl8814_sreset_{xmit,linked}_status_check].
_REG_TXDMA_STATUS = 0x0210     # xmit-status poll (REG_TXDMA_STATUS)
_REG_RXDMA_STATUS = 0x0288     # linked-status poll (REG_RXDMA_STATUS)

# phydm_dynamic_nbi_switch_8814a [SRC phydm_rtl8814a.c:104] — rssi_min<=15 (no link) clears
# the NBI notch enable (0x87c BIT13). rfe_type 1 takes this branch.
_REG_NBI = 0x087C
_NBI_NOTCH_EN = 1 << 13

# phydm_fa_cnt_statistics_ac [SRC phydm_dig.c:1433] read set (ODM_REG_*_11AC), in source order.
# Only cnt_all = OFDM-FA (+ CCK-FA on 2.4G) feeds the DIG decision; the rest feed the FA debug
# log, but every read is on the wire so all are issued.
_FA_TYPE = (0x0FCC, 0x0FD0, 0x0FBC, 0x0FC0, 0x0FC4, 0x0FC8)   # OFDM FA TYPE1..6 (32-bit)
_REG_OFDM_FA = 0x0F48          # OFDM FA count (low word)
_REG_CCK_FA = 0x0A5C           # ODM_REG_CCK_FA_11AC (low word)
_FA_CCA_CRC = (0x0F08, 0x0F04, 0x0F14, 0x0F10, 0x0F0C)       # CCK_CCA, CCK/OFDM/HT/VHT CRC32
_REG_BB_RX_PATH = 0x0808       # ODM_REG_BB_RX_PATH_11AC; BIT28 = CCK enabled (2.4G)
_CCK_ENABLE_BIT = 1 << 28

# BB debug-port access [SRC phydm_debug.c phydm_set/get/release_bb_dbg_port + phydm_get_dbg_port_info].
_REG_DBG_CLK = 0x198C          # phydm_bb_dbg_port_clock_en: [2:0] = 7 (en) / 0 (dis)
_REG_DBG_SEL = 0x08FC          # debug-port index select (MASKDWORD)
_REG_DBG_VAL = 0x0FA0          # debug-port value (read)
_REG_DBG_HDR = 0x08F8          # phydm_bb_dbg_port_header_sel: [25:22]
_ADAPTIVITY_DBG_PORT = 0x209   # dm->adaptivity.adaptivity_dbg_port for 8814A (EDCCA flag probe)

# phydm_adaptivity -> phydm_edcca_thre_calc (non-PWDB, NORMAL mode) [SRC phydm_adaptivity.c:513]:
#   th_l2h = max(igi + TH_L2H_DIFF_IGI, EDCCA_TH_L2H_LB); th_h2l = th_l2h - EDCCA_HL_DIFF_NORMAL
# written byte0=L2H / byte1=H2L into 0x8a4 [SRC phydm_set_edcca_threshold, 11AC]. 8814A is not a
# PWDB-EDCCA IC and runs the no-link NORMAL branch, so the thresholds track the carried IGI.
_REG_EDCCA = 0x08A4
_TH_L2H_DIFF_IGI = 8
_EDCCA_TH_L2H_LB = 48
_EDCCA_HL_DIFF_NORMAL = 8

# phydm_env_mntr_watchdog -> NHM + CLM (CCX) [SRC phydm_ccx.c:1989]. A stateful measurement
# engine: each tick reads the previous NHM/CLM report (when the HW ready bit is set), re-arms the
# measurement, and (when the IGI moved) re-derives the NHM thresholds. It is the heaviest writer
# on the wire but pure telemetry — its results are not consumed by the no-link DIG/adaptivity RX
# path. CCA_CAP/IGI_2_NHM_TH match phydm; thresholds reuse the init formula with the carried IGI.
_REG_CCX = 0x0994              # NHM/CLM control: [0]=CLM trig, [1]=NHM trig, [11:8]=incl, [31:16]=th9/10
_REG_NHM_PERIOD = 0x0990      # [31:16]=NHM period, [15:0]=CLM period
_REG_NHM_TH_3_0 = 0x0998      # NHM th[3..0] (full DWORD)
_REG_NHM_TH_7_4 = 0x099C      # NHM th[7..4] (full DWORD)
_REG_NHM_TH_8 = 0x09A0        # [7:0] = NHM th[8]
_REG_NHM_RDY = 0x0FB4         # [16] = NHM report ready; [15:0] = duration
_NHM_RPT = (0x0FA8, 0x0FAC, 0x0FB0, 0x0FB4)   # NHM report words read when ready (+ duration)
_REG_CLM_RDY = 0x0FA4         # [16] = CLM report ready; [15:0] = result
_CCA_CAP = 14
_NHM_PERIOD_MAX = 0xFFFE      # NHM_PERIOD_MAX (mntr_time >= 262 ms)
_CLM_PERIOD_MAX = 0xFFFF      # CLM_PERIOD_MAX
_NHM_INCL_FIELD = 0x1         # BIT_2_BYTE(CNT_ALL=0, EXCLUDE_TXON=0, EXCLUDE_CCA=0, en=1) -> 0x994[11:8]

# LED blink [SRC rtl8814au_led.c SwLedOn/Off_8814AU], LED_PIN_LED0 over REG_GPIO_PIN_CTRL_2 (0x60).
_REG_LED = 0x0060
_LED_GPO_CFG = (1 << 16) | (1 << 17) | (1 << 21) | (1 << 22)   # config pins as GPO
_LED_GPO_VAL = (1 << 8) | (1 << 9) | (1 << 13) | (1 << 14)     # gpo output value (set=off)
_LED_GPI_VAL = (1 << 0) | (1 << 1) | (1 << 5) | (1 << 6)       # gpi value (cleared on)


# halrf_watchdog -> phydm_rf_watchdog -> odm_txpowertracking_check [SRC halphyrf_ce.c:1151]:
# the thermal-meter read of RF reg 0x42 (R 0x2908 path-A readback) paired with a re-trigger
# write (W 0xc90); halrf_dpk_track is a no-op on 8814A. TX-thermal compensation, RX-irrelevant,
# but on the wire each tick. The first call runs the txpwrtrack init (a no-change RMW of 0x440).
_REG_THERMAL_RF = 0x42         # RF_T_METER; read via the path-A RF-readback window (0x2908)
_THERMAL_TRIGGER = 0x30000     # RF 0x42[17:16] = 3 re-arms the meter for the next measurement
_REG_TXPWRTRACK_INIT = 0x0440  # BB reg touched once on the first txpwrtrack init (no change)


# phydm_cck_pd_th + phydm_cckpd_type1 [SRC phydm_cck_pd.c:1019/78] — CCK packet-detect
# threshold adaptation (the 8814A is CCK_PD_IC_TYPE1, ODM_RTL8814A in phydm_cck_pd.h:43).
# Each tick folds the CCK false-alarm count into a moving average and, in the no-link
# (always-monitor) case, raises the CCK-PD threshold to LV_1 when the channel is noisy
# (cck_fa_ma > 1000) so the over-sensitive LV_0 detector is not swamped by CCK false
# alarms (which makes it miss real strong CCK beacons), dropping back to LV_0 when quiet
# (< 500); the 500..1000 band holds (hysteresis). 0xa0a is written only on a level change.
_REG_CCK_PD = 0x0A0A
_CCK_FA_MA_RESET = 0xFFFFFFFF
_CCK_PD_LV0, _CCK_PD_LV1 = 0, 1
_CCK_PD_TH = {_CCK_PD_LV0: 0x40, _CCK_PD_LV1: 0x83}   # phydm_set_cckpd_lv_type1 LV_0/LV_1


@dataclass
class WatchdogState:
    """State carried across watchdog ticks (the parts the chip does not re-encode in a read)."""
    cur_ig_value: int = _IGI_MIN   # phydm_dig cur_ig_value; SEED from InitHalDm's _dig_init read
    led_on: bool = True            # SwLed starts ON; the blink strictly alternates each fire
    txpwrtrack_init: bool = False  # odm_txpowertracking_check is_txpowertracking_init first-run
    # phydm_cckpd: CCK-PD moving average + current level. InitHalDm commits LV_0 (0xa0a=0x40)
    # and leaves the MA reset, so the carried state starts there.
    cck_fa_ma: int = _CCK_FA_MA_RESET
    cck_pd_lv: int = _CCK_PD_LV0
    # CCX (env_mntr) carried state. phydm_nhm_init leaves nhm_igi at the seed IGI, the include
    # fields at *_INIT (≠ the watchdog's params, so they re-set once), nhm_period 0, clm_period
    # 0xffff. SEED nhm_igi from InitHalDm alongside cur_ig_value.
    nhm_igi: int = 0xFF
    nhm_include_set: bool = False
    nhm_period: int = 0
    clm_period: int = _CLM_PERIOD_MAX


def led_blink(t, st: WatchdogState) -> None:
    """[SRC] SwLedOn_8814AU / SwLedOff_8814AU — toggle LED0 on REG_GPIO_PIN_CTRL_2.

    A separate periodic producer from the dynamic-check tick. Strict ON/OFF alternation; the
    write is a deterministic function of the read (the GPIO config/value bits), with the on/off
    intent carried in ``st.led_on``.
    """
    cfg = t.read32(_REG_LED) | _LED_GPO_CFG
    if st.led_on:
        cfg &= ~_LED_GPO_VAL        # ON = clear gpo output value...
        cfg &= ~_LED_GPI_VAL        # ...and the gpi value
    else:
        cfg |= _LED_GPO_VAL         # OFF = set gpo output value
    t.write32(_REG_LED, cfg)
    st.led_on = not st.led_on


def _sreset_status_check(t) -> None:
    """[SRC] rtl8814_sreset_{xmit,linked}_status_check — poll TX/RX DMA status (no-op if 0)."""
    t.read32(_REG_TXDMA_STATUS)
    t.read32(_REG_RXDMA_STATUS)


def _hw_setting_nbi(t) -> None:
    """[SRC] phydm_hwsetting_8814a -> phydm_dynamic_nbi_switch_8814a (rfe_type 1).

    No-link rssi_min (<=15) clears the NBI notch enable; a no-change RMW on the wire."""
    _bb32(t, _REG_NBI, _NBI_NOTCH_EN, 0)


def _fa_cnt_statistics(t) -> tuple[int, int]:
    """[SRC] phydm_fa_cnt_statistics_ac — read the full FA/CCA/CRC32 counter set.

    Returns ``(cnt_all, cck_fa)``; cnt_all = OFDM-FA (+ CCK-FA when CCK is enabled, i.e. 2.4G),
    which is all the DIG decision consumes. cck_fa feeds CCK-PD's moving average.
    """
    for reg in _FA_TYPE:
        t.read32(reg)                      # OFDM FA TYPE1..6 (debug detail)
    ofdm_fa = t.read32(_REG_OFDM_FA) & 0xFFFF   # odm_get_bb_reg(MASKLWORD): 32-bit read, low word
    cck_fa = t.read32(_REG_CCK_FA) & 0xFFFF
    for reg in _FA_CCA_CRC:
        t.read32(reg)                      # CCK_CCA + the four CRC32 counters (debug detail)
    cck_enabled = bool(t.read32(_REG_BB_RX_PATH) & _CCK_ENABLE_BIT)
    cnt_all = ofdm_fa + cck_fa if cck_enabled else ofdm_fa
    return cnt_all, cck_fa


def _get_dbg_port_info(t) -> None:
    """[SRC] phydm_get_dbg_port_info — read debug port 0x0 then the adaptivity EDCCA port.

    Each read brackets a clock-enable/select/value/disable/header-reset cycle [SRC
    phydm_set/get/release_bb_dbg_port]; the values feed the FA debug log + edcca_flag only.
    """
    for port in (0x0, _ADAPTIVITY_DBG_PORT):
        _bb32(t, _REG_DBG_CLK, 0x7, 0x7)               # clock_en(true)
        t.write32(_REG_DBG_SEL, port)                  # set debug-port index
        t.read32(_REG_DBG_VAL)                         # get value
        _bb32(t, _REG_DBG_CLK, 0x7, 0x0)               # clock_en(false)
        _bb32(t, _REG_DBG_HDR, 0x03C00000, 0x0)        # header_sel(0)


def _dig(t, st: WatchdogState, cnt_all: int) -> None:
    """[SRC] phydm_dig + odm_write_dig — step the CARRIED IGI by FA, clamp to the no-link range,
    and write all four paths only when it changes.

    phydm_dig uses ``dig_t->cur_ig_value`` (software state), not a chip read, for the decision;
    odm_write_dig RMWs each path's IGI byte and updates cur_ig_value only on a change. So a tick
    with a steady IGI emits NO 0xc50 ops — matching the wire (IGI changes 22× over the capture).
    """
    new_igi = max(_IGI_MIN, min(_IGI_MAX, _new_igi_by_fa(st.cur_ig_value, cnt_all)))
    if new_igi != st.cur_ig_value:
        for reg in _REG_IGI:
            _bb32(t, reg, _IGI_MASK, new_igi)      # odm_write_dig per-path RMW
        st.cur_ig_value = new_igi


def _cck_pd(t, st: WatchdogState, cck_fa: int) -> None:
    """[SRC] phydm_cck_pd_th + phydm_cckpd_type1 (no-link) — adapt the CCK-PD threshold.

    Fold this tick's CCK false-alarm count into the moving average, then in the no-link
    (always-monitor) case raise CCK-PD to LV_1 (0xa0a=0x83) when the channel is noisy
    (cck_fa_ma > 1000) and drop to LV_0 (0x40) when quiet (< 500); 500..1000 holds the
    current level. 0xa0a is written only on a level change, which also resets the MA.
    Without this, CCK-PD is stuck at the over-sensitive LV_0 seed and a busy 2.4 GHz
    channel swamps the CCK detector with false alarms — it then misses real CCK beacons
    (e.g. a 1 Mbps-beaconing AP), the dominant 2.4 GHz RX deficit. [HW] raising LV_0->LV_1
    ~doubled reference-AP CCK reception.
    """
    if st.cck_fa_ma == _CCK_FA_MA_RESET:
        st.cck_fa_ma = cck_fa
    else:
        st.cck_fa_ma = (st.cck_fa_ma * 3 + cck_fa) >> 2
    if st.cck_fa_ma > 1000:
        lv = _CCK_PD_LV1
    elif st.cck_fa_ma < 500:
        lv = _CCK_PD_LV0
    else:
        return                                 # hysteresis band: keep the current level
    if lv != st.cck_pd_lv:
        t.write8(_REG_CCK_PD, _CCK_PD_TH[lv])
        st.cck_pd_lv = lv
        st.cck_fa_ma = _CCK_FA_MA_RESET        # phydm_set_cckpd_lv_type1 resets the MA


def _adaptivity(t, st: WatchdogState) -> None:
    """[SRC] phydm_adaptivity -> phydm_edcca_thre_calc (non-PWDB, NORMAL) — re-derive the EDCCA
    L2H/H2L thresholds from the carried IGI and write them every tick.

    Seeding these once at init (and never updating) freezes the EDCCA gate — one half of the
    2.4 GHz dropout. Reproduced here so the runtime watchdog tracks the IGI like the vendor.
    """
    igi = st.cur_ig_value
    th_l2h = max(igi + _TH_L2H_DIFF_IGI, _EDCCA_TH_L2H_LB)
    th_h2l = th_l2h - _EDCCA_HL_DIFF_NORMAL
    _bb32(t, _REG_EDCCA, 0x00FF, th_l2h & 0xFF)    # MASKBYTE0 = L2H
    _bb32(t, _REG_EDCCA, 0xFF00, th_h2l & 0xFF)    # MASKBYTE1 = H2L


def _halrf(t, st: WatchdogState) -> None:
    """[SRC] halrf_watchdog -> odm_txpowertracking_check — read + re-arm the RF thermal meter.

    On the first call the txpwrtrack init touches a BB reg once (no change here); every call
    reads RF 0x42 (the path-A readback at 0x2908) and re-arms it (RF 0x42[17:16]=3 -> 0xc90).

    SCOPE: this reproduces only the no-thermal-delta path. When the averaged thermal value
    diverges from the EFUSE base, odm_txpowertracking_callback_thermal_meter applies a per-path
    TX-power swing/OFDM-index correction (0xc94/0xc1c/... per path) via the tracking-table LUT.
    That correction — TX-power thermal compensation, RX-irrelevant — is NOT ported; it is the
    capture gate's remaining frontier (first delta tick). The runtime watchdog only needs the
    re-arm, so the driver's RX behaviour is unaffected by the omission.
    """
    if not st.txpwrtrack_init:
        v = t.read32(_REG_TXPWRTRACK_INIT)
        t.write32(_REG_TXPWRTRACK_INIT, v)             # first-run init: no-change RMW
        st.txpwrtrack_init = True
    set_rf_masked(t, "a", _REG_THERMAL_RF, _THERMAL_TRIGGER, 0x3)


def _set_nhm_th(t, igi: int) -> None:
    """[SRC] phydm_nhm_set_th_reg — write the 11 IGI-derived NHM thresholds.

    nhm_th[0] = (igi - CCA_CAP) << 1; nhm_th[i] = nhm_th[0] + ((2*i) << 1). 0x998/0x99c are full
    DWORD writes; 0x9a0[7:0]=th[8] and 0x994[31:16]=th[10]<<8|th[9] are masked.
    """
    th0 = ((igi - _CCA_CAP) << 1) & 0xFF
    th = [th0] + [(th0 + ((2 * i) << 1)) & 0xFF for i in range(1, 11)]
    t.write32(_REG_NHM_TH_3_0, th[0] | th[1] << 8 | th[2] << 16 | th[3] << 24)
    t.write32(_REG_NHM_TH_7_4, th[4] | th[5] << 8 | th[6] << 16 | th[7] << 24)
    _bb32(t, _REG_NHM_TH_8, 0xFF, th[8])
    _bb32(t, _REG_CCX, 0xFFFF0000, th[9] | th[10] << 8)


def _nhm_mntr_chk(t, st: WatchdogState) -> None:
    """[SRC] phydm_nhm_mntr_chk(262) — get the prior NHM report then re-arm (background)."""
    _bb32(t, _REG_CCX, 1 << 1, 0)                       # nhm_get_result: clear trigger bit
    if t.read32(_REG_NHM_RDY) & (1 << 16):              # NHM report ready -> read it
        for reg in _NHM_RPT:
            t.read32(reg)
    # phydm_nhm_set(EXCLUDE_TXON, EXCLUDE_CCA, CNT_ALL, BACKGROUND, NHM_PERIOD_MAX):
    if not st.nhm_include_set:                          # include/cca/divider differ from *_INIT once
        _bb32(t, _REG_CCX, 0xF00, _NHM_INCL_FIELD)
        st.nhm_include_set = True
    if st.nhm_period != _NHM_PERIOD_MAX:
        _bb32(t, _REG_NHM_PERIOD, 0xFFFF0000, _NHM_PERIOD_MAX)
        st.nhm_period = _NHM_PERIOD_MAX
    igi_curr = t.read32(_REG_IGI[0]) & _IGI_MASK        # phydm_get_igi (th_update_chk reads it)
    if igi_curr != st.nhm_igi:
        _set_nhm_th(t, igi_curr)
        st.nhm_igi = igi_curr


def _clm_mntr_chk(t, st: WatchdogState) -> None:
    """[SRC] phydm_clm_mntr_chk(262) — get the prior CLM report then re-arm (background)."""
    _bb32(t, _REG_CCX, 1 << 0, 0)                       # clm_get_result: clear trigger bit
    if t.read32(_REG_CLM_RDY) & (1 << 16):              # CLM report ready -> read the result
        t.read32(_REG_CLM_RDY)
    if st.clm_period != _CLM_PERIOD_MAX:               # clm_setting (period unchanged after init)
        _bb32(t, _REG_NHM_PERIOD, 0xFFFF, _CLM_PERIOD_MAX)
        st.clm_period = _CLM_PERIOD_MAX


def _env_mntr(t, st: WatchdogState) -> None:
    """[SRC] phydm_env_mntr_watchdog — NHM + CLM check, then trigger each. Telemetry only."""
    _nhm_mntr_chk(t, st)
    _clm_mntr_chk(t, st)
    _bb32(t, _REG_CCX, 1 << 1, 0)                       # phydm_nhm_trigger: clear then set BIT1
    _bb32(t, _REG_CCX, 1 << 1, 1)
    _bb32(t, _REG_CCX, 1 << 0, 0)                       # phydm_clm_trigger: clear then set BIT0
    _bb32(t, _REG_CCX, 1 << 0, 1)


def tick(t, st: WatchdogState) -> int:
    """One dynamic-check tick: sreset poll + the phydm_watchdog members, in wire order.

    Mutates ``st`` (carried IGI / CCX state) and returns ``cnt_all`` (the FA count this tick)
    for the driver's debug log. The DIG IGI after the tick is ``st.cur_ig_value``.
    """
    _sreset_status_check(t)
    _hw_setting_nbi(t)
    cnt_all, cck_fa = _fa_cnt_statistics(t)
    _get_dbg_port_info(t)
    _reset_fa_cnt(t)
    _dig(t, st, cnt_all)
    _cck_pd(t, st, cck_fa)
    _adaptivity(t, st)
    _halrf(t, st)
    _env_mntr(t, st)
    return cnt_all
