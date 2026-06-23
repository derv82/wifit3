"""RTL8821CU phydm dynamic-check watchdog tick — the third async producer of the operational phase.

The ~2 s dynamic-check work [SRC] core/rtw_cmd.c:2992 rtw_dynamic_chk_wk_hdl:
  sreset xmit/linked status checks (DBG_CONFIG_ERROR_DETECT) [SRC] rtl8821c_ops.c:574/672
  dm_DynamicUsbTxAgg -> the 8821CU rx-agg reconfig [SRC] hal_com.c:14891 -> rtl8821cu_ops.c:47
                       -> cfg_usb_rx_agg_88xx [SRC] halmac_usb_88xx.c:88
  rtw_hal_dm_watchdog -> phydm_watchdog [SRC] phydm.c:2382

Only the members that touch the wire for the CE + CONFIG_RTL8821C build in monitor / no-link mode
are ported, in wire order. The silent / #if'd-out members are catalogued in RTL8821CU_DKMS.md. The
tick interleaves with the channel hops + LED blink in the operational phase and is dispatched by its
unique opener (read REG_TXDMA_STATUS 0x0210).

A note the gate forced out: the IGI lives at 0xC50 (read 0x20 at init); 0x09A4 is the OFDM-FA reset
control (its low byte is unrelated to IGI). So DIG, with cur_ig_value = 0x20 and the no-link FA in
the hold band, computes no change and writes nothing — matching the wire.
"""
from __future__ import annotations

from dataclasses import dataclass

from .bb import set_bb_reg
from .dm import _get_bb_dbg_port_val, _release_bb_dbg_port, _set_bb_dbg_port
from .rf import read_rf

# --- sreset status checks [SRC] rtl8821c_ops.c -----------------------------
_REG_TXDMA_STATUS = 0x0210      # xmit_status_check :583 (==0 -> no reset)
_REG_RXDMA_STATUS = 0x0288      # linked_status_check :679
_REG_RXFF_PTR_V1 = 0x1118       # check_rx_count :644 (16-bit)

# --- USB rx-agg reconfig [SRC] halmac_usb_88xx.c:88 cfg_usb_rx_agg_88xx ------
_REG_TXDMA_PQ_MAP = 0x010C      # agg_enable byte
_REG_RXDMA_AGG_PG_TH = 0x0280   # [3]=dma_usb_agg ; word = size | timeout<<8
_BIT_EN_PRE_CALC = 1 << 29      # BIT_EN_PRE_CALC (size_limit_en) [SRC] halmac_bit_8821c.h:6346
_BIT_RXDMA_AGG_EN = 1 << 2      # USB-mode agg enable on 0x10c
_BIT_DMA_USB_AGG = 1 << 7       # USB-mode bit on 0x283
# low-traffic (monitor) agg params: size 0, timeout 1 [SRC] rtl8821cu_ops.c:49-52
_RXAGG_SIZE, _RXAGG_TIMEOUT = 0x00, 0x01

_REG_RCR = 0x0608               # hw_var_rcr_get read interleaved into the tick (not watchdog state)

# --- phydm_false_alarm_counter_statistics (AC) [SRC] phydm_dig.c:1726 --------
# OFDM-FA TYPE1..6, OFDM-FA count, CCK-FA count, CCA + CRC32 counters — read in this order.
_FA_READ_SEQ = (0x0FCC, 0x0FD0, 0x0FBC, 0x0FC0, 0x0FC4, 0x0FC8, 0x0F48, 0x0A5C,
                0x0F08, 0x0F04, 0x0F14, 0x0F1C, 0x0F10, 0x0F18, 0x0F0C, 0x0F54)
_REG_OFDM_FA = 0x0F48           # cnt_ofdm_fail = [15:0]
_REG_CCK_FA = 0x0A5C            # cnt_cck_fail = [15:0]  (ODM_REG_CCK_FA_11AC)
_REG_BB_RX_PATH = 0x0808        # cck_enable = BIT28
_CCK_ENABLE = 1 << 28
_ADAPTIVITY_DBG_PORT = 0x209    # the EDCCA-flag dbg port read after dbg port 0x0

