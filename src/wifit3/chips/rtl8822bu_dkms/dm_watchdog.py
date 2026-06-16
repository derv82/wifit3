"""RTL8822BU runtime PHYDM watchdog — the per-hop + ~2 s loop the vendor runs during RX.

Ports `phydm_watchdog` (phydm.c:2384) for the jaguar2 (11AC) path. The loop reads the BB
false-alarm / CCA / CRC32 counters, then DIG adapts the RX IGI (0xC50/0xE50) from those counts.
Without it the IGI is frozen at the `dig_init` seed and the RX gain never tracks the channel's
false-alarm rate — measured as a far lower beacon rate than the vendor capture sustains.

Every `odm_get_bb_reg` is a full 32-bit BB read (a control read of the BB address); the field mask
is applied in software. So the read SEQUENCE is what the byte-for-byte gate checks.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import sipi

# DIG boundary constants (CE driver, DIG_HW==0) [SRC] phydm_dig.h:39-54
DIG_MAX_COVERAGE = 0x26                  # DIG_MAX_COVERAGR — unlinked upper
DIG_MIN_COVERAGE = 0x1C                  # DIG_MIN_COVERAGE — unlinked lower
DIG_MAX_OF_MIN_COVERAGE = 0x22           # the unlinked dig_max_of_min default

# CCK packet-detection (8822b == CCK_PD_IC_TYPE1, single-byte 0xA0A writer) [SRC] phydm_cck_pd.c
CCK_FA_MA_RESET = 0xFFFFFFFF
CCK_PD_LV_0, CCK_PD_LV_1 = 0, 1
_CCK_PD_TH = {CCK_PD_LV_0: 0x40, CCK_PD_LV_1: 0x83}   # LV_2/3/4 only reachable when linked

# Adaptivity / EDCCA (NORMAL mode = the CE default) [SRC] phydm_adaptivity.h:33-35
TH_L2H_DIFF_IGI = 8
EDCCA_TH_L2H_LB = 48
EDCCA_HL_DIFF_NORMAL = 8


@dataclass
class FaCnt:
    """[SRC] phydm_fa_struct — the false-alarm / CCA / CRC32 counter snapshot DIG consumes."""
    cck_fail: int = 0
    ofdm_fail: int = 0
    cnt_all: int = 0
    cck_cca: int = 0
    ofdm_cca: int = 0
    cnt_cca_all: int = 0


def fa_cnt_statistics_ac(t) -> FaCnt:
    """[SRC] phydm_fa_cnt_statistics_ac (phydm_dig.c:1801) — read the jaguar2 FA/CCA/CRC32 counters.

    The 20-read sequence (0xF50, 0xFCC, 0xFC8, 0xFCC, 0xFD0, 0xFBC, 0xFC0, 0xFC4, 0xFC8, 0xF48, 0xA5C,
    0xF08, 0xF04, 0xF14, 0xF1C, 0xF10, 0xF18, 0xF0C, 0xF54, 0x808) is byte-identical to the watchdog's
    wire. Only `cnt_all` (total FA) and `cnt_cca_all` (total CCA) feed DIG; the rest are read (the HW
    requires the read to advance the counter latch) but kept only where DIG needs them."""
    fa = FaCnt()
    t.read32(0xF50)                                # {cck,ofdm}_txen — unused by DIG
    t.read32(0xFCC)                                # cck_txon (LWORD)
    t.read32(0xFC8)                                # ofdm_txon (HWORD)
    t.read32(0xFCC)                                # TYPE1: fast_fsync (HWORD)
    t.read32(0xFD0)                                # TYPE2: sb_search_fail
    t.read32(0xFBC)                                # TYPE3: parity_fail / rate_illegal
    t.read32(0xFC0)                                # TYPE4: crc8_fail / mcs_fail
    t.read32(0xFC4)                                # TYPE5: vht crc8
    t.read32(0xFC8)                                # TYPE6: mcs_fail_vht (LWORD)
    fa.ofdm_fail = t.read32(0xF48) & 0xFFFF        # OFDM FA counter
    fa.cck_fail = t.read32(0xA5C) & 0xFFFF         # CCK FA counter
    v = t.read32(0xF08)                            # CCK/OFDM CCA
    fa.ofdm_cca = (v >> 16) & 0xFFFF
    fa.cck_cca = v & 0xFFFF
    t.read32(0xF04)                                # CCK CRC32
    t.read32(0xF14)                                # OFDM CRC32
    t.read32(0xF1C)                                # OFDM2 CRC32
    t.read32(0xF10)                                # HT CRC32
    t.read32(0xF18)                                # HT2 CRC32
    t.read32(0xF0C)                                # VHT CRC32
    t.read32(0xF54)                                # VHT2 CRC32
    cck_enable = t.read32(0x808) & (1 << 28)       # ODM_REG_BB_RX_PATH[28]: CCK block on (2.4 GHz)
    if cck_enable:
        fa.cnt_all = fa.ofdm_fail + fa.cck_fail
        fa.cnt_cca_all = fa.cck_cca + fa.ofdm_cca
    else:
        fa.cnt_all = fa.ofdm_fail
        fa.cnt_cca_all = fa.ofdm_cca
    return fa


def false_alarm_counter_reg_reset(t) -> None:
    """[SRC] phydm_false_alarm_counter_reg_reset (phydm_dig.c:1640, 11AC) + phydm_reset_bb_hw_cnt
    (phydm_api.c:66) — latch the FA/CCA counters back to 0 so the next watchdog reads a fresh window:
    toggle OFDM-FA (0x9A4[17]), CCK-FA (0xA2C[15]), then the page-F counter (0xB58[0])."""
    sipi.set_bb_reg(t, 0x09A4, 1 << 17, 1)         # reset OFDM FA counter
    sipi.set_bb_reg(t, 0x09A4, 1 << 17, 0)
    sipi.set_bb_reg(t, 0x0A2C, 1 << 15, 0)         # reset CCK FA counter
    sipi.set_bb_reg(t, 0x0A2C, 1 << 15, 1)
    sipi.set_bb_reg(t, 0x0B58, 1 << 0, 1)          # phydm_reset_bb_hw_cnt: reset page-F counters
    sipi.set_bb_reg(t, 0x0B58, 1 << 0, 0)


@dataclass
class DigState:
    """[SRC] phydm_dig_struct — the carried DIG state. Only `cur_ig_value` (the IGI accumulator) and
    `dig_max_of_min` (the upper clamp) survive across ticks; the rest is recomputed each tick. wifit3
    never associates, so `is_linked` is always False and only the unlinked/monitor path runs."""
    cur_ig_value: int = 0x20                        # seeded from 0xC50 at dig_init
    dig_max_of_min: int = DIG_MAX_OF_MIN_COVERAGE   # unlinked abs-boundary leaves this at its default
    rx_gain_range_max: int = DIG_MAX_OF_MIN_COVERAGE
    rx_gain_range_min: int = DIG_MIN_COVERAGE
    cck_new_agc: bool = False                       # read once at dig_init (0xA9C[17])
    first_connect: bool = False
    first_disconnect: bool = True                   # the first post-init tick sees a disconnect
    fa_th: tuple = (2000, 4000, 5000)
    cck_fa_ma: int = CCK_FA_MA_RESET                # CCK-FA moving average (EWMA across ticks)
    cck_pd_lv: int = CCK_PD_LV_1                    # cck_pd_init writes LV_1 (0xA0A=0x83) at cold init


def _new_igi_by_fa(igi: int, cnt_all: int, fa_th: tuple, step: tuple) -> int:
    """[SRC] phydm_new_igi_by_fa — step IGI by the false-alarm count vs the three thresholds."""
    if cnt_all > fa_th[2]:
        return igi + step[0]
    if cnt_all > fa_th[1]:
        return igi + step[1]
    if cnt_all < fa_th[0]:
        return igi - step[2]
    return igi


def phydm_dig(t, st: DigState, fa: FaCnt) -> None:
    """[SRC] phydm_dig (phydm_dig.c:1397) — the unlinked/monitor, non-DFS RX-IGI adaptation.

    Boundaries (abs + dym, !is_linked): rx_gain_range_max = dig_max_of_min, rx_gain_range_min =
    DIG_MIN_COVERAGE (0x1c). FA thresholds (unlinked, non-DFS) = {2000, 4000, 5000}, step = {+2,+1,-2}.
    New IGI: first_connect -> range_min; first_disconnect -> 0x1c; else step by cnt_all vs fa_th; then
    clamp to [range_min, range_max]. Written only when it changed (`odm_write_dig`). The linked-STA
    branches (rssi/throughput boundaries, DIG damping) never run in monitor and are not ported."""
    igi = st.cur_ig_value
    # abs + dym boundary (!is_linked): park at the lower bound, range_max = dig_max_of_min
    st.rx_gain_range_max = st.dig_max_of_min
    st.rx_gain_range_min = DIG_MIN_COVERAGE
    if st.rx_gain_range_min > st.rx_gain_range_max:    # phydm_dig_abnormal_case
        st.rx_gain_range_min = st.rx_gain_range_max
    st.fa_th = (2000, 4000, 5000)                      # phydm_fa_threshold_check (unlinked, non-DFS)

    step = (2, 1, 2)                                   # phydm_get_new_igi: unlinked step set
    if st.first_connect:
        igi = st.rx_gain_range_min
    elif st.first_disconnect:
        igi = DIG_MIN_COVERAGE
    else:
        igi = _new_igi_by_fa(igi, fa.cnt_all, st.fa_th, step)
    if igi < st.rx_gain_range_min:                     # dyn lower/upper clamp
        igi = st.rx_gain_range_min
    if igi >= st.rx_gain_range_max:
        igi = st.rx_gain_range_max
    _odm_write_dig(t, st, igi)


def _odm_write_dig(t, st: DigState, new_igi: int) -> None:
    """[SRC] odm_write_dig + phydm_write_dig_reg_c50 (phydm_dig.c:528/461) — write the new IGI to the
    path-A/B IGI regs (0xC50/0xE50[6:0]) only when it changed, with the CCK new-AGC mirror (0xA0C[13:8]
    = igi>>1) when `cck_new_agc`. The big-jump step (0x8C8) is gated by `enable_adjust_big_jump`, off by
    default. (The EDCCA-adapt sub-branch — phydm_adaptivity on a falling IGI — only runs when the BB is
    in EDCCA_ADAPT mode; wifit3's monitor seed leaves it in the normal mode, so it is not invoked.)"""
    if st.cur_ig_value == new_igi:
        return
    if st.cck_new_agc:
        sipi.set_bb_reg(t, 0x0A0C, 0x3F00, new_igi >> 1)
    sipi.set_bb_reg(t, 0x0C50, 0x7F, new_igi)
    sipi.set_bb_reg(t, 0x0E50, 0x7F, new_igi)
    st.cur_ig_value = new_igi


def cck_pd_th(t, st: DigState, fa: FaCnt) -> None:
    """[SRC] phydm_cck_pd_th + phydm_cckpd_type1 (phydm_cck_pd.c, 8822b = CCK_PD_IC_TYPE1, unlinked):
    smooth the CCK false-alarm count into an EWMA, pick LV_1 (>1000) / LV_0 (<500) / hold (500-1000
    hysteresis), and on a level *change* write the single-byte CCK PD threshold to 0xA0A (0x83 / 0x40).
    rssi_min/IGI are irrelevant on the unlinked branch; the only input is the CCK FA count."""
    cck_fa = fa.cck_fail
    if st.cck_fa_ma == CCK_FA_MA_RESET:
        st.cck_fa_ma = cck_fa
    else:
        st.cck_fa_ma = (st.cck_fa_ma * 3 + cck_fa) >> 2
    if st.cck_fa_ma > 1000:
        lv = CCK_PD_LV_1
    elif st.cck_fa_ma < 500:
        lv = CCK_PD_LV_0
    else:
        return                                       # hysteresis band: hold, no write
    if st.cck_pd_lv == lv:
        return
    st.cck_pd_lv = lv
    st.cck_fa_ma = CCK_FA_MA_RESET                    # MA resets on every level change
    t.write8(0x0A0A, _CCK_PD_TH[lv])


def adaptivity(t, st: DigState) -> None:
    """[SRC] phydm_edcca_thre_calc (NORMAL mode, 11AC `< JGR2 & N` branch) + phydm_set_edcca_threshold
    (phydm_adaptivity.c) — derive the EDCCA L2H/H2L thresholds from the current IGI and write them to
    0x8A4 byte0/byte1 each tick. `th_l2h = max(igi+8, 48)`, `th_h2l = th_l2h - 8`. The ADAPT-mode path
    (l2h_dyn_min, the 0x209 dbg port, 0xCE8/0x8DC) stays dormant — the CE build defaults to NORMAL."""
    th_l2h = max(st.cur_ig_value + TH_L2H_DIFF_IGI, EDCCA_TH_L2H_LB)
    th_h2l = (th_l2h - EDCCA_HL_DIFF_NORMAL) & 0xFF
    sipi.set_bb_reg(t, 0x08A4, 0x000000FF, th_l2h)   # MASKBYTE0 = L2H
    sipi.set_bb_reg(t, 0x08A4, 0x0000FF00, th_h2l)   # MASKBYTE1 = H2L


def phydm_watchdog(t, st: DigState) -> None:
    """The runtime PHYDM loop wifit3 runs every ~2 s + after each hop: read the FA/CCA counters, adapt
    the RX IGI from them, then reset the counters for the next window. This is the functional core of
    `phydm_watchdog` (phydm.c:2384) — the part that keeps RX gain tracking the channel. Other watchdog
    members the vendor also runs (TX-power tracking, cfo/ra) are TX/association-specific and never run in
    monitor; see RTL8822BU_DKMS.md. After init `first_disconnect` is True for one tick (forces IGI to
    0x1c), then the steady FA-driven hunt takes over."""
    fa = fa_cnt_statistics_ac(t)
    false_alarm_counter_reg_reset(t)                 # latch counters after reading (per the vendor order)
    cck_pd_th(t, st, fa)                             # CCK PD threshold (0xA0A) from CCK FA
    phydm_dig(t, st, fa)                             # RX IGI (0xC50/0xE50) from total FA
    adaptivity(t, st)                                # EDCCA thresholds (0x8A4) from the new IGI
    st.first_disconnect = False
    st.first_connect = False
