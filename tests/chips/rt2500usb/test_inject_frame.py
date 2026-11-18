import errno
import logging
from unittest.mock import MagicMock

import pytest
import usb.core

import wifit3.chips.rt2500usb.driver as drv
from wifit3.errors import is_device_gone


def _driver(monkeypatch, send) -> drv.RT2500USBDriver:
    monkeypatch.setattr(drv, "_tx_inject", send)
    d = drv.RT2500USBDriver(MagicMock())
    d._bulk_out_ep = 0x01
    return d


def _failing_send(error):
    def send(*_a, **_kw):
        raise error
    return send


async def test_a_failed_bulk_out_drops_the_frame(monkeypatch, caplog):
    """rt2x00usb_kick_tx_entry marks a failed URB ENTRY_DATA_IO_FAILED and carries on (rt2x00usb.c:330-336)."""
    d = _driver(monkeypatch, _failing_send(usb.core.USBError("pipe", errno=errno.EPIPE)))
    await d._inject_frame(bytes(24))
    assert d.transport.device_present is True
    assert [r for r in caplog.records if r.levelno > logging.DEBUG] == []


async def test_an_unplug_during_bulk_out_marks_the_device_gone_and_raises(monkeypatch):
    d = _driver(monkeypatch, _failing_send(usb.core.USBError("no device", errno=errno.ENODEV)))
    with pytest.raises(usb.core.USBError):
        await d._inject_frame(bytes(24))
    assert d.transport.device_present is False


async def test_once_the_device_is_gone_inject_raises_without_sending(monkeypatch):
    sent = []
    d = _driver(monkeypatch, lambda *a, **kw: sent.append(a) or 0)
    d.transport.device_present = False
    with pytest.raises(usb.core.USBError) as excinfo:
        await d._inject_frame(bytes(24))
    assert is_device_gone(excinfo.value)
    assert sent == []
