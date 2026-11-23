import errno
from unittest.mock import MagicMock

import pytest
import usb.core

from wifit3.chips.mt76x2u.mcu import McuChannel
from wifit3.chips.mt76x2u.transport import MT76x2UTransport
from wifit3.errors import is_device_gone


def test_vendor_request_retries_a_failed_transfer():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = [usb.core.USBError("pipe", errno=errno.EPIPE), bytes([1, 0, 0, 0])]
    assert MT76x2UTransport(dev).read32(0x1000) == 1
    assert dev.ctrl_transfer.call_count == 2


def test_a_failed_read_returns_all_ones_and_carries_on():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("io", errno=errno.EIO)
    transport = MT76x2UTransport(dev)
    assert transport.read32(0x1000) == 0xFFFFFFFF
    assert dev.ctrl_transfer.call_count == 10
    assert transport.removed is False


def test_a_failed_write_carries_on():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe", errno=errno.EPIPE)
    MT76x2UTransport(dev).write32(0x1000, 1)


def test_enodev_marks_the_device_removed_and_later_ops_skip_usb():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("no device", errno=errno.ENODEV)
    transport = MT76x2UTransport(dev)
    with pytest.raises(usb.core.USBError):
        transport.write32(0x1000, 1)
    dev.ctrl_transfer.reset_mock()
    with pytest.raises(usb.core.USBError) as excinfo:
        transport.read32(0x1000)
    assert is_device_gone(excinfo.value)
    dev.ctrl_transfer.assert_not_called()
    with pytest.raises(usb.core.USBError):
        transport.write_bulk(0x04, bytes(8))
    dev.write.assert_not_called()


async def test_an_unplug_during_an_mcu_command_raises_instead_of_failing_the_tune():
    dev = MagicMock()
    dev.write.side_effect = usb.core.USBError("no device", errno=errno.ENODEV)
    with pytest.raises(usb.core.USBError) as excinfo:
        await McuChannel(MT76x2UTransport(dev)).send(1, bytes(4))
    assert is_device_gone(excinfo.value)