# --- FA-counter reset [SRC] phydm_dig.c:1572 + phydm_api.c:69 ----------------
_REG_OFDM_FA_RST = 0x09A4       # BIT17 set->clear resets OFDM FA
_REG_CCK_FA_RST = 0x0A2C        # BIT15 set resets CCK FA
_REG_BB_HW_CNT_RST = 0x0B58     # BIT0 set->clear resets the page-F counters
# crc32-cnt2-rate (first tick only; *_rate_idx start 0) [SRC] phydm_dig.c:1902
_REG_CRC32_CNT2_RATE = 0x0B04
_SPEC_RATE_6M = 0xB             # PHYDM_SPEC_RATE_6M [SRC] phydm_pre_define.h:316

# --- DIG [SRC] phydm_dig.c:1336 / :1205 -------------------------------------
_REG_IGI_A = 0x0C50             # ODM_REG_IGI_A_11AC ([6:0] = IGI)
_DIG_MIN, _DIG_MAX_OF_MIN = 0x1C, 0x2A      # no-link rx_gain_range_min / max
_FA_TH = (2000, 4000, 5000)                 # no-link non-DFS FA thresholds [SRC] :226-230

# --- CCK-PD type2 [SRC] phydm_cck_pd.c:319 / :156 ---------------------------
_REG_CCK_N_RX = 0x0A2C          # BIT22 = 2nd RX path present
_REG_CCK_PD_TH = 0x0A08         # [21:16] = pd_th
_REG_CCK_CS_RATIO = 0x0AA8      # [20:16] = cs_ratio
_CCK_PD_LV0, _CCK_PD_LV1 = 0, 1
_CCK_FA_MA_RESET = 0xFFFFFFFF

# --- adaptivity EDCCA [SRC] phydm_adaptivity.c:203 (NORMAL mode) -------------
_REG_EDCCA = 0x08A4             # byte0 = L2H, byte1 = H2L
_TH_L2H_DIFF_IGI, _EDCCA_TH_L2H_LB, _EDCCA_HL_DIFF_NORMAL = 8, 48, 8

_REG_RRSR = 0x0440              # rtw_phydm_set_rrsr re-set (interleaved); rrsr_val_init 0x15d
_RRSR_VAL_INIT = 0x15D

# --- halrf thermal (tx-power tracking) [SRC] halrf_powertracking_ce.c:818 ----
_REG_RF_T_METER = 0x42          # RF reg 0x42; phase-1 sets [17:16]=3 to arm the meter
_RF_T_METER_TRIG = 0x3

# --- dyn-bw indication (bw-fixed) [SRC] phydm_api.c:800 ----------------------
_REG_BW_FIXED = 0x0840          # [3:0]=pri-ch (20 MHz->0) ; BIT4 = bw-fixed enable

# --- env-monitor NHM/CLM/FAHM [SRC] phydm_ccx.c:2338 phydm_env_mntr_watchdog -
_REG_CCX_CTRL = 0x0994          # [0]=CLM trig [1]=NHM trig [2]=FAHM trig [11:8]=NHM incl [31:16]=th9/10
_REG_CCX_PERIOD = 0x0990        # [31:16]=NHM period [15:0]=CLM period
_REG_NHM_RDY = 0x0FB4           # NHM ready BIT16 (+ duration [15:0])
_REG_NHM_RPT = (0x0FA8, 0x0FAC, 0x0FB0)     # NHM report words (read when ready)
_REG_CLM_RDY = 0x0FA4           # CLM ready BIT16 (+ result [15:0])
_REG_FAHM_RDY = 0x1F98          # FAHM ready BIT31 (+ denom [15:0])
_REG_FAHM_RPT = 0x1F80          # FAHM report base (6 dwords)
_REG_FAHM_CTRL = 0x1CF8         # FAHM period [23:8]
_NHM_PERIOD_MAX, _CLM_PERIOD_MAX = 0xFFFE, 0xFFFF        # [SRC] phydm_ccx.h:43/45


