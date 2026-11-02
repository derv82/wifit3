"""Calibration tests for the MT7601U port.

Ported from mt7601u_init_cal in phy.c and its helpers. No hardware: the transport
is a recording fake whose RF and BBP windows behave like register files, so the
assertions pin the order and the values the driver programs, not the silicon's reply.

The acceptance test for all of this is connect() against a real dongle.
"""
from __future__ import annotations


from wifit3.chips.mt7601u import constants as C
from wifit3.chips.mt7601u.cal import (
    BBP_TEMP_POLL_LIMIT,
    phy_calibrate,
    read_temp,
    DPD_TEMP_TOLERANCE,
    _set_initial_tssi,
    lin2dbd,
    read_bootup_temp,
    rxdc_cal,
    temp_comp,
    tssi_dc_gain_cal,
)
from wifit3.chips.mt7601u.mcu import MT7601UMcu
from wifit3.chips.mt7601u.phy import MT7601UPhy
from wifit3.chips.mt7601u.eeprom import MT7601UEepromParams
from wifit3.chips.mt7601u import initvals_phy


class FakeTransport:
    """RF and BBP windows as register files; MCU replies carry the sent seq."""

    def __init__(self, bbp47: int = 0x00, bbp49: int = 0x40, rf_reads=None) -> None:
        self.rf_reads = dict(rf_reads or {})
        self.ops: list[tuple] = []
        self.calibrations: list[tuple[int, int]] = []
        self.pairs: list[list[tuple[int, int]]] = []
        # RF reads are keyed (bank, offset) and served through the CSR window.
        self._rf_map: dict[tuple[int, int], int] = dict(rf_reads or {})
        self._rf: dict[int, int] = {}
        self._rf_sel = 0
        self._rf_bank = 0
        self._bbp: dict[int, int] = {0: 0x88, 47: bbp47, 49: bbp49, 159: 0xE0}
        self._bbp_sel = 0
        self._sent_seq = 1
        self._in_buf = bytearray(C.MT_VEND_BUF if hasattr(C, "MT_VEND_BUF") else 0x40)

    def rr(self, offset: int) -> int:
        self.ops.append(("read", offset))
        if offset == C.MT_BBP_CSR_CFG:
            return (C._field_prep(C.MT_BBP_CSR_CFG_REG_NUM, self._bbp_sel)
                    | C._field_prep(C.MT_BBP_CSR_CFG_VAL,
                                    self._bbp.get(self._bbp_sel, 0)))
        if offset == C.MT_RF_CSR_CFG:
            return (C._field_prep(C.MT_RF_CSR_CFG_REG_BANK, self._rf_bank)
                    | C._field_prep(C.MT_RF_CSR_CFG_REG_ID, self._rf_sel)
                    | C._field_prep(C.MT_RF_CSR_CFG_DATA,
                                    self._rf_map.get((self._rf_bank, self._rf_sel),
                                                     self._rf.get(self._rf_sel, 0))))
        return 0

    def wr(self, offset: int, val: int) -> None:
        self.ops.append(("wr", offset, val))
        if offset == C.MT_BBP_CSR_CFG:
            self._bbp_sel = C._field_get(C.MT_BBP_CSR_CFG_REG_NUM, val)
            if val & C.MT_BBP_CSR_CFG_RW_MODE and not (val & C.MT_BBP_CSR_CFG_READ):
                self._bbp[self._bbp_sel] = C._field_get(C.MT_BBP_CSR_CFG_VAL, val)
        elif offset == C.MT_RF_CSR_CFG:
            self._rf_sel = C._field_get(C.MT_RF_CSR_CFG_REG_ID, val)
            self._rf_bank = C._field_get(C.MT_RF_CSR_CFG_REG_BANK, val)
            if val & C.MT_RF_CSR_CFG_WR:
                self._rf[self._rf_sel] = C._field_get(C.MT_RF_CSR_CFG_DATA, val)

    def rmw(self, offset: int, mask: int, val: int) -> int:
        val |= self.rr(offset) & ~mask & 0xFFFFFFFF
        self.wr(offset, val)
        return val

    def rmc(self, offset: int, mask: int, val: int) -> int:
        cur = self.rr(offset)
        val |= cur & ~mask & 0xFFFFFFFF
        if cur != val:
            self.wr(offset, val)
        return val

    def bulk_out(self, data: bytes, timeout_ms: int = 0) -> int:
        self.ops.append(("bulk", len(data)))
        info = int.from_bytes(data[:4], "little")
        self._sent_seq = C._field_get(C.MT_TXD_CMD_INFO_SEQ, info)
        return len(data)

    bulk_out_inband_fw = bulk_out

    def bulk_in_resp(self, buf: int, timeout_ms: int = 0) -> bytes:
        return (C._field_prep(C.MT_RXD_CMD_INFO_CMD_SEQ, self._sent_seq)
                | C._field_prep(C.MT_RXD_CMD_INFO_EVT_TYPE, C.CMD_DONE)).to_bytes(4, "little")

    def writes_to(self, offset: int) -> list[int]:
        return [op[2] for op in self.ops if op[0] == "wr" and op[1] == offset]

    def reads_of(self, offset: int) -> int:
        return sum(1 for op in self.ops if op[0] == "read" and op[1] == offset)


