"""rtl8922au USB failures follow rtw89 usb.c: up to 10 tries; ENODEV or 5 failures in a
row mark the device unplugged (usb.c:20-73); a failed TX/H2C URB is logged and
dropped (usb.c:344-354)."""
import errno
from unittest.mock import MagicMock

import pytest
import usb.core

from wifit3.chips.rtl8922au.driver import RTL8922AUDriver
from wifit3.chips.rtl8922au.transport import RTL8922AUTransport
from wifit3.errors import SetChannelError

EIO = usb.core.USBError("Input/Output Error", errno=errno.EIO)
ENODEV = usb.core.USBError("No such device", errno=errno.ENODEV)


def _transport(ctrl_transfer) -> RTL8922AUTransport:
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = ctrl_transfer
    t = RTL8922AUTransport(dev)
    t.bulk_out_max_packet_size = {0x05: 512}
    return t


def test_a_failed_read_is_retried():
    t = _transport([EIO, bytes([0x44, 0x33, 0x22, 0x11])])
    assert t.read32(0x0200) == 0x11223344
    assert t.dev.ctrl_transfer.call_count == 2


def test_a_failed_write_warns_with_the_errno_and_the_written_value(caplog):
    t = _transport([EIO, 4])
    t.write32(0x0200, 0x11223344)
    assert "usb write32 0x200 fail ret=-5 value=0x11223344 attempt=0" in caplog.text


def test_five_failures_in_a_row_mark_the_device_unplugged():
    t = _transport(EIO)
    with pytest.raises(usb.core.USBError) as e:
        t.read32(0x0200)
    assert e.value.errno == errno.ENODEV
    assert t.dev.ctrl_transfer.call_count == 5
    with pytest.raises(usb.core.USBError):
        t.write32(0x0200, 1)
    assert t.dev.ctrl_transfer.call_count == 5


def test_a_short_read_keeps_the_partial_data_and_then_succeeds():
    t = _transport([bytes([0x01, 0x02]), bytes([0x44, 0x33, 0x22, 0x11])])
    assert t.read32(0x0200) == 0x11223344


def test_enodev_marks_the_device_unplugged_on_the_first_try():
    t = _transport([ENODEV])
    with pytest.raises(usb.core.USBError):
        t.write32(0x0200, 1)
    assert t.dev.ctrl_transfer.call_count == 1
    assert t.unplugged


def test_a_failed_h2c_or_tx_urb_is_dropped():
    t = _transport([])
    t.dev.write.side_effect = EIO
    t.bulk_out(0x05, bytes(64))
    assert not t.unplugged


def test_tx_enodev_raises_and_sends_nothing_after():
    t = _transport([])
    t.dev.write.side_effect = ENODEV
    with pytest.raises(usb.core.USBError):
        t.bulk_out(0x05, bytes(64))
    t.dev.write.reset_mock()
    with pytest.raises(usb.core.USBError) as e:
        t.bulk_out(0x05, bytes(64))
    assert e.value.errno == errno.ENODEV
    t.dev.write.assert_not_called()


async def test_set_channel_rejects_an_unsupported_channel():
    d = RTL8922AUDriver()
    d.transport = _transport([])
    with pytest.raises(SetChannelError):
        await d.set_channel(15)
    d.transport.dev.ctrl_transfer.assert_not_called()


async def test_inject_frame_raises_on_enodev():
    d = RTL8922AUDriver()
    d.transport = _transport([])
    d.transport.dev.write.side_effect = ENODEV
    d._mgmt_ep = 0x05
    frame = (bytes.fromhex("c0000000") + bytes.fromhex("aabbccddeeff") * 3 + bytes(2))
    with pytest.raises(usb.core.USBError):
        await d.inject_frame(frame)


def test_a_bulk_out_ending_on_a_packet_boundary_is_followed_by_a_zero_length_packet():
    t = _transport([])
    t.bulk_out(0x05, bytes(512))
    assert [c.args[:2] for c in t.dev.write.call_args_list] == [(0x05, bytes(512)), (0x05, b"")]


def test_a_bulk_out_not_ending_on_a_packet_boundary_sends_no_zero_length_packet():
    t = _transport([])
    t.bulk_out(0x05, bytes(513))
    assert t.dev.write.call_count == 1
