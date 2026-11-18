"""rtl8187 USB failures follow the C: one try, result ignored (rtl8225.c:22-113, dev.c:318-321);
ENODEV marks the device removed."""
import errno
from unittest.mock import MagicMock

import pytest
import usb.core

from wifit3.chips.rtl8187 import chan as chan_mod
from wifit3.chips.rtl8187.constants import REG_BEACON_INTERVAL_TIME
from wifit3.chips.rtl8187.driver import RTL8187Driver
from wifit3.chips.rtl8187.mac import ChipVariant
from wifit3.chips.rtl8187.rtl8225 import RfSetup, RfVariant, TxPower
from wifit3.chips.rtl8187.transport import RTL8187Transport
from wifit3.errors import SetChannelError

EIO = usb.core.USBError("Input/Output Error", errno=errno.EIO)
ENODEV = usb.core.USBError("No such device", errno=errno.ENODEV)
DEAUTH = (bytes.fromhex("c0000000") + bytes.fromhex("aabbccddeeff") + bytes.fromhex("020000000001")
          + bytes.fromhex("aabbccddeeff") + bytes(2))


def _transport(ctrl_transfer) -> RTL8187Transport:
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = ctrl_transfer
    return RTL8187Transport(dev)


def _driver() -> RTL8187Driver:
    d = RTL8187Driver(MagicMock())
    d._bulk_out_max_packet_sizes = {0x02: 512}
    d.chip_variant = ChipVariant(hwver_raw=0, name="RTL8187vB", is_rtl8187b=False, hw_rev=None)
    return d


def test_failed_read_returns_what_is_left_in_io_dmabuf():
    t = _transport([4, EIO])
    t.write32(0x100, 0x11223344)
    assert t.read32(0x100) == 0x11223344


def test_short_read_keeps_the_rest_of_io_dmabuf():
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


def test_tune_finishes_when_every_transfer_fails(monkeypatch):
    monkeypatch.setattr(chan_mod.time, "sleep", lambda *_a: None)
    t = _transport(EIO)
    chan_mod.config_channel(t, RfSetup(asic_rev=1, variant=RfVariant.RTL8225Z2, hw_rev=None), 6,
                            TxPower(hw_value=tuple([0] * 14), base=0))
    assert t.dev.ctrl_transfer.call_args_list[-1].args[2] == REG_BEACON_INTERVAL_TIME


async def test_set_channel_rejects_an_unsupported_channel():
    d = _driver()
    d._rf_setup = RfSetup(asic_rev=1, variant=RfVariant.RTL8225Z2, hw_rev=None)
    d._power = TxPower(hw_value=tuple([0] * 14), base=0)
    with pytest.raises(SetChannelError):
        await d.set_channel(15)


async def test_inject_drops_a_frame_on_a_failed_send():
    d = _driver()
    d.dev.write.side_effect = EIO
    await d.inject_frame(DEAUTH)
    assert not d.transport.removed


async def test_inject_raises_on_enodev_and_sends_nothing_after():
    d = _driver()
    d.dev.write.side_effect = ENODEV
    with pytest.raises(usb.core.USBError):
        await d.inject_frame(DEAUTH)
    d.dev.write.reset_mock()
    with pytest.raises(usb.core.USBError) as e:
        await d.inject_frame(DEAUTH)
    assert e.value.errno == errno.ENODEV
    d.dev.write.assert_not_called()
