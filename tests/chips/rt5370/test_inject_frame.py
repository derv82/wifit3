import errno
import logging
from unittest.mock import MagicMock

import pytest
import usb.core

import wifit3.chips.rt5370.driver as drv
from wifit3.errors import is_device_gone


def _driver_with_failing_send_frame(monkeypatch, error: usb.core.USBError) -> drv.RT5370Driver:
    def failing_send_frame(*_a, **_kw):
        raise error

    monkeypatch.setattr(drv.tx, "send_frame", failing_send_frame)
    return drv.RT5370Driver(MagicMock())


def test_a_failed_bulk_out_drops_the_frame(monkeypatch, caplog):
    """rt2x00usb_kick_tx_entry marks a failed URB ENTRY_DATA_IO_FAILED and carries on (rt2x00usb.c:330-336)."""
    d = _driver_with_failing_send_frame(monkeypatch, usb.core.USBError("timeout", errno=errno.ETIMEDOUT))
    d._do_inject(bytes(24))
    assert [r for r in caplog.records if r.levelno > logging.DEBUG] == []


def test_an_unplug_during_bulk_out_propagates(monkeypatch):
    d = _driver_with_failing_send_frame(monkeypatch, usb.core.USBError("no device", errno=errno.ENODEV))
    with pytest.raises(usb.core.USBError):
        d._do_inject(bytes(24))
    assert d.transport.device_present is False


def test_once_the_device_is_gone_inject_raises_without_sending(monkeypatch):
    sent = []
    monkeypatch.setattr(drv.tx, "send_frame", lambda *a, **kw: sent.append(a))
    d = drv.RT5370Driver(MagicMock())
    d.transport.device_present = False
    with pytest.raises(usb.core.USBError) as excinfo:
        d._do_inject(bytes(24))
    assert is_device_gone(excinfo.value)
    assert sent == []
