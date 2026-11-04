from wifit3.chips.rtl8812au_dkms import efuse, watchdog
from wifit3.chips.rtl88xxau_base.sipi import RF_PATH_A, RF_PATH_B


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
    watchdog.odm_tx_pwr_track_set_pwr8812a(rec, st, 1, RF_PATH_A, make_params())
    assert ("W32", watchdog.REG_A_TX_SCALE_JAGUAR, watchdog.tx_scaling_table_jaguar[32] << 21) in rec.ops
    assert st.remnant_ofdm_swing_idx[RF_PATH_A] == -38
    assert st.remnant_cck_swing_idx == -38
    assert st.modify_tx_agc_flag_path_a


def make_params() -> efuse.ChipParams:
    pp_2g = efuse.PathTxPwr(cck_base=(0x2A,) * 6, bw40_base=(0x28,) * 5, cck_diff=(0, 0, 0),
                            ofdm_diff=(2, 0, 0), bw20_diff=(0, -1, 0))
    pp_5g = efuse.PathTxPwr(cck_base=(), bw40_base=(0x26,) * 14, cck_diff=(0, 0, 0),
                            ofdm_diff=(2, 0, 0), bw20_diff=(0, -1, 0))
    return efuse.ChipParams(
        crystal_cap=0x20, mac_address=None, rfe_type=3, autoload_fail=False, sys_cfg=0, is_c_cut=True,
        tx_power_2g=[pp_2g, pp_2g], tx_power_5g=[pp_5g, pp_5g], bb_swing_2g=[0x200, 0x200],
        bb_swing_5g=[0x200, 0x200], board_type=0, type_glna=0, type_gpa=0, type_alna=0, type_apa=0,
        interface_sel=0, eeprom_thermal_meter=0x20)


def test_over_limit_reapplies_path_a_txagc_with_the_remnant():
    # halrf_8812a_ce.c:212: 24 + 10 = 34 > 32 leaves remnant 2 for PHY_GetTxPowerTrackingOffset.
    st = watchdog.PhydmState()
    st.absolute_ofdm_swing_idx[RF_PATH_A] = 10
    rec = Rec()
    watchdog.odm_tx_pwr_track_set_pwr8812a(rec, st, 1, RF_PATH_A, make_params())
    assert st.remnant_cck_swing_idx == 2
    assert st.remnant_ofdm_swing_idx[RF_PATH_A] == 2
    assert ("W32", 0x0C20, 0x2C) in rec.ops                        # CCK 1M: 0x2A + 0 + 2
    assert ("W32", 0x0C24, 0x2C) in rec.ops                        # OFDM 6M: 0x28 + 2 + 2
    assert not any(op[1] == 0x0E20 for op in rec.ops)


def test_back_in_range_reapplies_txagc_without_the_remnant():
    st = watchdog.PhydmState()
    st.modify_tx_agc_flag_path_b = True
    st.remnant_ofdm_swing_idx[RF_PATH_B] = 3
    rec = Rec()
    watchdog.odm_tx_pwr_track_set_pwr8812a(rec, st, 36, RF_PATH_B, make_params())
    assert st.remnant_ofdm_swing_idx[RF_PATH_B] == 0
    assert not st.modify_tx_agc_flag_path_b
    assert ("W32", 0x0E24, 0x28) in rec.ops                        # 5 GHz OFDM 6M: 0x26 + 2
    assert not any(op[1] == 0x0E20 for op in rec.ops)              # no CCK on 5 GHz
