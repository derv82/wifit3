from unittest.mock import MagicMock

import pytest

import wifit3.chips.mt76x0u.driver as driver_mod
from wifit3.chips.mt76x0u.driver import MT76x0UDriver
from wifit3.chips.mt76x0u.mcu import MCUError


def test_a_failed_tune_does_not_skip_the_retune_to_the_previous_channel(monkeypatch):
    tuned = []

    def recording_set_channel_20mhz(_t, _mcu, channel, **_kw):
        tuned.append(channel)

    def failing_set_channel_20mhz(*_a, **_kw):
        raise MCUError("timeout")

    d = MT76x0UDriver(MagicMock(), MagicMock())
    monkeypatch.setattr(driver_mod, "set_channel_20mhz", recording_set_channel_20mhz)
    d._set_channel_sync(1)
    monkeypatch.setattr(driver_mod, "set_channel_20mhz", failing_set_channel_20mhz)
    with pytest.raises(MCUError):
        d._set_channel_sync(36)
    assert d.current_channel is None

    tuned.clear()
    monkeypatch.setattr(driver_mod, "set_channel_20mhz", recording_set_channel_20mhz)
    d._set_channel_sync(1)
    assert tuned == [1, 1]
    assert d.current_channel == 1
