"""rtl8192cu USB failures follow rtlwifi: a vendor request is tried up to 10 times, then logged (the first 4)
and dropped (usb.c:28-58); a failed TX URB is dropped (usb.c:800-802); ENODEV marks the device removed."""
import errno
import logging
import time
from unittest.mock import MagicMock

import pytest
import usb.core

from wifit3.chips.rtl8192cu import SUPPORTED_IDS
from wifit3.chips.rtl8192cu import constants as C
from wifit3.chips.rtl8192cu.driver import RTL8192CUDriver
from wifit3.chips.rtl8192cu.transport import RTL8192CUTransport
from wifit3.errors import SetChannelError

from .test_monitor import _started, _tune

EIO = usb.core.USBError("Input/Output Error", errno=errno.EIO)
ENODEV = usb.core.USBError("No such device", errno=errno.ENODEV)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)


def _transport(ctrl_transfer) -> RTL8192CUTransport:
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = ctrl_transfer
    return RTL8192CUTransport(dev)


def test_failed_read_returns_what_is_left_in_its_usb_data_slot():
    t = _transport([4] + [1] * 99 + [EIO] * 10)
    t.write32(0x100, 0x11223344)
    for _ in range(99):
        t.write8(0x200, 0x55)
    assert t.read32(0x100) == 0x11223344


def test_short_read_keeps_the_rest_of_its_usb_data_slot():
    t = _transport([4] + [1] * 99 + [bytes([0x01, 0x02])])
    t.write32(0x100, 0xAABBCCDD)
    for _ in range(99):
        t.write8(0x200, 0x55)
    assert t.read32(0x100) == 0xAABB0201


def test_failed_write_is_tried_10_times_then_carries_on():
    t = _transport([EIO] * 10 + [1])
    t.write8(0x100, 0x01)
    t.write8(0x101, 0x02)
    assert t.dev.ctrl_transfer.call_count == 11


def test_only_the_first_4_failures_are_logged(caplog):
    t = _transport(EIO)
    with caplog.at_level(logging.ERROR, logger="wifit3.chips.rtl8192cu.transport"):
        for _ in range(6):
            t.write8(0x100, 0x01)
    assert len(caplog.records) == 4


def test_failure_log_prints_the_negative_errno_in_hex(caplog):
    """usb.c:55-56: status:0x%x of the negative errno."""
    t = _transport(EIO)
    with caplog.at_level(logging.ERROR, logger="wifit3.chips.rtl8192cu.transport"):
        t.write8(0x100, 0x01)
    assert "reg 0x100, usbctrl_vendorreq TimeOut! status:0xfffffffb value=0x1 reqtype=0x40" in caplog.text


def test_firmware_window_write_is_tried_once():
    t = _transport(EIO)
    t.write_chunk(C.FW_8192C_START_ADDRESS, bytes(64))
    assert t.dev.ctrl_transfer.call_count == 1


def test_enodev_marks_the_device_removed_and_later_ops_raise_without_usb():
    t = _transport([ENODEV])
    with pytest.raises(usb.core.USBError):
        t.read8(0x100)
    with pytest.raises(usb.core.USBError) as e:
        t.write8(0x100, 0x01)
    assert e.value.errno == errno.ENODEV
    assert t.dev.ctrl_transfer.call_count == 1


def test_tune_finishes_when_every_transfer_fails():
    priv, _io = _started()
    t = _transport(EIO)
    priv.io = t
    _tune(priv, 11)
    assert priv.phy.current_channel == 11


def test_enodev_mid_tune_makes_the_next_tune_raise_without_usb():
    def ctrl_transfer(reqtype, _req, value, _index, data_or_length, timeout):
        if value == C.REG_BWOPMODE:
            raise ENODEV
        return bytes(data_or_length) if reqtype == C.REALTEK_USB_VENQT_READ else len(data_or_length)

    priv, _io = _started()
    t = _transport(ctrl_transfer)
    priv.io = t
    with pytest.raises(usb.core.USBError):
        _tune(priv, 6)
    t.dev.ctrl_transfer.reset_mock()
    with pytest.raises(usb.core.USBError) as e:
        _tune(priv, 11)
    assert e.value.errno == errno.ENODEV
    t.dev.ctrl_transfer.assert_not_called()


def test_failed_tx_urb_is_dropped(caplog):
    t = _transport([])
    t.dev.write.side_effect = EIO
    with caplog.at_level(logging.ERROR, logger="wifit3.chips.rtl8192cu.transport"):
        t.bulk_write(0x02, bytes(100), 512, 1000)
    assert not t.removed
    assert "Urb has error status 0xFFFFFFFB" in caplog.text


def test_short_tx_urb_counts_as_sent():
    t = _transport([])
    t.dev.write.return_value = 10
    t.bulk_write(0x02, bytes(100), 512, 1000)


def test_tx_enodev_raises_and_sends_nothing_after():
    t = _transport([])
    t.dev.write.side_effect = ENODEV
    with pytest.raises(usb.core.USBError):
        t.bulk_write(0x02, bytes(100), 512, 1000)
    t.dev.write.reset_mock()
    with pytest.raises(usb.core.USBError) as e:
        t.bulk_write(0x02, bytes(100), 512, 1000)
    assert e.value.errno == errno.ENODEV
    t.dev.write.assert_not_called()


async def test_set_channel_rejects_an_unsupported_channel():
    entry = next(d for d in SUPPORTED_IDS if (d.vid, d.pid) == (0x0BDA, 0x8176))
    driver = RTL8192CUDriver.from_usb_device(MagicMock(), entry)
    with pytest.raises(SetChannelError):
        await driver.set_channel(15)
