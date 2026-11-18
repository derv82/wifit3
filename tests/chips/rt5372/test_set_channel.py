import pytest
import usb.core

from wifit3.chips.rt5372 import chan, mac


def test_set_channel_restarts_rx_when_the_tune_raises(monkeypatch):
    """rt2x00mac_config always turns RX back on (rt2x00mac.c:349)."""
    rx_queue = []

    def failing_update_survey(_t):
        raise usb.core.USBError("timeout")

    monkeypatch.setattr(mac, "stop_queue_rx", lambda _t: rx_queue.append("stop"))
    monkeypatch.setattr(mac, "start_queue_rx", lambda _t: rx_queue.append("start"))
    monkeypatch.setattr(chan, "config_lna_gain", lambda _ev, _channel: 0)
    monkeypatch.setattr(chan, "update_survey", failing_update_survey)
    with pytest.raises(usb.core.USBError):
        chan.set_channel(None, None, None, None, 6)
    assert rx_queue == ["stop", "start"]
