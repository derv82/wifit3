import errno
from array import array

import pytest
import usb.core

from wifit3.chips.rtl88xxau_base.transport import Rtl88xxauTransport
from wifit3.errors import is_device_gone


def test_usbctrl_vendorreq_retries_a_failed_write(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = [usb.core.USBError("pipe"), 1]
    Rtl88xxauTransport(dev).write8(0x522, 0xFF)
    assert dev.ctrl_transfer.call_count == 2


def test_usbctrl_vendorreq_raises_after_five_failures_in_a_row(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    with pytest.raises(usb.core.USBError) as excinfo:
        Rtl88xxauTransport(dev).write8(0x522, 0xFF)
    assert dev.ctrl_transfer.call_count == 5
    assert is_device_gone(excinfo.value)


def test_usbctrl_vendorreq_retries_a_short_write(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = [1, 2]
    Rtl88xxauTransport(dev).write16(0x522, 0xFFFF)
    assert dev.ctrl_transfer.call_count == 2


def test_usbctrl_vendorreq_marks_the_device_removed_after_five_short_reads(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.return_value = array("B", [0x12])
    with pytest.raises(usb.core.USBError) as excinfo:
        Rtl88xxauTransport(dev).read16(0x522)
    assert dev.ctrl_transfer.call_count == 5
    assert is_device_gone(excinfo.value)


def test_usbctrl_vendorreq_tries_a_firmware_page_write_once(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    assert Rtl88xxauTransport(dev).writeN(0x1000, bytes(4)) is False
    assert dev.ctrl_transfer.call_count == 1


def test_usbctrl_vendorreq_reads_zero_after_a_failed_read_from_0x1000_up(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    assert Rtl88xxauTransport(dev).read8(0xFE17) == 0
    assert dev.ctrl_transfer.call_count == 1


def test_usbctrl_vendorreq_logs_each_failed_try(mocker, caplog):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = [usb.core.USBError("pipe", errno=errno.EPIPE), 1]
    with caplog.at_level("INFO"):
        assert Rtl88xxauTransport(dev).write8(0x522, 0xFF) is True
    assert caplog.messages == [
        f"reg 0x522, usb write 1 fail, status:{-errno.EPIPE} value=0xff, vendorreq_times:1"]


def test_usbctrl_vendorreq_logs_the_fifth_failure_in_a_row(mocker, caplog):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    with caplog.at_level("INFO"), pytest.raises(usb.core.USBError):
        Rtl88xxauTransport(dev).write8(0x522, 0xFF)
    assert caplog.messages[-1] == "[ERROR] continual_io_error:5 > 4"


def test_usbctrl_vendorreq_skips_usb_after_enodev(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = [usb.core.USBError("no device", errno=errno.ENODEV), 1]
    transport = Rtl88xxauTransport(dev)
    transport.write8(0x522, 0xFF)
    dev.ctrl_transfer.reset_mock()
    with pytest.raises(usb.core.USBError) as excinfo:
        transport.read8(0x522)
    assert is_device_gone(excinfo.value)
    dev.ctrl_transfer.assert_not_called()


def test_usbctrl_vendorreq_skips_usb_after_five_failures_in_a_row(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    transport = Rtl88xxauTransport(dev)
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
    transport = Rtl88xxauTransport(dev)
    transport.bulk_out(b"\x00" * 24)
    assert transport.surprise_removed is False


def test_bulk_out_raises_and_marks_the_device_removed_on_enodev(mocker):
    dev = mocker.Mock()
    dev.write.side_effect = usb.core.USBError("no device", errno=errno.ENODEV)
    transport = Rtl88xxauTransport(dev)
    with pytest.raises(usb.core.USBError):
        transport.bulk_out(b"\x00" * 24)
    assert transport.surprise_removed is True


def test_bulk_out_skips_usb_once_the_device_is_marked_removed(mocker):
    dev = mocker.Mock()
    transport = Rtl88xxauTransport(dev)
    transport.surprise_removed = True
    with pytest.raises(usb.core.USBError) as excinfo:
        transport.bulk_out(b"\x00" * 24)
    assert is_device_gone(excinfo.value)
    dev.write.assert_not_called()


def test_bulk_in_raises_enodev_without_reading_once_the_device_is_removed(mocker):
    dev = mocker.Mock()
    t = Rtl88xxauTransport(dev)
    t.surprise_removed = True
    with pytest.raises(usb.core.USBError) as excinfo:
        t.bulk_in()
    assert is_device_gone(excinfo.value)
    dev.read.assert_not_called()


def test_a_good_rx_transfer_resets_the_continual_io_error_count(mocker):
    dev = mocker.Mock()
    dev.read.return_value = array("B", bytes(24))
    t = Rtl88xxauTransport(dev)
    t._in_ep = 0x81
    t.continual_io_error = 4
    t.bulk_in()
    assert t.continual_io_error == 0


def test_a_tx_enodev_logs_usb_write_port_status_before_raising(mocker, caplog):
    dev = mocker.Mock()
    dev.write.side_effect = usb.core.USBError("No such device", errno=errno.ENODEV)
    t = Rtl88xxauTransport(dev)
    with caplog.at_level("INFO"):
        with pytest.raises(usb.core.USBError):
            t.bulk_out(bytes(64))
    assert "usb_write_port, status=-19" in caplog.text
