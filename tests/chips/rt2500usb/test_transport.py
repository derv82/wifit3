import errno
import logging
from unittest.mock import MagicMock

import pytest
import usb.core

import wifit3.chips.rt2500usb.transport as transport_module
from wifit3.chips.rt2500usb import bbp, chan
from wifit3.chips.rt2500usb import constants as C
from wifit3.chips.rt2500usb.transport import RT2500USBTransport
from wifit3.errors import is_device_gone


def test_vendor_request_retries_a_failed_transfer():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = [usb.core.USBError("pipe", errno=errno.EPIPE), bytes([1, 0, 0, 0])]
    assert RT2500USBTransport(dev).read16(0x1000) == 1
    assert dev.ctrl_transfer.call_count == 2


def test_a_failed_register_read_reads_0_and_carries_on():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe", errno=errno.EPIPE)
    transport = RT2500USBTransport(dev)
    assert transport.read16(0x1000) == 0
    assert transport.device_present is True


def test_a_failed_register_write_carries_on():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe", errno=errno.EPIPE)
    RT2500USBTransport(dev).write16(0x1000, 1)


def test_enodev_marks_the_device_gone_and_later_ops_skip_usb():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("no device", errno=errno.ENODEV)
    transport = RT2500USBTransport(dev)
    with pytest.raises(usb.core.USBError):
        transport.write16(0x1000, 1)
    dev.ctrl_transfer.reset_mock()
    with pytest.raises(usb.core.USBError) as excinfo:
        transport.read16(0x1000)
    assert is_device_gone(excinfo.value)
    dev.ctrl_transfer.assert_not_called()


def test_four_eio_in_a_row_after_bring_up_mark_the_device_gone():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("io", errno=errno.EIO)
    transport = RT2500USBTransport(dev)
    transport.device_started = True
    with pytest.raises(usb.core.USBError) as excinfo:
        transport.write16(0x1000, 1)
    assert is_device_gone(excinfo.value)
    assert dev.ctrl_transfer.call_count == 4


def test_eio_during_bring_up_never_marks_the_device_gone():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("io", errno=errno.EIO)
    transport = RT2500USBTransport(dev)
    transport.write16(0x1000, 1)
    assert transport.device_present is True


@pytest.mark.parametrize("write", [
    lambda t: bbp.bbp_write(t, 2, 0x10),        # rt2500usb.c:122-143
    lambda t: chan.rf_write(t, 1, 0x1234),      # rt2500usb.c:179-206
])
def test_a_busy_timeout_logs_once_and_skips_the_write(monkeypatch, caplog, write):
    """The only log is rt2500usb_regbusy_read's rt2x00_err (rt2500usb.c:110-111)."""
    monkeypatch.setattr(transport_module.time, "sleep", lambda _s: None)
    dev = MagicMock()
    dev.ctrl_transfer.return_value = (0xFFFF).to_bytes(2, "little")
    write(RT2500USBTransport(dev))
    assert dev.ctrl_transfer.call_count == C.REGISTER_USB_BUSY_COUNT
    assert [r.levelno for r in caplog.records] == [logging.ERROR]


def test_a_failed_vendor_request_logs_the_negative_errno(caplog):
    """rt2x00usb_vendor_request logs the status with %d (rt2x00usb.c:75-77)."""
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe", errno=errno.EPIPE)
    RT2500USBTransport(dev).write16(0x1000, 1)
    assert [r.getMessage() for r in caplog.records] == [
        "Vendor Request 0x06 failed for offset 0x1000 with error -32"]
