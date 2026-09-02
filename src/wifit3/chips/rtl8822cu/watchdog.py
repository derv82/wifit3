"""RTL8822C phydm dynamic-check watchdog tick (JGR3 / Jaguar3, 2SS).

The kernel runs this ~2 s maintenance pass (rtw_dynamic_chk_wk_hdl -> phydm_watchdog,
phydm.c:2620). The 8822C takes the ODM_IC_JGR3_SERIES branch throughout, structurally different
from the 8821C AC model: the env-monitor splits into an early RESULT read pass and a late
SET/trigger pass over five CCX engines (NHM, CLM, FAHM, IFS-CLM, EDCCA-CLM); the false-alarm
counters live in the 0x2dxx/0x2cxx bank; DIG writes IGI to both paths; thermal power tracking is a
two-phase trigger.

In the capture the tick is dispatched by its unique opener (IN 0x0210, REG_TXDMA_STATUS) and never
interleaves with a channel hop. Only the members that reach the wire in monitor / no-link mode are
ported, in wire order (phydm.c:2624-2715). State that the chip does not re-encode in a read is
carried in ``WatchdogState``, seeded from the init ``PhydmState``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .dm import PhydmState
from .phy import (
    _RF_MASK,
    DBGPORT_PRI_2,
    MASKDWORD,
    get_bb_reg,
    get_rf_reg,
    set_bb_dbg_port,
    set_bb_reg,
    set_rf_reg,
)
from .transport import RTL8822CUTransport

# --- no-link DIG thresholds / clamp [SRC phydm_dig.c:1466, fa_threshold_check :43] ---
_FA_TH = (2000, 4000, 5000)
_DIG_MIN, _DIG_MAX_OF_MIN = 0x1C, 0x2A

# --- FA-counter read order (JGR3, 8822C VHT part) [SRC phydm_dig.c:345] ---
_FA_READ_SEQ = (0x2DE4, 0x2DE0, 0x2D20, 0x2D04, 0x2D08, 0x2D10, 0x2C04, 0x2C14, 0x2C1C, 0x2C10,
                0x2C18, 0x2C40, 0x2C2C, 0x2C30, 0x0664, 0x2C0C, 0x2C54, 0x2D10, 0x2D0C)
_REG_CCK_FA = 0x1A5C            # cnt_cck_fail [15:0]
_REG_CCK_CCA = 0x2C08
_REG_CCK_ENABLE = 0x1A14        # (v & 0x300) == 0 -> 2.4 GHz

# --- env-monitor CCX result/ready registers (JGR3) [SRC phydm_ccx.c] ---
_REG_NHM_RDY, _REG_NHM_RPT = 0x2D4C, (0x2D40, 0x2D44, 0x2D48, 0x2D4C)
_REG_CLM_RDY = 0x2D88
_REG_FAHM_RDY, _REG_FAHM_RPT = 0x2D84, (0x2D6C, 0x2D70, 0x2D74, 0x2D78, 0x2D7C, 0x2D80)
_REG_IFS_CLM_RPT = (0x2E60, 0x2E64, 0x2E68, 0x2E6C, 0x2E70, 0x2E74, 0x2E78, 0x2E7C, 0x2E80)
_REG_EDCCA_CLM_RDY = 0x2D8C

# --- env-monitor CCX set/trigger registers ---
_REG_CCX_TRIG = 0x1E60          # NHM/CLM/FAHM include + opt + trigger bits
_REG_NHM_PERIOD = 0x1E40        # NHM period [31:16]
_REG_FAHM_PERIOD_LO, _REG_FAHM_PERIOD_HI = 0x1E58, 0x1E5C
_REG_IGI = 0x1D70               # DIG IGI, [6:0] path A, [14:8] path B
_PERIOD_MAX = 0xFFFE            # NHM/FAHM measurement period (262 ms saturates to the max)
_REG_IFS_CLM_UNIT = 0x1EE4      # ctrl_unit [31:30] + restart BIT29
_REG_IFS_CLM_PERIOD = (0x1EEC, 0x1EF0, 0x1EF4, 0x1EF8)
_REG_EDCCA_CLM_TRIG = 0x1E5C    # BIT26


@dataclass
class WatchdogState:
    """phydm dynamic-mechanism state carried across ticks. Seeded from the init ``PhydmState`` via
    ``from_phydm`` so the first tick starts from the values ``odm_dm_init`` committed."""
    cur_ig_value: int = 0x20
    cck_pd_lv: int = 1
    cck_n_rx: int = 2
    cck_bw: int = 0
    cck_fa_ma: int = 0xFFFFFFFF
    cck_pd: dict = field(default_factory=dict)          # per (bw, n_rx) -> [(pd, cs) per level]
    first_tick: bool = True                             # crc32-cnt2 / RRSR / env period one-shots
    tm_trigger: bool = False                            # thermal 2-phase: False -> ARM, True -> CALLBACK
    nhm_igi: int = 0x20                                 # last IGI the NHM threshold curve was built for
    fahm_igi: int = 0x20
    rrsr_val: int = 0x11                                # ra_info_init's saved RRSR, masked to [19:0]
    # thermal power tracking, per path A/B. thermal_ref is tssi->thermal from EFUSE 0xd0/0xd1;
    # the avg ring and delta indices carry across CALLBACK ticks (init leaves them at 0).
    thermal_ref: tuple = (0xFF, 0xFF)
    power_track_type: int = 0
    thermal_avg: list = field(default_factory=lambda: [[0, 0, 0, 0], [0, 0, 0, 0]])
    thermal_avg_idx: list = field(default_factory=lambda: [0, 0])
    delta_power_index: list = field(default_factory=lambda: [0, 0])
    delta_power_index_last: list = field(default_factory=lambda: [0, 0])
    absolute_ofdm_swing_idx: list = field(default_factory=lambda: [0, 0])
    # thermal_value_lck: the averaged path-A meter latched the last time LCK fired. Seeds to the
    # EFUSE thermal reference (tssi->thermal[RF_PATH_A]) at init [SRC halrf_powertracking_ce.c:721].
    thermal_value_lck: int = 0xFF

    @classmethod
    def from_phydm(cls, dm: PhydmState) -> "WatchdogState":
        return cls(cur_ig_value=dm.cur_ig_value or 0x20, cck_n_rx=dm.cck_n_rx or 2,
                   cck_bw=dm.cck_bw, cck_pd=dm.cck_pd, rrsr_val=dm.rrsr_val_init & 0xFFFFF,
                   thermal_ref=tuple(dm.eeprom_thermal), power_track_type=dm.power_track_type,
                   thermal_value_lck=dm.eeprom_thermal[0])


def _sreset_check(t: RTL8822CUTransport) -> None:
    """rtw_hal_sreset xmit/linked status + RX-count checks. [SRC rtl8822c_ops.c]"""
    t.read32(0x0210)
    t.read32(0x0288)
    t.read16(0x1118)


def _usb_rx_agg(t: RTL8822CUTransport) -> None:
    """cfg_usb_rx_agg_88xx (USB, low-traffic monitor): pre-calc enable, USB-mode bits, size/timeout
    word (size 1, timeout 1 -> 0x0101 on 8822C). [SRC halmac_usb_88xx.c:88]"""
    dma_usb_agg = t.read8(0x0283)
    agg_enable = t.read8(0x010C)
    value32 = t.read32(0x0280)
    t.write32(0x0280, value32 | (1 << 29))              # EN_PRE_CALC
    t.write8(0x010C, agg_enable | (1 << 2))             # RXDMA_AGG_EN
    t.write8(0x0283, dma_usb_agg & ~(1 << 7))           # USB (not DMA) aggregation mode
    t.write16(0x0280, 0x01 | (0x01 << 8))               # size 1, timeout 1


def _hw_setting(t: RTL8822CUTransport) -> None:
    """phydm_hw_setting -> phydm_dynamic_switch_htstf_agc_8822c: read the NDP-valid counter off the
    HT-STF debug port; with no traffic (total_tp == 0) keep HT-STF AGC on. [SRC phydm_rtl8822c.c:29]"""
    set_bb_dbg_port(t, DBGPORT_PRI_2, 0x51F)
    t.read32(0x2DBC)                                    # get_bb_dbg_port_val (NDP-valid count)
    set_bb_reg(t, 0x08A0, 1 << 2, 1)


def _ccx_result_gated(t: RTL8822CUTransport, rdy_reg: int, rdy_bit: int,
                      rpt_regs: tuple[int, ...]) -> None:
    """A CCX engine's watchdog RESULT read: poll the ready flag, and read the report words (plus a
    ready re-read for duration/result) only when the chip reports ready. [SRC phydm_ccx.c get_result]"""
    if get_bb_reg(t, rdy_reg, 1 << rdy_bit):
        for r in rpt_regs:
            t.read32(r)
        t.read32(rdy_reg)


def _env_mntr_result(t: RTL8822CUTransport, st: WatchdogState) -> None:
    """phydm_env_mntr_result_watchdog: the early RESULT pass over the five CCX engines. NHM/CLM read
    only their ready flag until ready; FAHM/EDCCA-CLM add their report on ready; IFS-CLM reads its
    nine result words unconditionally. [SRC phydm_ccx.c:3714]"""
    _ccx_result_gated(t, _REG_NHM_RDY, 16, _REG_NHM_RPT[:3])
    _ccx_result_gated(t, _REG_CLM_RDY, 16, ())
    _ccx_result_gated(t, _REG_FAHM_RDY, 31, _REG_FAHM_RPT)
    for r in _REG_IFS_CLM_RPT:                          # IFS-CLM result: unconditional
        t.read32(r)
    _ccx_result_gated(t, _REG_EDCCA_CLM_RDY, 16, ())


def _fa_cnt_statistics(t: RTL8822CUTransport, st: WatchdogState) -> tuple[int, int]:
    """phydm_false_alarm_counter_statistics (JGR3): read the FA / CRC32 / CCA counters, the two
    diagnostic dbg ports, reset the counters, and (first tick) set the crc32-cnt2 spec rates.
    Returns (cnt_all, cnt_cck_fail) for DIG and CCK-PD. [SRC phydm_dig.c:345, :1636, :2033]"""
    vals = [t.read32(addr) for addr in _FA_READ_SEQ]
    fast_fsync = vals[2] & 0xFFFF
    sb_search_fail = (vals[2] >> 16) & 0xFFFF
    parity_fail = (vals[3] >> 16) & 0xFFFF
    rate_illegal = vals[4] & 0xFFFF
    crc8_fail = (vals[4] >> 16) & 0xFFFF
    mcs_fail = vals[5] & 0xFFFF
    mcs_fail_vht = (vals[17] >> 16) & 0xFFFF            # second 0x2D10 read
    crc8_fail_vhta = vals[18] & 0xFFFF
    cnt_ofdm_fail = (parity_fail + rate_illegal + crc8_fail + mcs_fail + fast_fsync
                     + sb_search_fail + mcs_fail_vht + crc8_fail_vhta)
    cnt_cck_fail = t.read32(_REG_CCK_FA) & 0xFFFF
    t.read32(_REG_CCK_CCA)                              # CCK/OFDM CCA counter
    cck_enable = get_bb_reg(t, _REG_CCK_ENABLE, 0x300)
    cnt_all = cnt_ofdm_fail + (cnt_cck_fail if cck_enable == 0 else 0)

    # phydm_get_dbg_port_info (JGR3): dbg_port0 + the EDCCA report.
    t.read32(0x2DB4)
    t.read32(0x2D38)

    # phydm_false_alarm_counter_reg_reset (JGR3, non-8723F).
    set_bb_reg(t, 0x1A2C, (1 << 15) | (1 << 14), 0)    # CCK FA counter reset
    set_bb_reg(t, 0x1A2C, (1 << 15) | (1 << 14), 2)
    set_bb_reg(t, 0x1A2C, (1 << 13) | (1 << 12), 0)    # CCK CCA counter reset
    set_bb_reg(t, 0x1A2C, (1 << 13) | (1 << 12), 2)
    set_bb_reg(t, 0x1D2C, 1 << 31, 0)                  # disable common rx clk gating
    set_bb_reg(t, 0x1EB4, 1 << 25, 1)                  # phydm_reset_bb_hw_cnt (OFDM CCA/FA)
    set_bb_reg(t, 0x1EB4, 1 << 25, 0)
    set_bb_reg(t, 0x1D2C, 1 << 31, 1)                  # re-enable common rx clk gating

    if st.first_tick:
        # phydm_set_crc32_cnt2_rate: ofdm2=6M (0xB), ht2/vht2/vht2-ss idx 0.
        set_bb_reg(t, 0x1EB8, 0x00000F00, 0x0B)
        set_bb_reg(t, 0x1EB8, 0x007F0000, 0x00)
        set_bb_reg(t, 0x1EB8, 0x0000F000, 0x00)
        set_bb_reg(t, 0x1EB8, 0x000000C0, 0x00)
    return cnt_all, cnt_cck_fail


def _write_dig(t: RTL8822CUTransport, new_igi: int) -> None:
    """odm_write_dig (JGR3, 2SS): the new IGI to both RX paths. [SRC phydm_dig.c write_dig_reg_jgr3]"""
    set_bb_reg(t, _REG_IGI, 0x7F, new_igi)
    set_bb_reg(t, _REG_IGI, 0x7F00, new_igi)


def _dig(t: RTL8822CUTransport, st: WatchdogState, cnt_all: int) -> None:
    """phydm_dig (no-link): step the IGI by the FA count against {2000,4000,5000}, clamp to
    [0x1c, 0x2a]; odm_write_dig only writes on a change. [SRC phydm_dig.c:1466, get_new_igi]"""
    igi = st.cur_ig_value
    if cnt_all > _FA_TH[2]:
        igi += 2
    elif cnt_all > _FA_TH[1]:
        igi += 1
    elif cnt_all < _FA_TH[0]:
        igi -= 2
    new_igi = max(_DIG_MIN, min(igi, _DIG_MAX_OF_MIN))
    if new_igi != st.cur_ig_value:
        _write_dig(t, new_igi)
        st.cur_ig_value = new_igi


def _cck_pd_th(t: RTL8822CUTransport, st: WatchdogState, cnt_cck_fail: int) -> None:
    """phydm_cck_pd_th -> phydm_cckpd_type4 (no-link): fold CCK-FA into the moving average, pick the
    PD level (LV_1 if MA>1000, LV_0 if MA<500, else hold), and on a real (lv, n_rx, bw) change write
    the fused pd_th/cs_ratio. [SRC phydm_cck_pd.c:1692, cckpd_type4, set_cck_pd_lv_type4 :854]"""
    if st.cck_fa_ma == 0xFFFFFFFF:
        st.cck_fa_ma = cnt_cck_fail
    else:
        st.cck_fa_ma = (st.cck_fa_ma * 3 + cnt_cck_fail) >> 2
    if st.cck_fa_ma > 1000:
        lv = 1
    elif st.cck_fa_ma < 500:
        lv = 0
    else:
        return
    cck_n_rx = get_bb_reg(t, 0x1A2C, 0x60000) + 1
    cck_bw = get_bb_reg(t, 0x09B0, 0xC)
    if lv == st.cck_pd_lv and cck_n_rx == st.cck_n_rx and cck_bw == st.cck_bw:
        return
    st.cck_pd_lv, st.cck_n_rx, st.cck_bw = lv, cck_n_rx, cck_bw
    st.cck_fa_ma = 0xFFFFFFFF
    (pd_reg, pd_mask), (cs_reg, cs_mask) = _CCK_PD_FIELDS[(cck_bw, cck_n_rx - 1)]
    pd_value, cs_value = st.cck_pd[(cck_bw, cck_n_rx - 1)][lv]
    set_bb_reg(t, pd_reg, pd_mask, pd_value)
    set_bb_reg(t, cs_reg, cs_mask, cs_value)


# Per (bandwidth, rx-path index) CCK power-detect / carrier-sense fields [SRC phydm_cck_pd.c:854]
_CCK_PD_FIELDS = {
    (0, 0): ((0x1AC8, 0x000000FF), (0x1AD0, 0x0000001F)),
    (1, 0): ((0x1ACC, 0x000000FF), (0x1AD0, 0x01F00000)),
    (0, 1): ((0x1AC8, 0x0000FF00), (0x1AD0, 0x000003E0)),
    (1, 1): ((0x1ACC, 0x0000FF00), (0x1AD0, 0x3E000000)),
}


def _adaptivity(t: RTL8822CUTransport, st: WatchdogState) -> None:
    """phydm_adaptivity -> phydm_edcca_thre_calc_jgr3 (NORMAL mode): L2H = max(igi + 8, 48),
    H2L = L2H - 8, each biased by 0x80 into 0x84c byte2/byte3. [SRC phydm_adaptivity.c:845, :520]"""
    th_l2h = max(st.cur_ig_value + 8, 48)
    th_h2l = th_l2h - 8
    set_bb_reg(t, 0x084C, 0x00FF0000, (th_l2h + 0x80) & 0xFF)
    set_bb_reg(t, 0x084C, 0xFF000000, (th_h2l + 0x80) & 0xFF)


def _rrsr_reset(t: RTL8822CUTransport, st: WatchdogState) -> None:
    """phydm_rrsr_mask -> phydm_masked_rrsr_set_register (no-link): re-apply the saved RRSR init
    value as one masked RMW. Fires only while rrsr_val_curr differs (first tick). [SRC phydm_rainfo.c:1942]"""
    set_bb_reg(t, 0x0440, 0x000FFFFF, st.rrsr_val)


_AVG_THERMAL_NUM = 4                 # AVG_THERMAL_NUM_8822C [SRC halrf_8822c.h:29]
_TXPWR_TRACK_TABLE_SIZE = 30         # [SRC halrf_powertracking_ce.h:41]
_IQK_THRESHOLD = 8                   # threshold_iqk = IQK_THRESHOLD [SRC halrf.h:468]

# 8822C 2.4 GHz CCK delta swing tables, per path (monitor tx_rate 0 -> IS_CCK_RATE). Index by the
# clamped thermal delta; "_UP" is chosen when the meter reads above the EFUSE reference, "_DOWN"
# below. [SRC halhwimg8822c_rf.c:36943 (a_p), :36935 (a_n), :36931 (b_p), :36929 (b_n);
# selection get_delta_swing_table_8822c halrf_8822c.c:145]
_CCK_DELTA_SWING_UP = (
    (0, 1, 2, 3, 4, 5, 5, 6, 7, 8, 9, 10, 11, 11, 12, 13, 14, 15, 16, 17, 18, 18, 19, 20, 21, 22,
     23, 24, 24, 25),
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25,
     26, 27, 28, 29),
)
_CCK_DELTA_SWING_DOWN = (
    (0, 1, 2, 3, 3, 4, 5, 6, 6, 7, 8, 9, 9, 10, 11, 12, 12, 13, 14, 15, 15, 16, 17, 18, 18, 19, 20,
     21, 21, 22),
    (0, 1, 2, 3, 4, 5, 5, 6, 7, 8, 9, 10, 11, 11, 12, 13, 14, 15, 16, 17, 17, 18, 19, 20, 21, 22,
     23, 23, 24, 25),
)

# 8822C 5 GHz delta swing tables, per path, indexed by 5 GHz band (0: ch36-64, 1: ch100-144,
# 2: ch149-177). get_delta_swing_table_8822c selects these by current channel. [SRC halrf_8822c.c:170;
# halhwimg8822c_rf.c:36908 (5ga_p), :36899 (5ga_n), :36890 (5gb_p), :36881 (5gb_n)]
_5GA_DELTA_SWING_UP = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 20, 21, 22, 23, 24, 25, 26,
     27, 28, 29, 30),
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 20, 21, 22, 23, 24, 25, 26,
     27, 28, 29, 30),
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 20, 21, 22, 23, 24, 25, 26,
     27, 28, 29, 30),
)
_5GA_DELTA_SWING_DOWN = (
    (0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 23, 24, 25, 26, 27, 28,
     29, 30, 31, 33),
    (0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 23, 24, 25, 26, 27, 28,
     29, 30, 31, 33),
    (0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 23, 24, 25, 26, 27, 28,
     29, 30, 31, 33),
)
_5GB_DELTA_SWING_UP = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 22, 23,
     24, 25, 26, 27),
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 22, 23,
     24, 25, 26, 27),
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 22, 23,
     24, 25, 26, 27),
)
_5GB_DELTA_SWING_DOWN = (
    (0, 1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27,
     28, 29, 30, 32),
    (0, 1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27,
     28, 29, 30, 32),
    (0, 1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27,
     28, 29, 30, 32),
)


def _5g_band_index(channel: int) -> int | None:
    """The 5 GHz band index get_delta_swing_table_8822c selects on: 0 ch36-64, 1 ch100-144,
    2 ch149-177. [SRC halrf_8822c.c:170]"""
    if 36 <= channel <= 64:
        return 0
    if 100 <= channel <= 144:
        return 1
    if 149 <= channel <= 177:
        return 2
    return None


def _delta_swing_tables(channel: int):
    """get_delta_swing_table_8822c: the (up_a, down_a, up_b, down_b) delta swing tables for the
    current channel. Monitor tx_rate 0 is CCK, so 2.4 GHz takes the CCK set. [SRC halrf_8822c.c:145]"""
    if 1 <= channel <= 14:
        return (_CCK_DELTA_SWING_UP[0], _CCK_DELTA_SWING_DOWN[0],
                _CCK_DELTA_SWING_UP[1], _CCK_DELTA_SWING_DOWN[1])
    band = _5g_band_index(channel)
    if band is None:
        return (None, None, None, None)
    return (_5GA_DELTA_SWING_UP[band], _5GA_DELTA_SWING_DOWN[band],
            _5GB_DELTA_SWING_UP[band], _5GB_DELTA_SWING_DOWN[band])


def _phy_lc_calibrate(t: RTL8822CUTransport) -> None:
    """halrf_lck_trigger -> phy_lc_calibrate_8822c: the driver LC calibration, path A only. AAC then
    RT, all RF_PATH_A writes at RFREGOFFSETMASK (0xfffff) through the RF-over-BB window.
    [SRC halrf_8822c.c:1429 (phy_lc_calibrate_8822c), :188 (_phy_aac_calibrate_8822c), :211
    (_phy_rt_calibrate_8822c); config->phy_lc_calibrate = halrf_lck_trigger halrf_8822c.c:1450]"""
    # _phy_aac_calibrate_8822c [SRC halrf_8822c.c:188]
    set_rf_reg(t, 0, 0xB0, _RF_MASK, 0x1F0FA)
    set_rf_reg(t, 0, 0xCA, _RF_MASK, 0x80000)
    set_rf_reg(t, 0, 0xC9, _RF_MASK, 0x80001)
    for _ in range(100):                                # break when RF 0xCA[12] != 1; it reads 0 on
        if get_rf_reg(t, 0, 0xCA, 0x1000) != 0x1:       # the first read here, so exactly one poll
            break
    set_rf_reg(t, 0, 0xB0, _RF_MASK, 0x1F0F8)
    # _phy_rt_calibrate_8822c [SRC halrf_8822c.c:211]
    set_rf_reg(t, 0, 0xCC, _RF_MASK, 0x0F000)
    set_rf_reg(t, 0, 0xCC, _RF_MASK, 0x4F000)
    set_rf_reg(t, 0, 0xCC, _RF_MASK, 0x0F000)


def _thermal_callback(t: RTL8822CUTransport, st: WatchdogState, channel: int) -> None:
    """odm_txpowertracking_new_callback_thermal_meter (8822C, no link, MIX_MODE): read the settled
    RF 0x42[6:1] meter on both paths, fold it into the per path average ring, evolve the swing index
    against the EFUSE thermal reference and flush it to 0x18a0/0x41a0. [SRC halphyrf_ce.c:866]"""
    # RF 0x42[6:1], direct read window 0x3d08 (A) / 0x4d08 (B).
    # [SRC halphyrf_ce.c:916, phydm_hal_api8822c.c:206]
    thermal = [get_rf_reg(t, path, 0x42, 0x7E) for path in (0, 1)]
    if 0xFF in st.thermal_ref:                          # no PG thermal -> track nothing [SRC :940]
        return
    for p in (0, 1):
        st.thermal_avg[p][st.thermal_avg_idx[p]] = thermal[p]
        st.thermal_avg_idx[p] = (st.thermal_avg_idx[p] + 1) % _AVG_THERMAL_NUM
        samples = [v for v in st.thermal_avg[p] if v]   # average over the filled ring slots
        if samples:
            thermal[p] = sum(samples) // len(samples)
    # LC calibration: when the averaged path-A meter has drifted >= 20 C (IQK_THRESHOLD) from the
    # last LCK latch, re-latch and run the driver LCK. is_scan_in_process / rfk_forbidden are false
    # for our monitor tick, and 8822C is none of 8814A/8822B/8822E, so the branch is always taken
    # once the delta clears the threshold. [SRC halphyrf_ce.c:983-998]
    delta_lck = abs(thermal[0] - st.thermal_value_lck)
    if delta_lck >= _IQK_THRESHOLD:
        st.thermal_value_lck = thermal[0]
        _phy_lc_calibrate(t)
    up_a, down_a, up_b, down_b = _delta_swing_tables(channel)
    up, down = (up_a, up_b), (down_a, down_b)
    for p in (0, 1):                                    # swing index evolution [SRC halphyrf_ce.c:1001]
        st.delta_power_index_last[p] = st.delta_power_index[p]
        ref = st.thermal_ref[p]
        delta = min(abs(thermal[p] - ref), _TXPWR_TRACK_TABLE_SIZE - 1)
        table, sign = (up[p], 1) if thermal[p] > ref else (down[p], -1)
        if table is None:                               # channel outside every band: hold [SRC :1031]
            continue
        idx = sign * table[delta]
        st.delta_power_index[p] = idx
        st.absolute_ofdm_swing_idx[p] = idx
    # odm_tx_pwr_track_set_pwr8822c MIX_MODE: masked RMW of bits[6:0]. TSSI_MODE (type 4..7) writes
    # nothing. [SRC halrf_8822c.c:131, tracking method select halphyrf_ce.c:1100]
    if not (4 <= st.power_track_type <= 7):
        for reg, p in ((0x18A0, 0), (0x41A0, 1)):
            set_bb_reg(t, reg, 0x7F, st.absolute_ofdm_swing_idx[p] & 0x7F)


def _halrf_thermal(t: RTL8822CUTransport, st: WatchdogState, channel: int) -> None:
    """odm_txpowertracking_check_ce (8822C): a two-phase thermal-meter trigger toggled every tick.
    ARM (tm_trigger 0->1): pulse RF 0x42 BIT19 1/0/1 on both paths so the meter settles; the
    CALLBACK phase reads the settled meter and applies the swing. [SRC halrf_powertracking_ce.c:818]"""
    if not st.tm_trigger:
        for path in (0, 1):
            set_rf_reg(t, path, 0x42, 1 << 19, 1)
            set_rf_reg(t, path, 0x42, 1 << 19, 0)
            set_rf_reg(t, path, 0x42, 1 << 19, 1)
        st.tm_trigger = True
    else:
        _thermal_callback(t, st, channel)
        st.tm_trigger = False


def _dyn_bw_indication(t: RTL8822CUTransport) -> None:
    """phydm_dyn_bw_indication -> phydm_bw_fixed_setting (8822C): pri-ch field 0 (20 MHz) then
    bw-fixed enable on 0x878 (both already committed -> identity). [SRC phydm_api.c:839]"""
    set_bb_reg(t, 0x0878, 0xC0000000, 0x0)
    set_bb_reg(t, 0x0878, 1 << 28, 1)


def _nhm_th_update_chk(t: RTL8822CUTransport, st: WatchdogState) -> None:
    """phydm_nhm_th_update_chk: read the live IGI; on a change rebuild the NHM threshold curve.
    At monitor idle the IGI holds, so only the read hits the wire. [SRC phydm_ccx.c]"""
    igi = get_bb_reg(t, _REG_IGI, 0x7F)
    if igi != st.nhm_igi:
        st.nhm_igi = igi
        _set_nhm_th(t, igi)


def _fahm_th_update_chk(t: RTL8822CUTransport, st: WatchdogState) -> None:
    igi = get_bb_reg(t, _REG_IGI, 0x7F)
    if igi != st.fahm_igi:
        st.fahm_igi = igi
        _set_fahm_th(t, igi)


def _nhm_thresholds(igi: int) -> tuple[int, ...]:
    base = igi - 14                                     # CCA_CAP [SRC phydm_ccx.c:492]
    return tuple(((base + 2 * i) << 1) & 0xFF for i in range(11))


def _set_nhm_th(t: RTL8822CUTransport, igi: int) -> None:
    """phydm_nhm_set_th_reg (JGR3): rewrite the 11 NHM bucket edges from the new IGI."""
    th = _nhm_thresholds(igi)
    set_bb_reg(t, 0x1E44, MASKDWORD, th[0] | th[1] << 8 | th[2] << 16 | th[3] << 24)
    set_bb_reg(t, 0x1E48, MASKDWORD, th[4] | th[5] << 8 | th[6] << 16 | th[7] << 24)
    set_bb_reg(t, 0x1E5C, 0x00FF0000, th[8])
    set_bb_reg(t, _REG_CCX_TRIG, 0xFFFF0000, th[9] | th[10] << 8)


def _set_fahm_th(t: RTL8822CUTransport, igi: int) -> None:
    """phydm_fahm_set_th_reg (JGR3): rewrite the FAHM threshold curve from the new IGI."""
    th = _nhm_thresholds(igi)
    set_bb_reg(t, 0x1E50, MASKDWORD, th[0] | th[1] << 8 | th[2] << 16 | th[3] << 24)
    set_bb_reg(t, 0x1E54, MASKDWORD, th[4] | th[5] << 8 | th[6] << 16 | th[7] << 24)
    set_bb_reg(t, 0x1E58, 0x00FFFFFF, th[8] | th[9] << 8 | th[10] << 16)


def _env_mntr_set(t: RTL8822CUTransport, st: WatchdogState) -> None:
    """phydm_env_mntr_set_watchdog: the late SET/trigger pass. First tick programs the include/opt
    fields and measurement periods; every tick reads the IGI (th_update_chk) and flips each engine's
    trigger bit. [SRC phydm_ccx.c:3761]"""
    # NHM
    if st.first_tick:
        set_bb_reg(t, _REG_CCX_TRIG, 0xF00, 0x1)        # divi_opt CNT_ALL + ccx_en
        set_bb_reg(t, _REG_NHM_PERIOD, 0xFFFF0000, _PERIOD_MAX)
    _nhm_th_update_chk(t, st)
    set_bb_reg(t, _REG_CCX_TRIG, 1 << 1, 0)             # NHM trigger
    set_bb_reg(t, _REG_CCX_TRIG, 1 << 1, 1)
    # CLM (period 0xffff set at init and carried -> only the trigger flips)
    set_bb_reg(t, _REG_CCX_TRIG, 1 << 0, 0)
    set_bb_reg(t, _REG_CCX_TRIG, 1 << 0, 1)
    # FAHM
    if st.first_tick:
        set_bb_reg(t, _REG_CCX_TRIG, 0xE0, 0x1)         # numer_opt INCLU_FA
        set_bb_reg(t, _REG_CCX_TRIG, 0x7000, 0x4)       # denom_opt CRC_ERR
        set_bb_reg(t, _REG_CCX_TRIG, 1 << 4, 0x0)       # inclu_cck
        set_bb_reg(t, _REG_FAHM_PERIOD_LO, 0xFF000000, _PERIOD_MAX & 0xFF)
        set_bb_reg(t, _REG_FAHM_PERIOD_HI, 0xFF, (_PERIOD_MAX >> 8) & 0xFF)
    _fahm_th_update_chk(t, st)
    set_bb_reg(t, _REG_CCX_TRIG, 1 << 2, 0)             # FAHM trigger
    set_bb_reg(t, _REG_CCX_TRIG, 1 << 2, 1)
    # IFS-CLM
    if st.first_tick:
        # IFS-CLM period 0xEA60 (960 ms, BW20) packed across four registers [SRC ifs_clm_setting].
        set_bb_reg(t, _REG_IFS_CLM_UNIT, 0xC0000000, 0x3)       # ctrl_unit = IFS_CLM_16
        set_bb_reg(t, _REG_IFS_CLM_PERIOD[0], 0xC0000000, 0x0)  # period[1:0]
        set_bb_reg(t, _REG_IFS_CLM_PERIOD[1], 0xFE000000, 24)   # period[8:2]
        set_bb_reg(t, _REG_IFS_CLM_PERIOD[2], 0xC0000000, 0x1)  # period[10:9]
        set_bb_reg(t, _REG_IFS_CLM_PERIOD[3], 0x3E000000, 29)   # period[15:11]
    set_bb_reg(t, _REG_IFS_CLM_UNIT, 1 << 29, 0)        # IFS-CLM trigger / restart
    set_bb_reg(t, _REG_IFS_CLM_UNIT, 1 << 29, 1)
    # EDCCA-CLM
    set_bb_reg(t, _REG_EDCCA_CLM_TRIG, 1 << 26, 0)
    set_bb_reg(t, _REG_EDCCA_CLM_TRIG, 1 << 26, 1)


def tick(t: RTL8822CUTransport, st: WatchdogState, channel: int) -> None:
    """One phydm dynamic-check watchdog tick, in wire order (phydm.c:2624-2715). ``channel`` is the
    driver's current channel; the thermal callback selects its delta swing table by it."""
    _sreset_check(t)
    _usb_rx_agg(t)
    t.read32(0x0608)                                    # hw_var_rcr_get (interleaved)
    _hw_setting(t)
    _env_mntr_result(t, st)
    cnt_all, cnt_cck_fail = _fa_cnt_statistics(t, st)
    _dig(t, st, cnt_all)
    _cck_pd_th(t, st, cnt_cck_fail)
    _adaptivity(t, st)
    if st.first_tick:
        _rrsr_reset(t, st)
    _halrf_thermal(t, st, channel)
    _dyn_bw_indication(t)
    _env_mntr_set(t, st)
    st.first_tick = False
