"""bulk_in timeout handling — a benign read timeout (no traffic) must return None, not
raise, or the shared RxReaderThread counts it as a fatal error and gives up. Regression
for the 5 GHz scan dying on quiet DFS channels: the Windows/WinUSB backend reports
"[Errno 10060] Operation timed out" (errno 10060, "timed out" — no "timeout" substring),
which the old message-only check missed.
"""
import errno
from array import array

import pytest
import usb.core

from wifit3.chips.rtl8814au_dkms.transport import Rtl8814auTransport
from wifit3.errors import is_device_gone


class _RaisingDev:
    def __init__(self, exc):
        self._exc = exc

    def read(self, ep, size, timeout):
        raise self._exc


def _transport(exc):
    t = Rtl8814auTransport(_RaisingDev(exc))
    t._in_ep = 0x84          # pre-cache so bulk_in skips the endpoint probe
    return t


def test_bulk_in_windows_timeout_returns_none():
    # The case that killed the 5 GHz scan: USBTimeoutError, errno 10060, "Operation timed out".
    t = _transport(usb.core.USBTimeoutError("Operation timed out", errno=10060))
    assert t.bulk_in() is None


def test_bulk_in_libusb_timeout_returns_none():
    t = _transport(usb.core.USBTimeoutError("Operation timed out", errno=110))
    assert t.bulk_in() is None


def test_bulk_in_plain_usberror_timeout_returns_none():
    # A backend raising plain USBError (not the timeout subclass) for a timeout.
    t = _transport(usb.core.USBError("Operation timed out", errno=10060))
    assert t.bulk_in() is None


def test_bulk_in_real_fault_propagates():
    # A real pipe fault must NOT be swallowed (else a wedged pipe looks like a quiet channel).
    t = _transport(usb.core.USBError("pipe error", errno=5))
    with pytest.raises(usb.core.USBError):
        t.bulk_in()


def test_bulk_in_success_returns_bytes():
    class _OkDev:
        def read(self, ep, size, timeout):
            return b"\x01\x02\x03"

    t = Rtl8814auTransport(_OkDev())
    t._in_ep = 0x84
    assert t.bulk_in() == b"\x01\x02\x03"


