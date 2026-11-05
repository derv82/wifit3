from unittest.mock import MagicMock, call

import pytest

from wifit3.chips.rtl8822cu import watchdog as wd


@pytest.mark.parametrize("tx_bytes,rx_bytes,low", [
    (0, 0, True),
    (262_143, 0, True),
    (262_144, 0, False),
    (0, 262_143, True),
    (0, 262_144, False),
    (140_000, 140_000, True),
])
def test_aggregation_uses_separate_vendor_mbps_counters(tx_bytes, rx_bytes, low):
    st = wd.WatchdogState()
    assert wd._traffic_statistics(st, tx_bytes, rx_bytes) is low


def test_traffic_statistics_uses_interval_deltas_and_independent_phydm_smoothing():
    st = wd.WatchdogState()
    assert wd._traffic_statistics(st, 262_144, 0) is False
    assert (st.tx_tp, st.rx_tp) == (0, 0)
    assert wd._traffic_statistics(st, 786_432, 1_048_576) is False
    assert (st.tx_tp, st.rx_tp) == (1, 2)
    assert wd._traffic_statistics(st, 786_432, 1_048_576) is True
    assert (st.tx_tp, st.rx_tp) == (0, 1)
    assert wd._traffic_statistics(st, 786_432, 1_048_576) is True
    assert (st.tx_tp, st.rx_tp) == (0, 0)


def test_phydm_counter_reset_does_not_underflow_its_delta():
    st = wd.WatchdogState(last_tx_bytes=100, last_tx_ok_cnt=100, tx_tp=2)
    wd._traffic_statistics(st, 50, 0)
    assert st.last_tx_ok_cnt == 50
    assert st.tx_tp == 1


@pytest.mark.parametrize("low,threshold", [(True, 0x0101), (False, 0x2005)])
def test_usb_rx_aggregation_register_order_and_values(low, threshold):
    t = MagicMock()
    t.read8.side_effect = [0xD3, 0x21]
    t.read32.return_value = 0x01234567
    wd._usb_rx_agg(t, low_traffic=low)
    assert t.mock_calls == [
        call.read8(0x0283), call.read8(0x010C), call.read32(0x0280),
        call.write32(0x0280, 0x21234567), call.write8(0x010C, 0x25),
        call.write8(0x0283, 0x53), call.write16(0x0280, threshold),
    ]


def test_htstf_no_ndp_disables_on_fourth_busy_pass_and_recovers():
    st = wd.WatchdogState(tx_tp=1)
    t = MagicMock()
    t.read32.return_value = 0
    for expected in (1, 2, 3):
        wd._hw_setting(t, st)
        assert st.no_ndp_cnts == expected
        assert call.write32(0x08A0, 0) not in t.mock_calls
    wd._hw_setting(t, st)
    assert st.no_ndp_cnts == 3
    assert t.mock_calls[-2:] == [call.read32(0x08A0), call.write32(0x08A0, 0)]
    t.read32.side_effect = lambda addr: 1 if addr == 0x2DBC else 0
    wd._hw_setting(t, st)
    assert st.ndp_cnt_pre == 1
    assert st.no_ndp_cnts == 0
    assert t.mock_calls[-2:] == [call.read32(0x08A0), call.write32(0x08A0, 4)]


def test_htstf_idle_and_ndp_wrap_enable_agc():
    st = wd.WatchdogState(ndp_cnt_pre=255, no_ndp_cnts=3, tx_tp=1)
    t = MagicMock()
    t.read32.return_value = 0
    wd._hw_setting(t, st)
    assert st.ndp_cnt_pre == 0 and st.no_ndp_cnts == 0
    assert t.mock_calls == [call.read32(0x1C3C), call.write32(0x1C3C, 0x51F00),
                            call.read32(0x2DBC), call.read32(0x08A0), call.write32(0x08A0, 4)]
    st.no_ndp_cnts = 3
    st.tx_tp = 0
    t.reset_mock()
    wd._hw_setting(t, st)
    assert st.no_ndp_cnts == 0
    assert t.mock_calls[-1] == call.write32(0x08A0, 4)


@pytest.mark.parametrize("disabled,priority", [(True, 0), (False, 1), (False, 2)])
def test_htstf_skips_when_disabled_or_debug_port_busy(disabled, priority):
    st = wd.WatchdogState(bhtstfdisabled=disabled, pre_dbg_priority=priority)
    t = MagicMock()
    wd._hw_setting(t, st)
    t.assert_not_called()
    assert t.mock_calls == []
    assert st.pre_dbg_priority == priority


def test_htstf_releases_debug_priority_after_read_failure():
    st = wd.WatchdogState()
    t = MagicMock()
    t.read32.side_effect = [0, OSError("USB read failed")]
    with pytest.raises(OSError, match="USB read failed"):
        wd._hw_setting(t, st)
    assert st.pre_dbg_priority == 0


def test_tick_applies_accounted_traffic_before_aggregation_and_htstf(monkeypatch):
    st = wd.WatchdogState(first_tick=False)
    t = MagicMock()
    events = []
    monkeypatch.setattr(wd, "_sreset_check", lambda t: events.append("sreset"))
    monkeypatch.setattr(wd, "_usb_rx_agg", lambda t, *, low_traffic: events.append(("agg", low_traffic)))
    monkeypatch.setattr(wd, "_hw_setting", lambda t, state: events.append(("hw", state.tx_tp)))
    monkeypatch.setattr(wd, "_env_mntr_result", lambda *args: None)
    monkeypatch.setattr(wd, "_fa_cnt_statistics", lambda *args: (0, 0))
    for name in ("_dig", "_cck_pd_th", "_adaptivity", "_halrf_thermal", "_dyn_bw_indication", "_env_mntr_set"):
        monkeypatch.setattr(wd, name, lambda *args: None)

    wd.tick(t, st, 1, tx_bytes=524_288)

    assert events == ["sreset", ("agg", False), ("hw", 1)]
    t.read32.assert_called_once_with(0x0608)


def test_ndp_uses_only_low_byte():
    st = wd.WatchdogState(ndp_cnt_pre=0x34, tx_tp=1)
    t = MagicMock()
    t.read32.side_effect = lambda addr: 0xABCD1234 if addr == 0x2DBC else 0
    wd._hw_setting(t, st)
    assert st.ndp_cnt_pre == 0x34
    assert st.no_ndp_cnts == 1


@pytest.mark.parametrize("count,expected", [
    (1999, 0x1E), (2000, 0x20), (4000, 0x20),
    (4001, 0x21), (5000, 0x21), (5001, 0x22),
])
def test_dig_no_link_false_alarm_boundaries(count, expected):
    st = wd.WatchdogState(cur_ig_value=0x20)
    t = MagicMock()
    t.read32.return_value = 0
    wd._dig(t, st, count)
    assert st.cur_ig_value == expected
    if expected == 0x20:
        assert t.mock_calls == []
    else:
        assert t.mock_calls == [call.read32(0x1D70), call.write32(0x1D70, expected),
                                call.read32(0x1D70), call.write32(0x1D70, expected << 8)]


@pytest.mark.parametrize("initial,count,expected,writes", [
    (0x1C, 0, 0x1C, False), (0x1D, 0, 0x1C, True),
    (0x29, 5001, 0x2A, True), (0x2A, 5001, 0x2A, False),
])
def test_dig_no_link_clamps(initial, count, expected, writes):
    st = wd.WatchdogState(cur_ig_value=initial)
    t = MagicMock()
    wd._dig(t, st, count)
    assert st.cur_ig_value == expected
    assert bool(t.write32.call_count) is writes
