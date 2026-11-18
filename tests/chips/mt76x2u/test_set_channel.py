from unittest.mock import MagicMock

import pytest

import wifit3.chips.mt76x2u.driver as driver_mod
from wifit3.chips.mt76x2u.driver import MT76x2UDriver
from wifit3.errors import SetChannelError


async def test_set_channel_raises_when_the_mcu_switch_fails(monkeypatch):
    async def failing_set_channel_20mhz(*_a, **_kw):
        return False

    monkeypatch.setattr(driver_mod, "set_channel_20mhz", failing_set_channel_20mhz)
    monkeypatch.setattr(driver_mod, "read_rate_power", lambda *_a, **_kw: None)
    monkeypatch.setattr(driver_mod, "read_power_info", lambda *_a, **_kw: None)
    d = MT76x2UDriver(MagicMock(), MagicMock())
    d.asic_rev = 0
    d.chainmask = 0
    monkeypatch.setattr(d, "_ext_pa_for", lambda _channel: False)
    monkeypatch.setattr(d, "_high_gain_for", lambda _channel: False)
    with pytest.raises(SetChannelError):
        await d.set_channel(6)


async def test_set_channel_rejects_an_unsupported_channel():
    d = MT76x2UDriver(MagicMock(), MagicMock())
    with pytest.raises(SetChannelError):
        await d.set_channel(15)
