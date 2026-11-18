from wifit3.chips.rt3572 import chan


def test_hop_channel_stops_rx_banks_the_survey_tunes_then_restarts_rx(monkeypatch):
    """rt2x00mac_config: stop RX, rt2800_config (update_survey, config_channel), start RX
    (rt2x00mac.c:326-349, rt2800lib.c:5691-5694)."""
    calls = []
    monkeypatch.setattr(chan, "toggle_rx", lambda _t, enable: calls.append(("toggle_rx", enable)))
    monkeypatch.setattr(chan, "update_survey", lambda _t: calls.append(("update_survey",)))
    monkeypatch.setattr(chan, "set_channel", lambda _t, _sid, channel, **_kw: calls.append(("set_channel", channel)))
    chan.hop_channel(None, 0, 6)
    assert calls == [("toggle_rx", False), ("update_survey",), ("set_channel", 6), ("toggle_rx", True)]
