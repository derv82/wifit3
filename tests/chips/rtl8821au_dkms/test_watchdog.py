from wifit3.chips.rtl8821au_dkms import efuse, watchdog

PP_2G = efuse.PathTxPwr(cck_base=(0x2A,) * 6, bw40_base=(0x28,) * 5, cck_diff=(0, 0, 0),
                        ofdm_diff=(2, 0, 0), bw20_diff=(0, 0, 0))
PP_5G = efuse.PathTxPwr(cck_base=(), bw40_base=(0x26,) * 14, cck_diff=(0, 0, 0),
                        ofdm_diff=(2, 0, 0), bw20_diff=(0, 0, 0))


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
    watchdog._tx_pwr_track_set_pwr(rec, st, 1, PP_2G, PP_5G)
    assert ("W32", watchdog.REG_A_TX_SCALE_JAGUAR, watchdog.TX_SCALING_TABLE_JAGUAR[32] << 21) in rec.ops
    assert st.modify_tx_agc_flag_path_a


def test_over_limit_reapplies_txagc_with_the_remnant():
    # halrf_8821a_ce.c:166: 24 + 10 = 34 > 32; CCK takes default_cck_index 0 + 10 - 32 = -22.
    st = make_dm_state(24)
    st.absolute_ofdm_swing_idx = 10
    rec = Rec()
    watchdog._tx_pwr_track_set_pwr(rec, st, 1, PP_2G, PP_5G)
    assert st.remnant_ofdm_swing_idx == 2
    assert st.remnant_cck_swing_idx == -22
    assert ("W32", 0x0C20, 0x2A - 22) in rec.ops                   # CCK 1M
    assert ("W32", 0x0C24, 0x2C) in rec.ops                        # OFDM 6M: 0x28 + 2 + 2


def test_back_in_range_reapplies_txagc_without_the_remnant():
    st = make_dm_state(24)
    st.modify_tx_agc_flag_path_a = True
    st.remnant_ofdm_swing_idx = 3
    rec = Rec()
    watchdog._tx_pwr_track_set_pwr(rec, st, 36, PP_2G, PP_5G)
    assert st.remnant_ofdm_swing_idx == 0
    assert not st.modify_tx_agc_flag_path_a
    assert ("W32", 0x0C24, 0x28) in rec.ops                        # 5 GHz OFDM 6M: 0x26 + 2
    assert not any(op[1] == 0x0C20 for op in rec.ops)              # no CCK on 5 GHz


def test_band_switch_records_the_path_b_swing():
    # rtl8812a_phycfg.c:1148-1151: phy_get_tx_bb_swing_8812a(RF_PATH_B) is the last call before the shift.
    st = make_dm_state(24)
    watchdog.set_bb_swing_by_band(st, True, 0x16A)
    assert st.bb_swing_diff_5g == -3
    assert st.default_ofdm_index == 24 - 6
    watchdog.set_bb_swing_by_band(st, False, 0x101)
    assert st.bb_swing_diff_2g == -6
    assert st.default_ofdm_index == 24 - 6 - 6
