import errno
from array import array

import pytest
import usb.core

from wifit3.chips.rtl8822bu_dkms.transport import Rtl8822buTransport
from wifit3.errors import is_device_gone


def test_usbctrl_vendorreq_retries_a_failed_write(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = [usb.core.USBError("pipe"), 1]
    Rtl8822buTransport(dev).write8(0x522, 0xFF)
    assert dev.ctrl_transfer.call_count == 2


def test_usbctrl_vendorreq_raises_after_five_failures_in_a_row(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    with pytest.raises(usb.core.USBError) as excinfo:
        Rtl8822buTransport(dev).write8(0x522, 0xFF)
    assert dev.ctrl_transfer.call_count == 5
    assert is_device_gone(excinfo.value)


def test_usbctrl_vendorreq_retries_a_short_write(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = [1, 2]
    Rtl8822buTransport(dev).write16(0x522, 0xFFFF)
    assert dev.ctrl_transfer.call_count == 2


def test_usbctrl_vendorreq_marks_the_device_removed_after_five_short_reads(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.return_value = array("B", [0x12])
    with pytest.raises(usb.core.USBError) as excinfo:
        Rtl8822buTransport(dev).read16(0x522)
    assert dev.ctrl_transfer.call_count == 5
    assert is_device_gone(excinfo.value)


def test_usbctrl_vendorreq_tries_a_firmware_page_write_once(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    Rtl8822buTransport(dev).writeN(0x1100, bytes(4))
    assert dev.ctrl_transfer.call_count == 1


def test_usbctrl_vendorreq_reads_zero_after_a_failed_read_from_0x1000_up(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    assert Rtl8822buTransport(dev).read32(0x1100) == 0
    assert dev.ctrl_transfer.call_count == 1


def test_usbctrl_vendorreq_logs_each_failed_try(mocker, caplog):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = [usb.core.USBError("pipe", errno=errno.EPIPE), 1]
    with caplog.at_level("INFO"):
        Rtl8822buTransport(dev).write8(0x522, 0xFF)
    assert caplog.messages == [
        f"reg 0x522, usb write 1 fail, status:{-errno.EPIPE} value=0xff, vendorreq_times:1"]


def test_usbctrl_vendorreq_logs_the_fifth_failure_in_a_row(mocker, caplog):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    with caplog.at_level("INFO"), pytest.raises(usb.core.USBError):
        Rtl8822buTransport(dev).write8(0x522, 0xFF)
    assert caplog.messages[-1] == "[ERROR] continual_io_error:5 > 4"


def test_usbctrl_vendorreq_skips_usb_after_enodev(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = [usb.core.USBError("no device", errno=errno.ENODEV), 1]
    transport = Rtl8822buTransport(dev)
    transport.write8(0x522, 0xFF)
    dev.ctrl_transfer.reset_mock()
    with pytest.raises(usb.core.USBError) as excinfo:
        transport.read8(0x522)
    assert is_device_gone(excinfo.value)
    dev.ctrl_transfer.assert_not_called()


def test_usbctrl_vendorreq_skips_usb_after_five_failures_in_a_row(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    transport = Rtl8822buTransport(dev)
    with pytest.raises(usb.core.USBError):
        transport.write8(0x522, 0xFF)
    dev.ctrl_transfer.reset_mock()
    with pytest.raises(usb.core.USBError) as excinfo:
        transport.read8(0x522)
    assert is_device_gone(excinfo.value)
    dev.ctrl_transfer.assert_not_called()


def test_usb_write_port_drops_a_frame_on_a_pipe_error(mocker):
    dev = mocker.Mock()
    dev.write.side_effect = usb.core.USBError("pipe", errno=errno.EPIPE)
    transport = Rtl8822buTransport(dev)
    transport.usb_write_port(b"\x00" * 24)
    assert transport.surprise_removed is False


def test_usb_write_port_raises_and_marks_the_device_removed_on_enodev(mocker):
    dev = mocker.Mock()
    dev.write.side_effect = usb.core.USBError("no device", errno=errno.ENODEV)
    transport = Rtl8822buTransport(dev)
    with pytest.raises(usb.core.USBError):
        transport.usb_write_port(b"\x00" * 24)
    assert transport.surprise_removed is True


def test_usb_write_port_skips_usb_once_the_device_is_marked_removed(mocker):
    dev = mocker.Mock()
    transport = Rtl8822buTransport(dev)
    transport.surprise_removed = True
    with pytest.raises(usb.core.USBError) as excinfo:
        transport.usb_write_port(b"\x00" * 24)
    assert is_device_gone(excinfo.value)
    dev.write.assert_not_called()


def test_bulk_out_skips_usb_once_the_device_is_marked_removed(mocker):
    dev = mocker.Mock()
    transport = Rtl8822buTransport(dev)
    transport.surprise_removed = True
    with pytest.raises(usb.core.USBError) as excinfo:
        transport.bulk_out(b"\x00" * 24)
    assert is_device_gone(excinfo.value)
    dev.write.assert_not_called()


def test_usbctrl_vendorreq_writes_0x4e0_after_a_failed_on_section_write(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = [usb.core.USBError("pipe")] * 5 + [1]
    with pytest.raises(usb.core.USBError) as excinfo:
        Rtl8822buTransport(dev).write8(0x10, 0xAB)
    assert is_device_gone(excinfo.value)
    assert dev.ctrl_transfer.call_args.args[2] == 0x4E0
    assert dev.ctrl_transfer.call_args.args[4] == bytes([0xAB])


def test_usbctrl_vendorreq_logs_a_failed_0x4e0_write(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = [1, usb.core.USBError("pipe")]
    Rtl8822buTransport(dev).write8(0x10, 0xAB)
    assert dev.ctrl_transfer.call_count == 2


def test_usbctrl_vendorreq_0x4e0_write_resets_the_failure_count(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = [usb.core.USBError("pipe"), 1]
    transport = Rtl8822buTransport(dev)
    transport.writeN(0x1000, bytes([0x01]))
    assert transport.continual_io_error == 0


def test_bulk_in_raises_enodev_without_reading_once_the_device_is_removed(mocker):
    dev = mocker.Mock()
    t = Rtl8822buTransport(dev)
    t.surprise_removed = True
    with pytest.raises(usb.core.USBError) as excinfo:
        t.bulk_in()
    assert is_device_gone(excinfo.value)
    dev.read.assert_not_called()


def test_a_good_rx_transfer_resets_the_continual_io_error_count(mocker):
    dev = mocker.Mock()
    dev.read.return_value = array("B", bytes(24))
    t = Rtl8822buTransport(dev)
    t._in_ep = 0x81
    t.continual_io_error = 4
    t.bulk_in()
    assert t.continual_io_error == 0


def test_a_tx_enodev_logs_usb_write_port_status_before_raising(mocker, caplog):
    dev = mocker.Mock()
    dev.write.side_effect = usb.core.USBError("No such device", errno=errno.ENODEV)
    t = Rtl8822buTransport(dev)
    with caplog.at_level("INFO"):
        with pytest.raises(usb.core.USBError):
            t.usb_write_port(bytes(64))
    assert "usb_write_port, status=-19" in caplog.text
