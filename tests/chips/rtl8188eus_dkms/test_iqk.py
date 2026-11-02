"""Hardware-free regression for the RTL8188EUS (DKMS) IQ calibration (``iqk``) and the IQK
result feeding power tracking's ``set_iqk_matrix_8188e``."""
from wifit3.chips.rtl8188eus_dkms import iqk, powertrack
from wifit3.chips.rtl8188eus_dkms import powertrack_tbl as T

_RF = "wifit3.chips.rtl8188eus_dkms.powertrack.rf"


class RegTx:
    def __init__(self, regs=None):
        self.regs = dict(regs or {})
        self.w = []

    def read32(self, a):
        return self.regs.get(a, 0)

    def read8(self, a):
        return self.regs.get(a, 0) & 0xFF

    def write32(self, a, v):
        v &= 0xFFFFFFFF
        self.regs[a] = v
        self.w.append((a, v))

    def write8(self, a, v):
        v &= 0xFF
        self.regs[a] = v
        self.w.append((a, v))


def _state(**kw):
    return powertrack.PowerTrackState(
        eeprom_thermal=29, tx_pwr=None, default_ofdm_index=28, default_cck_index=20,
        delta_swing_table_idx_2g_cck_a_p=T.DELTA_TT_2G_CCK_A_P,
        delta_swing_table_idx_2g_cck_a_n=T.DELTA_TT_2G_CCK_A_N, **kw)


# IQK read-backs that pass every check: X=0x100, Y=0, RX X=0x100, RX Y=0.
_IQK_OK = {0x0E94: 0x01000000, 0x0E9C: 0x00000000, 0x0EA4: 0x01000000, 0x0EAC: 0x00000000}


def test_set_iqk_matrix_scales_swing_by_iqk_result():
    # OFDM_SWING_TABLE[25] = 0x300000C0 -> ele_D 0xC0; X=0x100 (unity), Y=0x3FF (-1).
    t = RegTx({0x0C94: 0, 0x0C4C: 0})
    powertrack._set_iqk_matrix(t, 25, 0x100, 0x3FF)
    assert t.regs[0x0C80] == (0xC0 << 22) | (0x3F << 16) | 0xC0
    assert t.regs[0x0C94] >> 28 == 0xF
    assert t.regs[0x0C4C] & (1 << 24) == 0


def test_set_iqk_matrix_without_result_writes_table_swing():
    t = RegTx({0x0C94: 0xF0000000, 0x0C4C: 1 << 24})
    powertrack._set_iqk_matrix(t, 25, 0, 0)
    assert t.regs[0x0C80] == T.OFDM_SWING_TABLE[25]
    assert t.regs[0x0C94] == 0 and t.regs[0x0C4C] == 0


def test_apply_mix_mode_reads_iqk_result_for_channel_slot_0():
    st = _state()
    st.iqk_matrix_reg_setting[0].value[0][0:2] = [0x100, 0]
    t = RegTx({0x0C4C: 0x001C0324})
    powertrack._apply_mix_mode(t, st, -3)            # final OFDM 25 -> ele_D 0xC0
    assert (0x0C80, (0xC0 << 22) | 0xC0) in t.w


def test_simularity_compare():
    same = [[0x100, 0, 0x100, 0, 0, 0, 0, 0], [0x102, 1, 0x0FF, 2, 0, 0, 0, 0],
            [0] * 8, [0] * 8]
    assert iqk.phy_simularity_compare_8188e(same, 0, 1) is True
    far = [[0x100, 0, 0x100, 0, 0, 0, 0, 0], [0x110, 0, 0x100, 0, 0, 0, 0, 0],
           [0] * 8, [0] * 8]
    assert iqk.phy_simularity_compare_8188e(far, 0, 1) is False
    # RX of c1 failed (zero): TX of c2 is copied into result[3], returns False.
    rx_failed = [[0x100, 0, 0, 0, 0, 0, 0, 0], [0x100, 0, 0x100, 0, 0, 0, 0, 0],
                 [0] * 8, [0] * 8]
    assert iqk.phy_simularity_compare_8188e(rx_failed, 0, 1) is False
    assert rx_failed[3][0:2] == [0x100, 0]


def test_phy_iq_calibrate_two_agreeing_runs_store_result_and_restore():
    regs = dict(_IQK_OK)
    regs.update({0x0522: 0x00, 0x0550: 0x18, 0x0551: 0x08, 0x0040: 0x00000060,
                 0x0C80: 0x300000C0, 0x0C04: 0x03A05611, 0x0820: 0x01000000})
    t = RegTx(regs)
    st = _state()
    iqk.phy_iq_calibrate_8188e(t, st, False)

    assert [v for a, v in t.w if a == 0x0E48].count(0xF9000000) == 6   # 2 runs x (TX + 2 RX)
    assert st.iqk_matrix_reg_setting[0].is_iqk_done
    assert st.iqk_matrix_reg_setting[0].value[0] == [0x100, 0, 0x100, 0, 0, 0, 0, 0]
    assert (st.rege94, st.rege9c) == (0x100, 0)
    # _phy_path_a_fill_iqk_matrix: TX0_A = (0x100 * 0xC0) >> 8.
    assert t.regs[0x0C80] & 0x3FF == 0xC0
    # Second run restores MAC/BB, SI mode, and the IQC defaults.
    assert (t.regs[0x0522], t.regs[0x0550], t.regs[0x0551]) == (0x00, 0x18, 0x08)
    assert t.regs[0x0040] == 0x00000060
    assert t.regs[0x0C04] == 0x03A05611
    assert t.regs[0x0820] == 0x01000000
    assert t.regs[0x0840] == 0x00032ED3
    assert t.regs[0x0E30] == 0x01008C00 and t.regs[0x0E34] == 0x01008C00


def test_phy_iq_calibrate_tx_failure_stores_zero_result():
    regs = dict(_IQK_OK)
    regs[0x0EAC] = 1 << 28                           # TX IQK fail bit
    t = RegTx(regs)
    st = _state()
    iqk.phy_iq_calibrate_8188e(t, st, False)
    # Zero results agree, so candidate 0 (all zero) is stored; set_iqk_matrix then
    # takes the no-result branch.
    assert st.iqk_matrix_reg_setting[0].value[0] == [0] * 8
    assert [v for a, v in t.w if a == 0x0E48].count(0xF9000000) == 8   # 2 runs x 2 TX retries x 2


def test_callback_runs_iqk_at_threshold(mocker):
    mocker.patch(f"{_RF}.phy_query_rf_reg", return_value=29 + 9)
    mocker.patch(f"{_RF}.set_rf_reg")
    mocker.patch("wifit3.chips.rtl8188eus_dkms.powertrack.phy_lc_calibrate")
    mocker.patch("wifit3.chips.rtl8188eus_dkms.powertrack._apply_mix_mode")
    cal = mocker.patch("wifit3.chips.rtl8188eus_dkms.iqk.phy_iq_calibrate_8188e")
    st = _state()
    powertrack._callback(RegTx(), st)
    cal.assert_called_once()
    assert st.thermal_value_iqk == 38
    assert st.is_iqk_in_progress is False


def test_callback_below_threshold_skips_iqk(mocker):
    mocker.patch(f"{_RF}.phy_query_rf_reg", return_value=29 + 7)
    mocker.patch("wifit3.chips.rtl8188eus_dkms.powertrack._apply_mix_mode")
    cal = mocker.patch("wifit3.chips.rtl8188eus_dkms.iqk.phy_iq_calibrate_8188e")
    st = _state()
    powertrack._callback(RegTx(), st)
    cal.assert_not_called()
    assert st.thermal_value_iqk == 29
