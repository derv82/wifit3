"""Unit tests for the RTL8822BU PHYDM watchdog DIG (RX IGI adaptation), monitor/unlinked path.

The DIG is stateful (the IGI accumulates across ticks) and its capture window interleaves with the
spur sweep's igi_toggle, so it isn't cleanly pcap-sliceable; these assert the monitor IGI math against
the verbatim source extraction (phydm_dig.c). `fa_cnt_statistics_ac` itself is replay-verified offline.
"""
from wifit3.chips.rtl8822bu_dkms.dm_watchdog import DigState, FaCnt, phydm_dig


class _FakeBB:
    """Minimal BB transport: records the IGI register writes the DIG makes (set_bb_reg = RMW)."""
    def __init__(self):
        self.regs: dict[int, int] = {}

    def read32(self, addr):
        return self.regs.get(addr, 0)

    def write32(self, addr, val):
        self.regs[addr] = val & 0xFFFFFFFF


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