@dataclass
class WatchdogState:
    """phydm dynamic-mechanism state carried across ticks, seeded from the init `odm_dm_init`
    (so the first tick starts from the right values). The replay supplies the chip read-backs;
    these are the pure-software carried fields the chip does not re-encode in a read."""
    cur_ig_value: int = 0x20            # DIG IGI, seeded from _dig_init's 0xC50 read
    cck_pd_lv: int = _CCK_PD_LV1        # _cck_pd_init commits LV_1
    cck_fa_ma: int = _CCK_FA_MA_RESET   # CCK-FA moving average (reset)
    aaa_default: int = 0x0F             # _cck_pd_init: 0xAAA[4:0]
    first_tick: bool = True             # crc32-cnt2-rate writes fire only while *_rate_idx==0
    tm_trigger: bool = False            # halrf: init 0 -> first tick arms the thermal meter
    # env-monitor carried state (seeded from phydm_env_monitor_init); IGI 0x20 so the th curves
    # already match -> th writes suppressed. period/include fire on the first tick then suppress.
    nhm_igi: int = 0x20
    fahm_igi: int = 0x20
    nhm_period: int = 0                 # init leaves NHM/FAHM period 0; CLM period 0xffff
    fahm_period: int = 0
    clm_period: int = _CLM_PERIOD_MAX
    nhm_include_set: bool = False       # include fields at *_INIT sentinel until the first set
    fahm_include_set: bool = False


# ======================================================================
# member helpers (wire order)
# ======================================================================
def _sreset_check(t) -> None:
    """rtw_hal_sreset_xmit/linked_status_check [SRC] rtl8821c_ops.c:574/672 — read the TX/RX DMA
    status (0 -> no hang -> no reset) and the RX-FIFO pointer (check_rx_count)."""
    t.read32(_REG_TXDMA_STATUS)
    t.read32(_REG_RXDMA_STATUS)
    t.read16(_REG_RXFF_PTR_V1)


def _usb_rx_agg(t) -> None:
    """cfg_usb_rx_agg_88xx (USB mode, low-traffic) [SRC] halmac_usb_88xx.c:88 — re-apply the RX-DMA
    aggregation: pre-calc enable on 0x280, USB-mode bits on 0x10c/0x283, then the size/timeout word
    (size 0, timeout 1 in monitor)."""
    dma_usb_agg = t.read8(_REG_RXDMA_AGG_PG_TH + 3)
    agg_enable = t.read8(_REG_TXDMA_PQ_MAP)
    value32 = t.read32(_REG_RXDMA_AGG_PG_TH)
    t.write32(_REG_RXDMA_AGG_PG_TH, value32 | _BIT_EN_PRE_CALC)
    t.write8(_REG_TXDMA_PQ_MAP, agg_enable | _BIT_RXDMA_AGG_EN)
    t.write8(_REG_RXDMA_AGG_PG_TH + 3, dma_usb_agg & ~_BIT_DMA_USB_AGG)
    t.write16(_REG_RXDMA_AGG_PG_TH, _RXAGG_SIZE | (_RXAGG_TIMEOUT << 8))