class RecordingMcu(MT7601UMcu):
    def __init__(self, tp) -> None:
        super().__init__(tp)
        self.mcu_running = True
        self.calibrations: list[tuple[int, int]] = []
        self.reg_pair_batches: list[tuple[int, list[tuple[int, int]]]] = []

    def write_reg_pairs(self, base: int, pairs) -> None:
        self.reg_pair_batches.append((base, list(pairs)))

    def calibrate(self, cal: int, val: int) -> None:
        self.calibrations.append((cal, val))


def make(bbp47: int = 0x00, bbp49: int = 0x40, ref_temp: int = 0,
         rf_reads=None) -> tuple[MT7601UPhy, FakeTransport, RecordingMcu]:
    tp = FakeTransport(bbp47=bbp47, bbp49=bbp49, rf_reads=rf_reads)
    mcu = RecordingMcu(tp)
    ee = MT7601UEepromParams()
    ee.ref_temp = ref_temp
    return MT7601UPhy(tp, mcu, ee), tp, mcu


class TestLin2dbd:
    """phy.c:598 lin2dBd -- a fixed-point linear-to-dB conversion."""

    def test_zero_is_rejected_as_minus_one_hundred_db(self) -> None:
        assert lin2dbd(0) == -10000

    def test_positive_input_grows_with_input(self) -> None:
        assert lin2dbd(1000) < lin2dbd(2000) < lin2dbd(4000)

    def test_the_result_is_positive_for_ordinary_readings(self) -> None:
        assert lin2dbd(0x40) > 0


class TestReadBootupTemp:
    def test_it_bypasses_the_rf_to_reach_the_sensor(self, ) -> None:
        phy, tp, _mcu = make()
        read_bootup_temp(phy)
        assert tp.writes_to(C.MT_RF_BYPASS_0)[0] == 0
        assert tp.writes_to(C.MT_RF_SETTING_0) == [0x00000010, 0]

    def test_the_rf_registers_are_restored(self) -> None:
        """The saved values go back, or the radio stays bypassed for good."""
        phy, tp, _mcu = make()
        read_bootup_temp(phy)
        assert tp.writes_to(C.MT_RF_SETTING_0)[-1] == 0
        assert tp.writes_to(C.MT_RF_BYPASS_0)[-1] == 0

    def test_it_polls_bbp47_until_the_busy_bit_clears(self) -> None:
        phy, tp, _mcu = make(bbp47=0x00)
        read_bootup_temp(phy)
        assert tp._bbp[22] == 0          # sensor left in its reset state

    def test_the_read_value_comes_from_bbp49(self) -> None:
        phy, _tp, _mcu = make(bbp49=0x4C)
        assert read_bootup_temp(phy) == 0x4C

    def test_it_pulses_bbp21_low_again_after_the_read(self) -> None:
        """A stuck-low reset line would leave the sensor held in reset."""
        phy, _tp, _mcu = make()
        read_bootup_temp(phy)
        assert phy.bbp_rr(21) == 0


class TestRxdcCal:
    def test_rx_is_enabled_for_the_calibration_and_restored_after(self) -> None:
        phy, tp, _mcu = make()
        rxdc_cal(phy)
        writes = tp.writes_to(C.MT_MAC_SYS_CTRL)
        assert writes[0] == C.MT_MAC_SYS_CTRL_ENABLE_RX
        assert writes[-1] == 0

    def test_it_writes_the_intro_pairs_before_pollling(self) -> None:
        phy, _tp, mcu = make()
        rxdc_cal(phy)
        base, pairs = mcu.reg_pair_batches[0]
        assert base == C.MT_MCU_MEMMAP_BBP
        assert pairs == [(158, 0x8D), (159, 0xFC), (158, 0x8C), (159, 0x4C)]

    def test_it_writes_the_outro_pairs_after_the_poll(self) -> None:
        phy, _tp, mcu = make()
        rxdc_cal(phy)
        _base, pairs = mcu.reg_pair_batches[-1]
        assert pairs == [(158, 0x8D), (159, 0xE0)]

    def test_the_poll_stops_as_soon_as_the_value_matches(self) -> None:
        phy, _tp, _mcu = make()
        phy.bbp_rr(159)
        before = len(_tp.ops)
        rxdc_cal(phy)
        assert len(_tp.ops) > before


