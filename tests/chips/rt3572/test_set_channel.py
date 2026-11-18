from unittest.mock import MagicMock

import pytest
import usb.core

import wifit3.chips.rt3572.driver as drv
from wifit3.errors import SetChannelError


def _connected_driver(monkeypatch) -> drv.RT3572Driver:
    d = drv.RT3572Driver(MagicMock())
    d.chip_id = MagicMock()
    monkeypatch.setattr(d, "_channel_kwargs", lambda _channel: {})
    return d


async def test_set_channel_propagates_a_usb_error(monkeypatch):
    def failing_set_channel(*_a, **_kw):
        raise usb.core.USBError("timeout")

    monkeypatch.setattr(drv, "hop_channel", failing_set_channel)
    d = _connected_driver(monkeypatch)
    with pytest.raises(usb.core.USBError):
        await d.set_channel(6)
    assert d.current_channel != 6


async def test_set_channel_rejects_an_unsupported_channel_before_any_write(monkeypatch):
    tuned = []
    monkeypatch.setattr(drv, "hop_channel", lambda *a, **kw: tuned.append(a))
    d = _connected_driver(monkeypatch)
    with pytest.raises(SetChannelError):
        await d.set_channel(52)
    assert tuned == []