def _fa_cnt_statistics(t, st: WatchdogState) -> tuple[int, int]:
    """phydm_false_alarm_counter_statistics (AC) [SRC] phydm_dig.c:1940 — read the FA/CCA/CRC32
    counters, the two diagnostic dbg ports, reset the counters, and (first tick) set the crc32-cnt2
    spec-rate. Returns (cnt_all, cnt_cck_fail) for the DIG and CCK-PD decisions."""
    vals = {addr: t.read32(addr) for addr in _FA_READ_SEQ}
    cnt_ofdm_fail = vals[_REG_OFDM_FA] & 0xFFFF
    cnt_cck_fail = vals[_REG_CCK_FA] & 0xFFFF
    cck_enable = bool(t.read32(_REG_BB_RX_PATH) & _CCK_ENABLE)
    cnt_all = cnt_ofdm_fail + (cnt_cck_fail if cck_enable else 0)

    # phydm_get_dbg_port_info: dbg port 0x0 (dbg_port0) then 0x209 (EDCCA flag) — diagnostic reads.
    for port in (0x0, _ADAPTIVITY_DBG_PORT):
        _set_bb_dbg_port(t, port)
        _get_bb_dbg_port_val(t)
        _release_bb_dbg_port(t)

    # phydm_false_alarm_counter_reg_reset (11AC): OFDM FA (0x9a4[17]), CCK FA (0xa2c[15]), page-F.
    set_bb_reg(t, _REG_OFDM_FA_RST, 1 << 17, 1)
    set_bb_reg(t, _REG_OFDM_FA_RST, 1 << 17, 0)
    set_bb_reg(t, _REG_CCK_FA_RST, 1 << 15, 0)
    set_bb_reg(t, _REG_CCK_FA_RST, 1 << 15, 1)
    set_bb_reg(t, _REG_BB_HW_CNT_RST, 1 << 0, 1)
    set_bb_reg(t, _REG_BB_HW_CNT_RST, 1 << 0, 0)

    if st.first_tick:
        # phydm_set_crc32_cnt2_rate: ofdm2=6M (0xb), ht2/vht2 idx 0 (the rest write back unchanged).
        set_bb_reg(t, _REG_CRC32_CNT2_RATE, 0x0000F000, _SPEC_RATE_6M)
        set_bb_reg(t, _REG_CRC32_CNT2_RATE, 0x007F0000, 0x0)
        set_bb_reg(t, _REG_CRC32_CNT2_RATE, 0x0F000000, 0x0)
        set_bb_reg(t, _REG_CRC32_CNT2_RATE, 0x30000000, 0x0)
    return cnt_all, cnt_cck_fail


def _dig(t, st: WatchdogState, cnt_all: int) -> None:
    """phydm_dig [SRC] phydm_dig.c:1336 — no-link new-IGI from the FA count, clamped to
    [0x1c, 0x2a]; odm_write_dig only writes 0xC50 on a change. At monitor idle the FA count holds
    in the [2000, 4000] band so IGI is unchanged (no wire), but the decision still runs."""
    igi = st.cur_ig_value
    if cnt_all > _FA_TH[2]:
        igi += 2
    elif cnt_all > _FA_TH[1]:
        igi += 1
    elif cnt_all < _FA_TH[0]:
        igi -= 2
    new_igi = max(_DIG_MIN, min(igi, _DIG_MAX_OF_MIN))
    if new_igi != st.cur_ig_value:
        set_bb_reg(t, _REG_IGI_A, 0x7F, new_igi)
        st.cur_ig_value = new_igi


def _cck_pd_th(t, st: WatchdogState, cnt_cck_fail: int) -> None:
    """phydm_cck_pd_th -> type2 [SRC] phydm_cck_pd.c:319 — fold the CCK-FA count into a moving
    average and pick the PD level (no-link: LV_1 if >1000, LV_0 if <500, else hold). On a level
    change write pd_th (0xa08[21:16]) + cs_ratio (0xaa8[20:16]); cs_ratio uses aaa_default."""
    if st.cck_fa_ma == _CCK_FA_MA_RESET:
        st.cck_fa_ma = cnt_cck_fail
    else:
        st.cck_fa_ma = (st.cck_fa_ma * 3 + cnt_cck_fail) >> 2
    if st.cck_fa_ma > 1000:
        lv = _CCK_PD_LV1
    elif st.cck_fa_ma < 500:
        lv = _CCK_PD_LV0
    else:
        return
    if lv == st.cck_pd_lv:
        return
    t.read32(_REG_CCK_N_RX)                              # n_rx (BIT22) — 1T1R here
    pd_th = 0x7 if lv == _CCK_PD_LV1 else 0x3
    cs_ratio = (st.aaa_default + 2) if lv == _CCK_PD_LV1 else st.aaa_default
    set_bb_reg(t, _REG_CCK_PD_TH, 0x3F0000, pd_th)
    set_bb_reg(t, _REG_CCK_CS_RATIO, 0x1F0000, cs_ratio)
    st.cck_pd_lv = lv