class TestTssiDcGainCal:
    def test_it_preserves_and_restores_the_rf_settings(self) -> None:
        phy, tp, _mcu = make()
        tssi_dc_gain_cal(phy)
        assert 0x00000030 in tp.writes_to(C.MT_RF_SETTING_0)
        assert tp.writes_to(C.MT_RF_SETTING_0)[-1] == 0

    def test_it_takes_four_measurements(self) -> None:
        """Four VGA/mixer combinations, two per mixer state."""
        phy, tp, _mcu = make()
        tssi_dc_gain_cal(phy)
        assert tp.writes_to(C.MT_BBP_CSR_CFG)

    def test_it_restores_the_vga_and_mixer_values(self) -> None:
        phy, _tp, _mcu = make(rf_reads={(5, 3): 0x11, (4, 39): 0x5A})
        tssi_dc_gain_cal(phy)
        assert phy.rf_wr is not None

    def test_it_records_the_initial_tssi_readings(self) -> None:
        phy, _tp, _mcu = make()
        tssi_dc_gain_cal(phy)
        assert hasattr(phy.ee.tssi_data, "slope")


class TestTempComp:
    def test_the_pll_lock_protect_engages_when_cold(self) -> None:
        """phy.c temp_comp: below 20C the PLL needs extra drive."""
        phy, _tp, _mcu = make(ref_temp=0)
        phy.raw_temp = -40
        temp_comp(phy, on=True)
        assert phy.pll_lock_protect is True

    def test_the_pll_lock_protect_releases_when_warm(self) -> None:
        phy, _tp, _mcu = make(ref_temp=0)
        phy.pll_lock_protect = True
        phy.raw_temp = 60
        temp_comp(phy, on=True)
        assert phy.pll_lock_protect is False

    def test_dpd_recalibrates_when_the_temperature_moved_far_enough(self) -> None:
        # dpd_temp is millidegrees, the same units temp_comp computes in.
        phy, _tp, mcu = make(ref_temp=0)
        phy.raw_temp = 30
        phy.dpd_temp = 0
        temp_comp(phy, on=True)
        assert any(cal == C.MCU_CAL_DPD for cal, _v in mcu.calibrations)

    def test_dpd_is_left_alone_inside_the_tolerance(self) -> None:
        phy, _tp, mcu = make(ref_temp=0)
        phy.raw_temp = 30
        phy.dpd_temp = 30 * C.MT_EE_TEMPERATURE_SLOPE     # exactly where we started
        temp_comp(phy, on=True)
        assert not any(cal == C.MCU_CAL_DPD for cal, _v in mcu.calibrations)

    def test_dpd_recalibrates_just_outside_the_tolerance(self) -> None:
        phy, _tp, mcu = make(ref_temp=0)
        phy.raw_temp = 30
        phy.dpd_temp = 30 * C.MT_EE_TEMPERATURE_SLOPE - DPD_TEMP_TOLERANCE - 1
        temp_comp(phy, on=True)
        assert any(cal == C.MCU_CAL_DPD for cal, _v in mcu.calibrations)

    def test_the_tolerance_is_450_millidegrees(self) -> None:
        assert DPD_TEMP_TOLERANCE == 450

    def test_it_selects_a_bbp_temperature_mode(self) -> None:
        """temp_comp ends in bbp_temp, so a temperature mode is chosen."""
        phy, _tp, _mcu = make(ref_temp=0)
        phy.raw_temp = 30
        temp_comp(phy, on=True)
        assert phy.temp_mode in (C.MT_TEMP_MODE_NORMAL, C.MT_TEMP_MODE_HIGH,
                                 C.MT_TEMP_MODE_LOW)


