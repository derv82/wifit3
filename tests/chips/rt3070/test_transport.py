import errno
import logging
from unittest.mock import MagicMock

import pytest
import usb.core

import wifit3.chips.rt3070.transport as transport_module
from wifit3.chips.rt3070 import constants as C
from wifit3.chips.rt3070.transport import RT3070Transport
from wifit3.errors import is_device_gone


def test_vendor_request_retries_a_failed_transfer():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = [usb.core.USBError("pipe", errno=errno.EPIPE), bytes([1, 0, 0, 0])]
    assert RT3070Transport(dev).register_read(0x1000) == 1
    assert dev.ctrl_transfer.call_count == 2


def test_a_failed_register_read_reads_0_and_carries_on():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe", errno=errno.EPIPE)
    transport = RT3070Transport(dev)
    assert transport.register_read(0x1000) == 0
    assert transport.device_present is True


def test_a_failed_register_write_carries_on():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe", errno=errno.EPIPE)
    RT3070Transport(dev).register_write(0x1000, 1)


def test_enodev_marks_the_device_gone_and_later_ops_skip_usb():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("no device", errno=errno.ENODEV)
    transport = RT3070Transport(dev)
    with pytest.raises(usb.core.USBError):
        transport.register_write(0x1000, 1)
    dev.ctrl_transfer.reset_mock()
    with pytest.raises(usb.core.USBError) as excinfo:
        transport.register_read(0x1000)
    assert is_device_gone(excinfo.value)
    dev.ctrl_transfer.assert_not_called()


def test_four_eio_in_a_row_after_bring_up_mark_the_device_gone():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("io", errno=errno.EIO)
    transport = RT3070Transport(dev)
    transport.device_started = True
    with pytest.raises(usb.core.USBError) as excinfo:
        transport.register_write(0x1000, 1)
    assert is_device_gone(excinfo.value)
    assert dev.ctrl_transfer.call_count == 4


def test_eio_during_bring_up_never_marks_the_device_gone():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("io", errno=errno.EIO)
    transport = RT3070Transport(dev)
    transport.register_write(0x1000, 1)
    assert transport.device_present is True


def test_a_register_stuck_busy_is_polled_20_times_then_logged(monkeypatch, caplog):
    """rt2x00usb_regbusy_read: udelay(REGISTER_BUSY_DELAY) per try, then rt2x00_err (rt2x00usb.c:155-163)."""
    sleeps = []
    monkeypatch.setattr(transport_module.time, "sleep", sleeps.append)
    dev = MagicMock()
    dev.ctrl_transfer.return_value = C.BBP_CSR_CFG_BUSY.to_bytes(4, "little")
    transport = RT3070Transport(dev)
    assert transport.regbusy_read(C.BBP_CSR_CFG, C.BBP_CSR_CFG_BUSY) == (False, 0xFFFFFFFF)
    assert dev.ctrl_transfer.call_count == C.REGISTER_USB_BUSY_COUNT
    assert sleeps == [C.REGISTER_BUSY_DELAY / 1_000_000] * C.REGISTER_USB_BUSY_COUNT
    assert [r.levelno for r in caplog.records] == [logging.ERROR]
    assert caplog.records[0].getMessage() == "Indirect register access failed: offset=0x0000101c, value=0x00020000"


def test_a_failed_vendor_request_logs_the_negative_errno(caplog):
    """rt2x00usb_vendor_request logs the status with %d (rt2x00usb.c:75-77)."""
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe", errno=errno.EPIPE)
    RT3070Transport(dev).register_write(0x1000, 1)
    assert [r.getMessage() for r in caplog.records] == [
        "Vendor Request 0x06 failed for offset 0x1000 with error -32"]