def _adaptivity(t, st: WatchdogState) -> None:
    """phydm_adaptivity -> phydm_set_edcca_threshold (NORMAL) [SRC] phydm_adaptivity.c:203 —
    L2H = max(igi + 8, 48), H2L = L2H - 8, into 0x8a4 byte0/byte1."""
    th_l2h = max(st.cur_ig_value + _TH_L2H_DIFF_IGI, _EDCCA_TH_L2H_LB)
    th_h2l = th_l2h - _EDCCA_HL_DIFF_NORMAL
    set_bb_reg(t, _REG_EDCCA, 0x000000FF, th_l2h)
    set_bb_reg(t, _REG_EDCCA, 0x0000FF00, th_h2l)


def _rrsr_reset(t) -> None:
    """rtw_phydm_set_rrsr [SRC] hal_dm.c:1812 (interleaved into the tick, not a watchdog member):
    re-apply the RRSR init value 0x15d (masked RMW -> unchanged)."""
    cur = t.read32(_REG_RRSR)
    t.write32(_REG_RRSR, (cur & ~0xFFFFF) | _RRSR_VAL_INIT)


def _halrf_thermal(t, st: WatchdogState) -> None:
    """halrf_watchdog -> odm_txpowertracking_check [SRC] halrf_powertracking_ce.c:818 — the thermal
    meter is a 2-phase trigger: the first tick (tm_trigger=0) arms it by RF 0x42[17:16]=3 (a masked
    RF write = read-back at 0x2908 + LSSI write at 0xc90), then flips the flag."""
    if not st.tm_trigger:
        cur = read_rf(t, _REG_RF_T_METER)               # 0x2800 + (0x42<<2) = 0x2908
        data = (cur & ~(0x3 << 16)) | (_RF_T_METER_TRIG << 16)
        t.write32(0x0C90, (_REG_RF_T_METER << 20) | (data & 0xFFFFF))
        st.tm_trigger = True


def _dyn_bw_indication(t) -> None:
    """phydm_dyn_bw_indication -> phydm_bw_fixed_setting [SRC] phydm_api.c:800 — no-link bw-fixed:
    pri-ch field 0 (20 MHz) then bw-fixed enable (both already committed at init -> identity)."""
    set_bb_reg(t, _REG_BW_FIXED, 0xF, 0x0)
    set_bb_reg(t, _REG_BW_FIXED, 1 << 4, 0x1)


def _ccx_get_result(t, st: WatchdogState, trig_bit: int, rdy_reg: int,
                    rpt_regs: tuple[int, ...]) -> None:
    """phydm_{nhm,clm}_get_result [SRC] phydm_ccx.c:993/1960 — clear the trigger bit, poll the
    ready flag (BIT16), and read the report words only when ready (no-link monitor: not ready, so
    only the clear RMW + the ready poll hit the wire)."""
    set_bb_reg(t, _REG_CCX_CTRL, trig_bit, 0)
    if t.read32(rdy_reg) & (1 << 16):
        for r in rpt_regs:
            t.read32(r)
        t.read32(rdy_reg)                               # duration/result re-read when ready


def _ccx_trigger(t, trig_bit: int) -> None:
    """phydm_{nhm,clm,fahm}_trigger [SRC] phydm_ccx.c:820/1884/98 — clear then set the trigger bit."""
    set_bb_reg(t, _REG_CCX_CTRL, trig_bit, 0)
    set_bb_reg(t, _REG_CCX_CTRL, trig_bit, 1)


