import errno
from array import array

import pytest
import usb.core

from wifit3.chips.rtl8821au_dkms.transport import RTL8821AUDkmsTransport
from wifit3.errors import is_device_gone


def test_usbctrl_vendorreq_retries_a_failed_write(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = [usb.core.USBError("pipe"), 1]
    RTL8821AUDkmsTransport(dev).write8(0x522, 0xFF)
    assert dev.ctrl_transfer.call_count == 2


def test_usbctrl_vendorreq_raises_after_five_failures_in_a_row(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    with pytest.raises(usb.core.USBError) as excinfo:
        RTL8821AUDkmsTransport(dev).write8(0x522, 0xFF)
    assert dev.ctrl_transfer.call_count == 5
    assert is_device_gone(excinfo.value)


def test_usbctrl_vendorreq_retries_a_short_write(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = [1, 2]
    RTL8821AUDkmsTransport(dev).write16(0x522, 0xFFFF)
    assert dev.ctrl_transfer.call_count == 2


def test_usbctrl_vendorreq_marks_the_device_removed_after_five_short_reads(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.return_value = array("B", [0x12])
    with pytest.raises(usb.core.USBError) as excinfo:
        RTL8821AUDkmsTransport(dev).read16(0x522)
    assert dev.ctrl_transfer.call_count == 5
    assert is_device_gone(excinfo.value)


def test_usbctrl_vendorreq_tries_a_firmware_page_write_once(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    assert RTL8821AUDkmsTransport(dev).writeN(0x1000, bytes(4)) is False
    assert dev.ctrl_transfer.call_count == 1


def test_usbctrl_vendorreq_reads_zero_after_a_failed_read_from_0x1000_up(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    assert RTL8821AUDkmsTransport(dev).read8(0xFE17) == 0
    assert dev.ctrl_transfer.call_count == 1


def test_usbctrl_vendorreq_logs_each_failed_try(mocker, caplog):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = [usb.core.USBError("pipe", errno=errno.EPIPE), 1]
    with caplog.at_level("INFO"):
        assert RTL8821AUDkmsTransport(dev).write8(0x522, 0xFF) is True
    assert caplog.messages == [
        f"reg 0x522, usb write 1 fail, status:{-errno.EPIPE} value=0xff, vendorreq_times:1"]


def test_usbctrl_vendorreq_logs_the_fifth_failure_in_a_row(mocker, caplog):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    with caplog.at_level("INFO"), pytest.raises(usb.core.USBError):
        RTL8821AUDkmsTransport(dev).write8(0x522, 0xFF)
    assert caplog.messages[-1] == "[ERROR] continual_io_error:5 > 4"


def test_usbctrl_vendorreq_skips_usb_after_enodev(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = [usb.core.USBError("no device", errno=errno.ENODEV), 1]
    transport = RTL8821AUDkmsTransport(dev)
    transport.write8(0x522, 0xFF)
    dev.ctrl_transfer.reset_mock()
    with pytest.raises(usb.core.USBError) as excinfo:
        transport.read8(0x522)
    assert is_device_gone(excinfo.value)
    dev.ctrl_transfer.assert_not_called()


def test_usbctrl_vendorreq_skips_usb_after_five_failures_in_a_row(mocker):
    dev = mocker.Mock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe")
    transport = RTL8821AUDkmsTransport(dev)
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
    transport = RTL8821AUDkmsTransport(dev)
    transport.bulk_out(b"\x00" * 24)
    assert transport.surprise_removed is False


def test_bulk_out_raises_and_marks_the_device_removed_on_enodev(mocker):
    dev = mocker.Mock()
    dev.write.side_effect = usb.core.USBError("no device", errno=errno.ENODEV)
    transport = RTL8821AUDkmsTransport(dev)
    with pytest.raises(usb.core.USBError):
        transport.bulk_out(b"\x00" * 24)
    assert transport.surprise_removed is True


def test_bulk_out_skips_usb_once_the_device_is_marked_removed(mocker):
    dev = mocker.Mock()
    transport = RTL8821AUDkmsTransport(dev)
    transport.surprise_removed = True
    with pytest.raises(usb.core.USBError) as excinfo:
        transport.bulk_out(b"\x00" * 24)
    assert is_device_gone(excinfo.value)
    dev.write.assert_not_called()


def test_bulk_in_raises_enodev_without_reading_once_the_device_is_removed(mocker):
    dev = mocker.Mock()
    t = RTL8821AUDkmsTransport(dev)
    t.surprise_removed = True
    with pytest.raises(usb.core.USBError) as excinfo:
        t.bulk_in()
    assert is_device_gone(excinfo.value)
    dev.read.assert_not_called()


def test_a_good_rx_transfer_resets_the_continual_io_error_count(mocker):
    dev = mocker.Mock()
    dev.read.return_value = array("B", bytes(24))
    t = RTL8821AUDkmsTransport(dev)
    t._in_ep = 0x81
    t.continual_io_error = 4
    t.bulk_in()
    assert t.continual_io_error == 0


def test_a_tx_enodev_logs_usb_write_port_status_before_raising(mocker, caplog):
    dev = mocker.Mock()
    dev.write.side_effect = usb.core.USBError("No such device", errno=errno.ENODEV)
    t = RTL8821AUDkmsTransport(dev)
    with caplog.at_level("INFO"):
        with pytest.raises(usb.core.USBError):
            t.bulk_out(bytes(64))
    assert "usb_write_port, status=-19" in caplog.text


class FakeEndpoint:
    def __init__(self, address: int, attributes: int):
        self.bEndpointAddress = address
        self.bmAttributes = attributes


class FakeInterface:
    def __init__(self, number: int, interface_class: int, endpoints: list[FakeEndpoint]):
        self.bInterfaceNumber = number
        self.bInterfaceClass = interface_class
        self.bInterfaceSubClass = interface_class
        self.bInterfaceProtocol = interface_class
        self.endpoints = endpoints

    def __iter__(self):
        return iter(self.endpoints)


class FakeConfiguration:
    def __init__(self, interfaces: list[FakeInterface]):
        self.interfaces = interfaces

    def __iter__(self):
        return iter(self.interfaces)

    def __getitem__(self, index: tuple[int, int]) -> FakeInterface:
        return self.interfaces[index[0]]


BULK, INTERRUPT, ISOCHRONOUS = 0x02, 0x03, 0x01


def test_bulk_in_ep_is_on_the_vendor_interface_of_a_multi_function_device(mocker):
    dev = mocker.Mock()
    dev.get_active_configuration.return_value = FakeConfiguration([
        FakeInterface(0, 0xE0, [FakeEndpoint(0x81, INTERRUPT), FakeEndpoint(0x02, BULK), FakeEndpoint(0x82, BULK)]),
        FakeInterface(1, 0xE0, [FakeEndpoint(0x03, ISOCHRONOUS), FakeEndpoint(0x83, ISOCHRONOUS)]),
        FakeInterface(2, 0xFF, [FakeEndpoint(0x84, BULK), FakeEndpoint(0x05, BULK), FakeEndpoint(0x06, BULK),
                                FakeEndpoint(0x87, INTERRUPT), FakeEndpoint(0x08, BULK), FakeEndpoint(0x09, BULK)]),
    ])
    transport = RTL8821AUDkmsTransport(dev)
    assert transport.wlan_interface().bInterfaceNumber == 2
    assert transport._bulk_in_ep() == 0x84


def test_bulk_in_ep_is_on_interface_0_of_a_single_interface_device(mocker):
    dev = mocker.Mock()
    dev.get_active_configuration.return_value = FakeConfiguration([
        FakeInterface(0, 0xFF, [FakeEndpoint(0x84, BULK), FakeEndpoint(0x05, BULK), FakeEndpoint(0x06, BULK),
                                FakeEndpoint(0x87, INTERRUPT), FakeEndpoint(0x08, BULK), FakeEndpoint(0x09, BULK)]),
    ])
    transport = RTL8821AUDkmsTransport(dev)
    assert transport.wlan_interface().bInterfaceNumber == 0
    assert transport._bulk_in_ep() == 0x84


def test_wlan_interface_is_interface_0_without_a_vendor_interface(mocker):
    dev = mocker.Mock()
    dev.get_active_configuration.return_value = FakeConfiguration([
        FakeInterface(0, 0x00, [FakeEndpoint(0x84, BULK)]),
        FakeInterface(1, 0x00, [FakeEndpoint(0x81, BULK)]),
    ])
    assert RTL8821AUDkmsTransport(dev).wlan_interface().bInterfaceNumber == 0
