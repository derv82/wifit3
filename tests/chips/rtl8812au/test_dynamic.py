"""RTL8812AU DIG (Dynamic Initial Gain) watchdog.

Coverage-path port of rtw_phy_dig (raise IGI on high false-alarm, lower it on
quiet) + rtw88xxa_false_alarm_statistics (sum FA, then reset the counters).
"""
from unittest.mock import MagicMock

from wifit3.chips.rtl8812au import constants as C
from wifit3.chips.rtl8812au import dynamic


def test_dig_init_seeds_from_live_igi_without_writing():
    t = MagicMock()
    t.read32.return_value = 0x24
    state = dynamic.dig_init(t)
    assert state.igi == 0x24 and state.history[0] == 0x24
    t.read32.assert_called_once_with(C.REG_RXIGI_A)
    t.write32_mask.assert_not_called()   # kernel only *reads* dig[0] at init


def test_dig_step_high_fa_raises_igi_on_both_paths():
    t = MagicMock()
    state = dynamic.DigState(igi=0x20, history=[0x20] * 4)
    dynamic.dig_step(t, state, fa_cnt=9000)   # > EXTRA_HIGH(5000) → +4-2 = +2
    assert state.igi == 0x22
    t.write32_mask.assert_any_call(C.REG_RXIGI_A, C.DIG_IGI_MASK, 0x22)
    t.write32_mask.assert_any_call(C.REG_RXIGI_B, C.DIG_IGI_MASK, 0x22)


def test_dig_step_quiet_band_lowers_igi():
    t = MagicMock()
    state = dynamic.DigState(igi=0x22, history=[0x22] * 4)
    dynamic.dig_step(t, state, fa_cnt=0)       # < LOW → just the -2
    assert state.igi == 0x20


def test_dig_step_clamps_at_min_and_does_not_write_when_unchanged():
    t = MagicMock()
    state = dynamic.DigState(igi=C.DIG_CVRG_MIN, history=[C.DIG_CVRG_MIN] * 4)
    dynamic.dig_step(t, state, fa_cnt=0)       # would go below MIN → clamp
    assert state.igi == C.DIG_CVRG_MIN
    t.write32_mask.assert_not_called()


def test_dig_step_clamps_at_upper_bound():
    t = MagicMock()
    upper = min(C.DIG_CVRG_MAX, C.DIG_CVRG_MIN + C.DIG_RSSI_GAIN_OFFSET)
    state = dynamic.DigState(igi=upper, history=[upper] * 4)
    dynamic.dig_step(t, state, fa_cnt=9000)    # would go above → clamp
    assert state.igi == upper


def test_read_total_fa_cnt_sums_cck_when_enabled_and_resets():
    t = MagicMock()
    t.read32.side_effect = lambda a: C.BIT_RXPSEL_CCK_EN if a == C.REG_RXPSEL else 0
    t.read16.side_effect = lambda a: {C.REG_FA_OFDM: 100, C.REG_FA_CCK: 5}[a]
    assert dynamic.read_total_fa_cnt(t) == 105
    touched = ({c.args[0] for c in t.write32_set.call_args_list}
               | {c.args[0] for c in t.write32_clr.call_args_list})
    assert {C.REG_FAS, C.REG_CCK0_FAREPORT, C.REG_CNTRST} <= touched


def test_read_total_fa_cnt_excludes_cck_when_disabled():
    t = MagicMock()
    t.read32.side_effect = lambda a: 0         # CCK demod off
    t.read16.side_effect = lambda a: {C.REG_FA_OFDM: 100, C.REG_FA_CCK: 5}[a]
    assert dynamic.read_total_fa_cnt(t) == 100


# --- thermal pwr-track / LCK (VCO re-lock) ---------------------------------

def test_thermal_ewma_matches_kernel_formula():
    # DECLARE_EWMA(thermal, 10, 4): first add seeds, then 1/16 weight per sample.
    e = dynamic._ThermalEwma()
    e.add(40)
    assert e.read() == 40
    e.add(40)
    assert e.read() == 40
    e.add(8)                                   # internal=(40960*15+8192)>>4=38912
    assert e.read() == 38                       # 38912 >> 10


def test_pwrtrack_init_uses_efuse_cold_reference(monkeypatch):
    # Warm chip (live=40), cold efuse cal (28) → reference is the cold temp, so a
    # warm-started chip already shows drift and LCK can fire promptly.
    monkeypatch.setattr(dynamic, "read_thermal", lambda tr: 40)
    state = dynamic.pwrtrack_init(MagicMock(), efuse_thermal=28)
    assert state.thermal_meter_k == 28        # cold efuse reference
    assert state.avg.read() == 40             # avg seeded from the live reading


def test_pwrtrack_init_falls_back_when_efuse_uncalibrated(monkeypatch):
    monkeypatch.setattr(dynamic, "read_thermal", lambda tr: 33)
    state = dynamic.pwrtrack_init(MagicMock(), efuse_thermal=0xFF)
    assert state.thermal_meter_k == 33        # fell back to the live reading


def test_pwrtrack_step_runs_lck_on_fixed_cadence(monkeypatch):
    # LCK is decoupled from thermal drift: it fires every LCK_PERIOD_TICKS ticks
    # regardless of temperature (a constant 35 here would never trip need_iqk).
    t = MagicMock()
    state = dynamic.PwrTrackState()
    monkeypatch.setattr(dynamic, "read_thermal", lambda tr: 35)
    called = []
    monkeypatch.setattr(dynamic, "do_lck", lambda tr: called.append(True))

    for _ in range(C.LCK_PERIOD_TICKS - 1):
        assert dynamic.pwrtrack_step(t, state) is False
    assert not called

    assert dynamic.pwrtrack_step(t, state) is True   # cadence tick
    assert called and state.ticks_since_lck == 0     # counter reset


def test_do_lck_runs_lc_calibration_sequence(monkeypatch):
    t = MagicMock()
    t.read32.return_value = 0                   # cont_tx false → TXPAUSE toggled
    writes = []
    monkeypatch.setattr(
        dynamic, "write_rf_masked",
        lambda tr, addr, mask, data, **kw: writes.append((addr, mask, data)))
    # full-mask read returns saved RF18; the busy-flag poll returns 0 (done).
    monkeypatch.setattr(
        dynamic, "read_rf",
        lambda tr, addr, mask, **kw: 0x12345 if mask == dynamic.RFREG_MASK else 0)
    monkeypatch.setattr(dynamic.time, "sleep", lambda s: None)

    dynamic.do_lck(t)

    assert (C.RF_LCK, C.RF_LCK_EN, 1) in writes          # enter LCK
    assert (C.RF_CFGCH, C.RF_CFGCH_LCK_TRIG, 1) in writes  # trigger cal
    assert (C.RF_LCK, C.RF_LCK_EN, 0) in writes          # exit LCK
    assert (C.RF_CFGCH, dynamic.RFREG_MASK, 0x12345) in writes  # restore RF18
    t.write8.assert_any_call(C.REG_TXPAUSE, 0xFF)        # pause TX
    t.write8.assert_any_call(C.REG_TXPAUSE, 0)           # un-pause
