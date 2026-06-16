"""Unit tests for the RTL8822BU PHYDM watchdog DIG (RX IGI adaptation), monitor/unlinked path.

The DIG is stateful (the IGI accumulates across ticks) and its capture window interleaves with the
spur sweep's igi_toggle, so it isn't cleanly pcap-sliceable; these assert the monitor IGI math against
the verbatim source extraction (phydm_dig.c). `fa_cnt_statistics_ac` itself is replay-verified offline.
"""
from wifit3.chips.rtl8822bu_dkms.dm_watchdog import (
    CCK_PD_LV_0,
    CCK_PD_LV_1,
    DigState,
    FaCnt,
    adaptivity,
    cck_pd_th,
    phydm_dig,
)


class _FakeBB:
    """Minimal BB transport: records the register writes the watchdog makes (set_bb_reg = RMW)."""
    def __init__(self):
        self.regs: dict[int, int] = {}
        self.regs8: dict[int, int] = {}

    def read32(self, addr):
        return self.regs.get(addr, 0)

    def write32(self, addr, val):
        self.regs[addr] = val & 0xFFFFFFFF

    def write8(self, addr, val):
        self.regs8[addr] = val & 0xFF


def _run(st, cnt_all):
    bb = _FakeBB()
    phydm_dig(bb, st, FaCnt(cnt_all=cnt_all))
    return bb


def test_first_disconnect_forces_min():
    st = DigState(cur_ig_value=0x30, first_disconnect=True)
    bb = _run(st, cnt_all=0)
    assert st.cur_ig_value == 0x1C
    assert bb.regs[0x0C50] & 0x7F == 0x1C
    assert bb.regs[0x0E50] & 0x7F == 0x1C        # path B too


def test_high_fa_steps_up_2():
    st = DigState(cur_ig_value=0x24, dig_max_of_min=0x30, first_disconnect=False)
    bb = _run(st, cnt_all=6000)                  # > fa_th[2]=5000 -> +2
    assert st.cur_ig_value == 0x26
    assert bb.regs[0x0C50] & 0x7F == 0x26


def test_mid_fa_steps_up_1():
    st = DigState(cur_ig_value=0x24, dig_max_of_min=0x30, first_disconnect=False)
    _run(st, cnt_all=4500)                       # > fa_th[1]=4000 -> +1
    assert st.cur_ig_value == 0x25


def test_low_fa_steps_down_2():
    st = DigState(cur_ig_value=0x24, dig_max_of_min=0x30, first_disconnect=False)
    _run(st, cnt_all=1000)                        # < fa_th[0]=2000 -> -2
    assert st.cur_ig_value == 0x22


def test_mid_band_no_change_no_write():
    st = DigState(cur_ig_value=0x24, dig_max_of_min=0x30, first_disconnect=False)
    bb = _run(st, cnt_all=3000)                   # between fa_th[0] and fa_th[1] -> unchanged
    assert st.cur_ig_value == 0x24
    assert 0x0C50 not in bb.regs                  # write skipped when IGI unchanged


def test_upper_clamp_to_dig_max_of_min():
    st = DigState(cur_ig_value=0x22, dig_max_of_min=0x22, first_disconnect=False)
    _run(st, cnt_all=6000)                         # +2 -> 0x24, clamped to range_max=0x22
    assert st.cur_ig_value == 0x22


def test_lower_clamp_to_min_coverage():
    st = DigState(cur_ig_value=0x1C, dig_max_of_min=0x30, first_disconnect=False)
    _run(st, cnt_all=0)                            # -2 -> 0x1a, clamped to range_min=0x1c
    assert st.cur_ig_value == 0x1C


def test_cck_new_agc_writes_a0c_mirror():
    st = DigState(cur_ig_value=0x24, dig_max_of_min=0x30, cck_new_agc=True, first_disconnect=False)
    bb = _run(st, cnt_all=6000)                    # -> 0x26; 0xA0C[13:8] = 0x26>>1 = 0x13
    assert (bb.regs[0x0A0C] & 0x3F00) >> 8 == 0x13


# ---- CCK packet-detection (phydm_cckpd_type1, unlinked) -----------------------------------------

def test_cck_pd_drops_to_lv0_writes_0x40():
    st = DigState(cck_pd_lv=CCK_PD_LV_1)             # cck_fa_ma starts at RESET
    bb = _FakeBB()
    cck_pd_th(bb, st, FaCnt(cck_fail=100))           # ma <- 100 (< 500) -> LV_0
    assert st.cck_pd_lv == CCK_PD_LV_0
    assert bb.regs8[0x0A0A] == 0x40


def test_cck_pd_rises_to_lv1_writes_0x83():
    st = DigState(cck_pd_lv=CCK_PD_LV_0)
    bb = _FakeBB()
    cck_pd_th(bb, st, FaCnt(cck_fail=2000))          # ma <- 2000 (> 1000) -> LV_1
    assert st.cck_pd_lv == CCK_PD_LV_1
    assert bb.regs8[0x0A0A] == 0x83


def test_cck_pd_hysteresis_band_no_write():
    st = DigState(cck_pd_lv=CCK_PD_LV_1, cck_fa_ma=700)   # already seeded (not RESET)
    bb = _FakeBB()
    cck_pd_th(bb, st, FaCnt(cck_fail=700))           # ma stays 700, in [500,1000] -> hold
    assert 0x0A0A not in bb.regs8


def test_cck_pd_same_level_no_write():
    st = DigState(cck_pd_lv=CCK_PD_LV_1)
    bb = _FakeBB()
    cck_pd_th(bb, st, FaCnt(cck_fail=2000))          # ma 2000 -> LV_1, already LV_1 -> no write
    assert 0x0A0A not in bb.regs8


# ---- Adaptivity / EDCCA (phydm_edcca_thre_calc NORMAL, 11AC) ------------------------------------

def test_adaptivity_thresholds_from_igi():
    st = DigState(cur_ig_value=0x20)
    bb = _FakeBB()
    adaptivity(bb, st)                                # th_l2h = max(0x28, 48) = 48 = 0x30
    assert bb.regs[0x08A4] & 0xFF == 0x30            # L2H (byte0)
    assert (bb.regs[0x08A4] >> 8) & 0xFF == 0x28     # H2L = 0x30 - 8 (byte1)


def test_adaptivity_l2h_lower_bound():
    st = DigState(cur_ig_value=0x10)                 # igi+8 = 24 < 48 -> clamp to 48
    bb = _FakeBB()
    adaptivity(bb, st)
    assert bb.regs[0x08A4] & 0xFF == 0x30            # 48


def test_adaptivity_high_igi():
    st = DigState(cur_ig_value=0x3E)                 # igi+8 = 70 = 0x46 > 48
    bb = _FakeBB()
    adaptivity(bb, st)
    assert bb.regs[0x08A4] & 0xFF == 0x46
    assert (bb.regs[0x08A4] >> 8) & 0xFF == 0x3E     # 0x46 - 8
