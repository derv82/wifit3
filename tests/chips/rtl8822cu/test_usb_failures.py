"""rtl8822cu USB failures follow the vendor usbctrl_vendorreq (usb_ops_linux.c:26-205): retry below
0x1000, one try from 0x1000 up, log and read zeros, 5 failures in a row or ENODEV mark the device
removed; a failed TX URB is dropped (usb_ops_linux.c:673-685)."""
import errno
import logging
from unittest.mock import MagicMock

import pytest
import usb.core

import wifit3.chips.rtl8822cu.driver as drv
from wifit3.chips.rtl8822cu.driver import RTL8822CUDriver
from wifit3.chips.rtl8822cu.transport import RTL8822CUTransport

EIO = usb.core.USBError("Input/Output Error", errno=errno.EIO)
ENODEV = usb.core.USBError("No such device", errno=errno.ENODEV)
DEAUTH = (bytes.fromhex("c0000000") + bytes.fromhex("aabbccddeeff") + bytes.fromhex("020000000001")
          + bytes.fromhex("aabbccddeeff") + bytes(2))


def _transport(ctrl_transfer) -> RTL8822CUTransport:
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = ctrl_transfer
    return RTL8822CUTransport(dev)


def test_a_failed_mac_register_read_is_retried():
    t = _transport([EIO, bytes([0x44, 0x33, 0x22, 0x11])])
    assert t.read32(0x0200) == 0x11223344
    assert t.dev.ctrl_transfer.call_count == 2


def test_five_failures_in_a_row_mark_the_device_removed():
    t = _transport(EIO)
    with pytest.raises(usb.core.USBError) as e:
        t.read8(0x0200)
    assert e.value.errno == errno.ENODEV
    assert t.dev.ctrl_transfer.call_count == 5
    with pytest.raises(usb.core.USBError):
        t.write8(0x0200, 0x01)
    assert t.dev.ctrl_transfer.call_count == 5


def test_the_fifth_failure_in_a_row_logs_continual_io_error(caplog):
    t = _transport(EIO)
    with caplog.at_level(logging.INFO), pytest.raises(usb.core.USBError):
        t.read8(0x0200)
    assert caplog.messages[-1] == "[ERROR] continual_io_error:5 > 4"


def test_a_failed_read_logs_the_status_and_the_data_read_so_far(caplog):
    t = _transport([bytes([0x01, 0x02]), EIO, bytes([0x03, 0x04, 0x05, 0x06])])
    with caplog.at_level(logging.INFO):
        assert t.read32(0x0200) == 0x06050403
    assert caplog.messages == [
        "reg 0x200, usb read 4 fail, status:2 value=0x0, vendorreq_times:1",
        f"reg 0x200, usb read 4 fail, status:{-errno.EIO} value=0x201, vendorreq_times:2"]


def test_a_failed_bb_register_read_is_one_try_and_reads_zero():
    t = _transport([EIO])
    assert t.read32(0x1C90) == 0
    assert t.dev.ctrl_transfer.call_count == 1
    assert not t.surprise_removed


def test_a_short_read_keeps_the_partial_data():
    t = _transport([bytes([0x01, 0x02])])
    assert t.read32(0x1C90) == 0x0201


def test_a_failed_write_carries_on():
    t = _transport([EIO, 4])
    t.write32(0x1C90, 0x1)
    t.write32(0x1C94, 0x2)
    assert t.dev.ctrl_transfer.call_count == 2


def test_enodev_marks_the_device_removed():
    t = _transport([ENODEV])
    with pytest.raises(usb.core.USBError):
        t.read32(0x1C90)
    with pytest.raises(usb.core.USBError) as e:
        t.read32(0x1C90)
    assert e.value.errno == errno.ENODEV
    assert t.dev.ctrl_transfer.call_count == 1


def test_the_0x4e0_echo_follows_a_failed_on_section_read_with_zero():
    t = _transport([EIO, 1])
    assert t.read8(0x1004) == 0
    echo = t.dev.ctrl_transfer.call_args_list[1].args
    assert echo[2] == 0x4E0 and echo[4] == bytes([0])


def _driver() -> RTL8822CUDriver:
    d = RTL8822CUDriver(MagicMock())
    d._bulk_out_eps = [0x05]
    return d


async def test_a_failed_tx_urb_is_dropped(monkeypatch):
    monkeypatch.setattr(drv, "write_bulk", MagicMock(side_effect=EIO))
    d = _driver()
    await d.inject_frame(DEAUTH)
    assert not d.transport.surprise_removed


async def test_a_tx_enodev_logs_usb_write_port_status_before_raising(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(drv, "write_bulk", MagicMock(side_effect=ENODEV))
    d = _driver()
    with pytest.raises(usb.core.USBError):
        await d.inject_frame(DEAUTH)
    assert "usb_write_port, status=-19" in caplog.text


async def test_tx_enodev_raises_and_sends_nothing_after(monkeypatch):
    write_bulk = MagicMock(side_effect=ENODEV)
    monkeypatch.setattr(drv, "write_bulk", write_bulk)
    d = _driver()
    with pytest.raises(usb.core.USBError):
        await d.inject_frame(DEAUTH)
    write_bulk.reset_mock()
    with pytest.raises(usb.core.USBError) as e:
        await d.inject_frame(DEAUTH)
    assert e.value.errno == errno.ENODEV
    write_bulk.assert_not_called()
