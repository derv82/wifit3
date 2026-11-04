from wifit3.chips.rtl8821au_dkms import watchdog


class Rec:
    def __init__(self):
        self.ops = []

    def read32(self, a):
        self.ops.append(("R32", a))
        return 0

    def write32(self, a, v):
        self.ops.append(("W32", a, v))


def make_dm_state(default_ofdm_index: int) -> watchdog.DmState:
    return watchdog.DmState(cur_ig_value=0x20, nhm_igi=0x20, rrsr_val_init=0, h2l_lb=0, l2h_lb=0,
                            eeprom_thermal=0x20, default_ofdm_index=default_ofdm_index,
                            bb_swing_diff_2g=0, bb_swing_diff_5g=0)


def test_negative_final_ofdm_swing_index_wraps_to_the_over_limit_branch():
    # halrf_8821a_ce.c:55: u8 final_ofdm_swing_index; 24 - 30 wraps to 250 > limit 32 (ch1, MGN_1M).
    st = make_dm_state(24)
    st.absolute_ofdm_swing_idx = -30
    rec = Rec()
    watchdog._tx_pwr_track_set_pwr(rec, st, 1)
    assert ("W32", watchdog.REG_A_TX_SCALE_JAGUAR, watchdog.TX_SCALING_TABLE_JAGUAR[32] << 21) in rec.ops
    assert st.modify_tx_agc_flag_path_a
