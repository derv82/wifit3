"""rtl8192fu USB failures follow rtl8xxxu: register I/O is one try with the result ignored
(core.c:611-745), a failed TX URB is dropped (core.c:5553-5562); ENODEV marks the device removed."""
import errno
from unittest.mock import MagicMock

import pytest
import usb.core

import wifit3.chips.rtl8192fu.driver as drv
from wifit3.chips.rtl8192fu.core import Rtl8xxxuPriv, rtl8xxxu_config
from wifit3.chips.rtl8192fu.rtl8192f import rtl8192fu_fops
from wifit3.chips.rtl8192fu.transport import RTL8192FUTransport
from wifit3.errors import SetChannelError

EIO = usb.core.USBError("Input/Output Error", errno=errno.EIO)
ENODEV = usb.core.USBError("No such device", errno=errno.ENODEV)


def _transport(ctrl_transfer) -> RTL8192FUTransport:
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = ctrl_transfer
    return RTL8192FUTransport(dev)


def test_failed_read_returns_what_is_left_in_usb_buf():
    t = _transport([4, EIO])
    t.write32(0x100, 0x11223344)
    assert t.read32(0x100) == 0x11223344


def test_short_read_keeps_the_rest_of_usb_buf():
    t = _transport([4, bytes([0x01, 0x02])])
    t.write32(0x100, 0xAABBCCDD)
    assert t.read32(0x100) == 0xAABB0201


def test_failed_write_carries_on():
    t = _transport([EIO, 1])
    t.write8(0x100, 0x01)
    t.write8(0x101, 0x02)
    assert t.dev.ctrl_transfer.call_count == 2


def test_enodev_marks_the_device_removed_and_later_ops_raise_without_usb():
    t = _transport([ENODEV])
    with pytest.raises(usb.core.USBError):
        t.read8(0x100)
    with pytest.raises(usb.core.USBError) as e:
        t.write8(0x100, 0x01)
    assert e.value.errno == errno.ENODEV
    assert t.dev.ctrl_transfer.call_count == 1


@pytest.mark.parametrize("result", [EIO, 100])
def test_failed_or_short_firmware_block_aborts(result):
    t = _transport([result])
    with pytest.raises(IOError):
        t.write_block(0x1000, bytes(254))


def test_tune_finishes_when_every_transfer_fails():
    t = _transport(EIO)
    rtl8xxxu_config(Rtl8xxxuPriv(t, rtl8192fu_fops), 6)
    assert t.dev.ctrl_transfer.call_count > 0


def test_failed_tx_urb_is_dropped():
    t = _transport([])
    t.dev.write.side_effect = EIO
    t.bulk_write(0x05, bytes(64))
    assert not t.removed


def test_tx_enodev_raises_and_sends_nothing_after():
    t = _transport([])
    t.dev.write.side_effect = ENODEV
    with pytest.raises(usb.core.USBError):
        t.bulk_write(0x05, bytes(64))
    t.dev.write.reset_mock()
    with pytest.raises(usb.core.USBError) as e:
        t.bulk_write(0x05, bytes(64))
    assert e.value.errno == errno.ENODEV
    t.dev.write.assert_not_called()


async def test_set_channel_rejects_an_unsupported_channel():
    driver = drv.RTL8192FUDriver(MagicMock(), MagicMock())
    with pytest.raises(SetChannelError):
        await driver.set_channel(15)


async def test_oversized_tx_frame_is_dropped(caplog):
    """core.c:5439-5443: dev_warn "%s: Trying to send over-sized skb (%i)" with __func__ rtl8xxxu_tx."""
    driver = drv.RTL8192FUDriver(MagicMock(), MagicMock())
    driver._started = True
    await driver._inject_frame(bytes(65535))
    driver.dev.write.assert_not_called()
    assert "rtl8xxxu_tx: Trying to send over-sized skb (65535)" in caplog.text
