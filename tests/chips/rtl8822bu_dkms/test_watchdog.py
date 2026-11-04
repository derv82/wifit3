"""Unit tests for the RTL8822BU dynamic-check tick branches the captures do not reach (the replay covers the
captured ones): DIG clamps, CCK-PD hysteresis, and the TX power-tracking mix-mode swing split."""
from wifit3.chips.rtl8822bu_dkms.cal import (CCK_FA_MA_RESET, CCK_PD_LV_0, CCK_PD_LV_1,
                                             TX_SCALING_TABLE_JAGUAR, DmState)
from wifit3.chips.rtl8822bu_dkms.dm_watchdog import cck_pd_th, odm_tx_pwr_track_set_pwr8822b, phydm_dig
from wifit3.chips.rtl8822bu_dkms.txpower import TxpwrPG


class _FakeBB:
    """Register file for the masked read-modify-write accessors."""
    def __init__(self):
        self.regs: dict[int, int] = {}
        self.regs8: dict[int, int] = {}

    def read32(self, addr):
        return self.regs.get(addr, 0)

    def write32(self, addr, val):
        self.regs[addr] = val & 0xFFFFFFFF

    def write8(self, addr, val):
        self.regs8[addr] = val & 0xFF


def _dm(cur_ig_value=0x20, **fields) -> DmState:
    st = DmState()
    st.cur_ig_value = cur_ig_value
    for name, value in fields.items():
        setattr(st, name, value)
    return st


def test_dig_steps_by_fa_thresholds():
    for fa, igi in ((6000, 0x26), (4500, 0x25), (3000, 0x24), (1000, 0x22)):
        st = _dm(0x24)
        phydm_dig(_FakeBB(), st, fa)
        assert st.cur_ig_value == igi


def test_dig_clamps_to_max_of_min_and_min_coverage():
    st = _dm(0x2A)
    phydm_dig(_FakeBB(), st, 6000)
    assert st.cur_ig_value == 0x2A
    st = _dm(0x1C)
    phydm_dig(_FakeBB(), st, 0)
    assert st.cur_ig_value == 0x1C


def test_dig_write_mirrors_cck_agc_and_big_jump():
    bb = _FakeBB()
    st = _dm(0x24, cck_new_agc=True, big_jump_step1=3)
    phydm_dig(bb, st, 6000)
    assert (bb.regs[0x0A0C] & 0x3F00) >> 8 == 0x26 >> 1
    assert (bb.regs[0x08C8] & 0xE) >> 1 == 3
    assert bb.regs[0x0C50] & 0x7F == 0x26 and bb.regs[0x0E50] & 0x7F == 0x26


def test_cck_pd_hysteresis_band_holds():
    bb = _FakeBB()
    st = _dm(cck_pd_lv=CCK_PD_LV_1, cck_fa_ma=700)
    cck_pd_th(bb, st, 700)
    assert st.cck_pd_lv == CCK_PD_LV_1 and 0x0A0A not in bb.regs8


def test_cck_pd_level_change_resets_moving_average():
    bb = _FakeBB()
    st = _dm(cck_pd_lv=CCK_PD_LV_1)
    cck_pd_th(bb, st, 100)
    assert st.cck_pd_lv == CCK_PD_LV_0 and st.cck_fa_ma == CCK_FA_MA_RESET
    assert bb.regs8[0x0A0A] == 0x40


def _pg(cck_base: int) -> TxpwrPG:
    return TxpwrPG([[cck_base] * 6] * 2, [[0] * 5] * 2, [[0] * 14] * 2,
                   [{"2g": 0, "5g": 0}] * 2, [{"2g": 0, "5g": 0}] * 2, [{"2g": 0, "5g": 0}] * 2)


def _set_pwr(absolute: int, cck_base: int) -> tuple[DmState, _FakeBB]:
    bb = _FakeBB()
    st = _dm(default_ofdm_index=24, absolute_ofdm_swing_idx=[absolute, absolute])
    odm_tx_pwr_track_set_pwr8822b(bb, st, 0, 1, _pg(cck_base))
    return st, bb


def test_pwr_track_hotter_within_agc_headroom_goes_to_tx_agc():
    st, bb = _set_pwr(absolute=3, cck_base=0x30)          # offset min(63-48, 15) = 15
    assert (bb.regs[0x0C94] >> 25) & 0x1F == 3
    assert bb.regs[0x0C1C] >> 21 == TX_SCALING_TABLE_JAGUAR[24]


def test_pwr_track_hotter_past_agc_headroom_spills_into_bb_swing():
    st, bb = _set_pwr(absolute=6, cck_base=0x3B)          # offset 63-59 = 4, remnant 2
    assert (bb.regs[0x0C94] >> 25) & 0x1F == 4
    assert st.bb_swing_idx_ofdm[0] == 26
    assert bb.regs[0x0C1C] >> 21 == TX_SCALING_TABLE_JAGUAR[26]


def test_pwr_track_colder_lowers_bb_swing_only():
    st, bb = _set_pwr(absolute=-4, cck_base=0x30)
    assert (bb.regs[0x0C94] >> 25) & 0x1F == 0
    assert bb.regs[0x0C1C] >> 21 == TX_SCALING_TABLE_JAGUAR[20]
