import pytest
import usb.core

from wifit3.chips.rt5572 import chan, mac


def test_hop_channel_restarts_rx_when_the_tune_raises(monkeypatch):
    """rt2x00mac_config always turns RX back on (rt2x00mac.c:349)."""
    rx_toggles = []

    def failing_reconfig_channel(*_a, **_kw):
        raise usb.core.USBError("timeout")

    monkeypatch.setattr(mac, "toggle_rx", lambda _t, enable: rx_toggles.append(enable))
    monkeypatch.setattr(mac, "update_survey", lambda _t: None)
    monkeypatch.setattr(chan, "reconfig_channel", failing_reconfig_channel)
    with pytest.raises(usb.core.USBError):
        chan.hop_channel(None, 0, 6)
    assert rx_toggles == [False, True]