def test_usbctrl_vendorreq_retries_a_failed_write(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = [usb.core.USBError("pipe"), 1]
    Rtl8814auTransport(dev).write8(0x522, 0xFF)
    assert dev.ctrl_transfer.call_count == 2


def test_usbctrl_vendorreq_raises_after_five_failures_in_a_row(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    with pytest.raises(usb.core.USBError) as excinfo:
        Rtl8814auTransport(dev).write8(0x522, 0xFF)
    assert dev.ctrl_transfer.call_count == 5
    assert is_device_gone(excinfo.value)


def test_usbctrl_vendorreq_retries_a_short_write(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = [1, 2]
    Rtl8814auTransport(dev).write16(0x522, 0xFFFF)
    assert dev.ctrl_transfer.call_count == 2


def test_usbctrl_vendorreq_marks_the_device_removed_after_five_short_reads(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.return_value = array("B", [0x12])
    with pytest.raises(usb.core.USBError) as excinfo:
        Rtl8814auTransport(dev).read16(0x522)
    assert dev.ctrl_transfer.call_count == 5
    assert is_device_gone(excinfo.value)


def test_usbctrl_vendorreq_tries_a_firmware_page_write_once(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    Rtl8814auTransport(dev).write32(0x1000, 0)
    assert dev.ctrl_transfer.call_count == 1


def test_usbctrl_vendorreq_reads_zero_after_a_failed_read_from_0x1000_up(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    assert Rtl8814auTransport(dev).read32(0x1A00) == 0
    assert dev.ctrl_transfer.call_count == 1


def test_usbctrl_vendorreq_logs_each_failed_try(mocker, caplog):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = [usb.core.USBError("pipe", errno=errno.EPIPE), 1]
    with caplog.at_level("INFO"):
        Rtl8814auTransport(dev).write8(0x522, 0xFF)
    assert caplog.messages == [
        f"reg 0x522, usb write 1 fail, status:{-errno.EPIPE} value=0xff, vendorreq_times:1"]


def test_usbctrl_vendorreq_logs_the_fifth_failure_in_a_row(mocker, caplog):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    with caplog.at_level("INFO"), pytest.raises(usb.core.USBError):
        Rtl8814auTransport(dev).write8(0x522, 0xFF)
    assert caplog.messages[-1] == "[ERROR] continual_io_error:5 > 4"


def test_usbctrl_vendorreq_skips_usb_after_enodev(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = [usb.core.USBError("no device", errno=errno.ENODEV), 1]
    transport = Rtl8814auTransport(dev)
    transport.write8(0x522, 0xFF)
    dev.ctrl_transfer.reset_mock()
    with pytest.raises(usb.core.USBError) as excinfo:
        transport.read8(0x522)
    assert is_device_gone(excinfo.value)
    dev.ctrl_transfer.assert_not_called()


def test_usbctrl_vendorreq_skips_usb_after_five_failures_in_a_row(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    transport = Rtl8814auTransport(dev)
    with pytest.raises(usb.core.USBError):
        transport.write8(0x522, 0xFF)
    dev.ctrl_transfer.reset_mock()
    with pytest.raises(usb.core.USBError) as excinfo:
        transport.read8(0x522)
    assert is_device_gone(excinfo.value)
    dev.ctrl_transfer.assert_not_called()


def test_bulk_out_drops_a_frame_on_a_pipe_error(mocker):
    dev = mocker.Mock()
    dev.write.side_effect = usb.core.USBError("pipe", errno=errno.EPIPE)
    transport = Rtl8814auTransport(dev)
    transport.bulk_out(b"\x00" * 24)
    assert transport.surprise_removed is False


def test_bulk_out_raises_and_marks_the_device_removed_on_enodev(mocker):
    dev = mocker.Mock()
    dev.write.side_effect = usb.core.USBError("no device", errno=errno.ENODEV)
    transport = Rtl8814auTransport(dev)
    with pytest.raises(usb.core.USBError):
        transport.bulk_out(b"\x00" * 24)
    assert transport.surprise_removed is True


def test_bulk_out_skips_usb_once_the_device_is_marked_removed(mocker):
    dev = mocker.Mock()
    transport = Rtl8814auTransport(dev)
    transport.surprise_removed = True
    with pytest.raises(usb.core.USBError) as excinfo:
        transport.bulk_out(b"\x00" * 24)
    assert is_device_gone(excinfo.value)
    dev.write.assert_not_called()


def test_bulk_in_raises_enodev_without_reading_once_the_device_is_removed(mocker):
    dev = mocker.Mock()
    t = Rtl8814auTransport(dev)
    t.surprise_removed = True
    with pytest.raises(usb.core.USBError) as excinfo:
        t.bulk_in()
    assert is_device_gone(excinfo.value)
    dev.read.assert_not_called()


def test_a_good_rx_transfer_resets_the_continual_io_error_count(mocker):
    dev = mocker.Mock()
    dev.read.return_value = array("B", bytes(24))
    t = Rtl8814auTransport(dev)
    t._in_ep = 0x81
    t.continual_io_error = 4
    t.bulk_in()
    assert t.continual_io_error == 0


def test_a_tx_enodev_logs_usb_write_port_status_before_raising(mocker, caplog):
    dev = mocker.Mock()
    dev.write.side_effect = usb.core.USBError("No such device", errno=errno.ENODEV)
    t = Rtl8814auTransport(dev)
    with caplog.at_level("INFO"):
        with pytest.raises(usb.core.USBError):
            t.bulk_out(bytes(64))
    assert "usb_write_port, status=-19" in caplog.text
