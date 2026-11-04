from wifit3.chips.rtl8812au_dkms import watchdog
from wifit3.chips.rtl88xxau_base.sipi import RF_PATH_A


class Rec:
    def __init__(self):
        self.ops = []

    def read32(self, a):
        self.ops.append(("R32", a))
        return 0

    def write32(self, a, v):
        self.ops.append(("W32", a, v))


def test_negative_final_ofdm_swing_index_wraps_to_the_over_limit_branch():
    # halrf_8812a_ce.c:97: u8 final_ofdm_swing_index; 24 - 30 wraps to 250 > limit 32 (ch1, MGN_1M).
    st = watchdog.PhydmState()
    st.absolute_ofdm_swing_idx[RF_PATH_A] = -30
    rec = Rec()
    watchdog.odm_tx_pwr_track_set_pwr8812a(rec, st, 1, RF_PATH_A)
    assert ("W32", watchdog.REG_A_TX_SCALE_JAGUAR, watchdog.tx_scaling_table_jaguar[32] << 21) in rec.ops
    assert st.remnant_ofdm_swing_idx[RF_PATH_A] == -38
    assert st.remnant_cck_swing_idx == -38
    assert st.modify_tx_agc_flag_path_a
