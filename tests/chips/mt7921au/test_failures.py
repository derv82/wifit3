import errno
from unittest.mock import AsyncMock, MagicMock

import pytest
import usb.core

from wifit3.chips.mt7921au.driver import MT7921AUDriver
from wifit3.chips.mt7921au.transport import MT7921AUTransport
from wifit3.errors import BringUpError, SetChannelError, is_device_gone


def _driver(reply) -> MT7921AUDriver:
    drv = object.__new__(MT7921AUDriver)
    drv.transport = MagicMock()
    drv.transport.send_mcu_command = AsyncMock(return_value=reply)
    drv._channel = 1
    return drv


async def test_set_channel_rejects_an_unsupported_channel_before_any_write():
    drv = _driver(b"ok")
    with pytest.raises(SetChannelError):
        await drv.set_channel(200)
    drv.transport.send_mcu_command.assert_not_called()


async def test_set_channel_raises_when_the_firmware_does_not_reply():
    drv = _driver(None)
    with pytest.raises(SetChannelError):
        await drv.set_channel(6)
    assert drv._channel == 1


async def test_set_channel_records_the_channel_after_the_reply():
    drv = _driver(b"ok")
    await drv.set_channel(6)
    assert drv._channel == 6


async def test_warm_reattach_raises_bringup_error_when_the_tune_times_out():
    drv = _driver(None)
    with pytest.raises(BringUpError):
        await drv._warm_reattach(None)


async def test_a_bulk_write_after_an_unplug_raises_without_touching_usb():
    dev = MagicMock()
    dev.write.side_effect = usb.core.USBError("no device", errno=errno.ENODEV)
    transport = MT7921AUTransport(dev)
    with pytest.raises(usb.core.USBError):
        await transport.send_bulk_checked(bytes(8), 0x09)
    dev.write.reset_mock()
    with pytest.raises(usb.core.USBError) as excinfo:
        await transport.send_bulk_checked(bytes(8), 0x09)
    assert is_device_gone(excinfo.value)
    dev.write.assert_not_called()


async def test_a_failed_bulk_write_is_dropped():
    dev = MagicMock()
    dev.write.side_effect = usb.core.USBError("pipe", errno=errno.EPIPE)
    transport = MT7921AUTransport(dev)
    assert await transport.send_bulk_checked(bytes(8), 0x09) is False