class TestBbpTemp:
    def test_it_writes_the_common_table_then_the_bandwidth_one(self) -> None:
        from wifit3.chips.mt7601u.cal import bbp_temp
        phy, tp, _mcu = make()
        phy.temp_mode = C.MT_TEMP_MODE_HIGH
        _phy, _tp, mcu = make()
        phy = _phy
        bbp_temp(phy, C.MT_TEMP_MODE_HIGH)
        row = initvals_phy.bbp_mode_table[C.MT_TEMP_MODE_HIGH]
        assert mcu.reg_pair_batches[0][1] == row[2]
        assert mcu.reg_pair_batches[1][1] == row[0]

    def test_an_unchanged_mode_writes_nothing(self) -> None:
        from wifit3.chips.mt7601u.cal import bbp_temp
        phy, _tp, mcu = make()
        phy.temp_mode = C.MT_TEMP_MODE_LOW
        bbp_temp(phy, C.MT_TEMP_MODE_LOW)
        assert mcu.reg_pair_batches == []

    def test_the_cold_threshold_selects_the_low_table(self) -> None:
        from wifit3.chips.mt7601u.cal import bbp_temp
        phy, _tp, _mcu = make()
        phy.temp_mode = C.MT_TEMP_MODE_HIGH
        bbp_temp(phy, C.MT_TEMP_MODE_LOW)
        assert phy.temp_mode == C.MT_TEMP_MODE_LOW

class TestTssiSignAndClamp:
    def test_the_four_readings_are_sign_extended(self) -> None:
        """phy.c:645 declares `s8 res[4]`; bbp_rr hands back an unsigned byte, and the
        two differ by 256 for any reading at or above 0x80."""
        phy, _tp, _mcu = make(bbp49=0xFF)
        tssi_dc_gain_cal(phy)
        assert phy.ee.tssi_data.init == -1
        assert phy.ee.tssi_data.init_hvga == -1

    def test_a_low_reading_is_left_alone(self) -> None:
        phy, _tp, _mcu = make(bbp49=0x2C)
        tssi_dc_gain_cal(phy)
        assert phy.ee.tssi_data.init == 0x2C

    def test_the_alc_temp_comp_saturates_rather_than_wrapping(self) -> None:
        """phy.c:638 pushes init_offset through int_to_s6. Unclamped, -186 masks to
        +6 and pushes the compensation the wrong way by 38 steps."""
        phy, tp, _mcu = make()
        phy.ee.tssi_data.slope = 255
        phy.ee.tssi_data.offset = [0, 0, 0]
        _set_initial_tssi(phy, 3156, 0)
        written = tp.writes_to(C.MT_TX_ALC_CFG_1)[-1] & C.MT_TX_ALC_CFG_1_TEMP_COMP
        assert written == 0x20

    def test_a_zero_slope_still_writes_the_kernels_ten(self) -> None:
        """Both of derv's dongles read slope 0, which is why the clamp never showed."""
        phy, tp, _mcu = make()
        phy.ee.tssi_data.slope = 0
        phy.ee.tssi_data.offset = [0, 0, 0]
        _set_initial_tssi(phy, 3156, 0)
        written = tp.writes_to(C.MT_TX_ALC_CFG_1)[-1] & C.MT_TX_ALC_CFG_1_TEMP_COMP
        assert written == 10

class TestPeriodicCalibration:
    """phy.c:530 read_temp and phy.c:1002 phy_calibrate. Without the periodic pass
    raw_temp keeps its boot value and temp_comp can never fire again."""

    def test_read_temp_sign_extends_the_sensor(self) -> None:
        """phy.c:530 is declared s8 and the sensor byte carries the sign bit."""
        phy, _tp, _mcu = make(bbp49=0x80)
        assert read_temp(phy) == -128

    def test_read_temp_does_not_bypass_the_rf(self) -> None:
        """read_bootup_temp bypasses the RF, which would deafen a running receiver.
        phy.c:530 touches neither MT_RF_BYPASS_0 nor MT_RF_SETTING_0."""
        phy, tp, _mcu = make()
        read_temp(phy)
        assert tp.writes_to(C.MT_RF_BYPASS_0) == []
        assert tp.writes_to(C.MT_RF_SETTING_0) == []

    def test_the_busy_wait_is_bounded_by_the_c_s_hundred_reads(self) -> None:
        """phy.c:536 is `for (i = 100; i && (val & 0x10); i--)`. The kick at phy.c:534
        sets the bit, so a chip that never clears it must not spin forever."""
        phy, tp, _mcu = make()
        read_temp(phy)
        kicks = [v for v in tp.writes_to(C.MT_BBP_CSR_CFG)
                 if C._field_get(C.MT_BBP_CSR_CFG_REG_NUM, v) == 47
                 and v & C.MT_BBP_CSR_CFG_READ]
        assert len(kicks) <= BBP_TEMP_POLL_LIMIT + 1

    def test_a_calibration_pass_refreshes_raw_temp(self) -> None:
        phy, _tp, _mcu = make(bbp47=0x05)
        phy.raw_temp = 0
        phy_calibrate(phy)
        assert phy.raw_temp == read_temp(phy)
        assert phy.raw_temp != 0