def _env_mntr(t, st: WatchdogState) -> None:
    """phydm_env_mntr_watchdog [SRC] phydm_ccx.c:2338 — the NHM/CLM/FAHM measurement engines, in
    order NHM-get/set, CLM-get/set, NHM-trig, CLM-trig, then FAHM (get/set/trig). At monitor idle
    with the IGI unchanged the period/include SETs fire once (first tick) and the threshold curves
    are suppressed (carried *_igi already equals the live IGI); only the trigger bit flips repeat."""
    # NHM get + mntr_set
    _ccx_get_result(t, st, 1 << 1, _REG_NHM_RDY, _REG_NHM_RPT)
    if not st.nhm_include_set:
        set_bb_reg(t, _REG_CCX_CTRL, 0xF00, 0x1)        # CNT_ALL + ccx-enable (identity)
        st.nhm_include_set = True
    if st.nhm_period != _NHM_PERIOD_MAX:
        set_bb_reg(t, _REG_CCX_PERIOD, 0xFFFF0000, _NHM_PERIOD_MAX)
        st.nhm_period = _NHM_PERIOD_MAX
    igi = t.read32(_REG_IGI_A) & 0x7F                    # nhm th_update_chk
    if igi != st.nhm_igi:
        st.nhm_igi = igi                                # th-curve writes — unexercised (IGI steady)
    # CLM get + mntr_set (period already 0xffff from init -> suppressed)
    _ccx_get_result(t, st, 1 << 0, _REG_CLM_RDY, ())
    if st.clm_period != _CLM_PERIOD_MAX:
        set_bb_reg(t, _REG_CCX_PERIOD, 0x0000FFFF, _CLM_PERIOD_MAX)
        st.clm_period = _CLM_PERIOD_MAX
    _ccx_trigger(t, 1 << 1)                             # NHM trigger
    _ccx_trigger(t, 1 << 0)                             # CLM trigger
    # FAHM get + mntr_set + trig (FAHM is ready in the capture -> denom + 6 report dwords read)
    if t.read32(_REG_FAHM_RDY) & (1 << 31):
        t.read32(_REG_FAHM_RDY)                         # denominator [15:0]
        for i in range(6):
            t.read32(_REG_FAHM_RPT + i * 4)
    if not st.fahm_include_set:
        set_bb_reg(t, _REG_CCX_CTRL, 0xE0, 0x1)         # INCLUDE_FA
        st.fahm_include_set = True
    if st.fahm_period != _NHM_PERIOD_MAX:
        set_bb_reg(t, _REG_FAHM_CTRL, 0xFFFF00, _NHM_PERIOD_MAX)
        st.fahm_period = _NHM_PERIOD_MAX
    igi = t.read32(_REG_IGI_A) & 0x7F                    # fahm th_update_chk
    if igi != st.fahm_igi:
        st.fahm_igi = igi
    _ccx_trigger(t, 1 << 2)                             # FAHM trigger


def tick(t, st: WatchdogState) -> None:
    """One dynamic-check watchdog tick, in wire order. ``phydm_noisy_detection`` / ``phydm_dig``
    (no change) / ``phydm_ra_info_watchdog`` / ``phydm_cfo_tracking`` are software-silent here."""
    _sreset_check(t)
    _usb_rx_agg(t)
    t.read32(_REG_RCR)                                   # hw_var_rcr_get (interleaved)
    cnt_all, cnt_cck_fail = _fa_cnt_statistics(t, st)
    _dig(t, st, cnt_all)
    _cck_pd_th(t, st, cnt_cck_fail)
    _adaptivity(t, st)
    _rrsr_reset(t)
    _halrf_thermal(t, st)
    _dyn_bw_indication(t)
    _env_mntr(t, st)
    st.first_tick = False
