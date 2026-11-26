import errno
import itertools
import logging
from unittest.mock import MagicMock

import pytest
import usb.core

import wifit3.chips.rt5572.transport as transport_module
from wifit3.chips.rt5572 import bbp, eeprom, firmware
from wifit3.chips.rt5572 import constants as C
from wifit3.chips.rt5572.transport import RT5572Transport
from wifit3.errors import is_device_gone


@pytest.fixture(autouse=True)
def _fake_monotonic_clock(monkeypatch):
    """Each clock read moves it 10 ms, so the 100 ms retry window runs out without a real wait."""
    clock = itertools.count(step=0.01)
    monkeypatch.setattr(transport_module.time, "monotonic", lambda: next(clock))


def test_vendor_request_retries_a_failed_transfer():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = [usb.core.USBError("pipe", errno=errno.EPIPE), bytes([1, 0, 0, 0])]
    assert RT5572Transport(dev).read32(0x1000) == 1
    assert dev.ctrl_transfer.call_count == 2


def test_a_failed_register_read_reads_0_and_carries_on():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe", errno=errno.EPIPE)
    transport = RT5572Transport(dev)
    assert transport.read32(0x1000) == 0
    assert transport.device_present is True


def test_a_failed_register_write_carries_on():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe", errno=errno.EPIPE)
    RT5572Transport(dev).write32(0x1000, 1)


def test_enodev_marks_the_device_gone_and_later_ops_skip_usb():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("no device", errno=errno.ENODEV)
    transport = RT5572Transport(dev)
    with pytest.raises(usb.core.USBError):
        transport.write32(0x1000, 1)
    dev.ctrl_transfer.reset_mock()
    with pytest.raises(usb.core.USBError) as excinfo:
        transport.read32(0x1000)
    assert is_device_gone(excinfo.value)
    dev.ctrl_transfer.assert_not_called()


def test_four_eio_in_a_row_after_bring_up_mark_the_device_gone():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("io", errno=errno.EIO)
    transport = RT5572Transport(dev)
    transport.device_started = True
    with pytest.raises(usb.core.USBError) as excinfo:
        transport.write32(0x1000, 1)
    assert is_device_gone(excinfo.value)
    assert dev.ctrl_transfer.call_count == 4


def test_eio_during_bring_up_never_marks_the_device_gone():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("io", errno=errno.EIO)
    transport = RT5572Transport(dev)
    transport.write32(0x1000, 1)
    assert transport.device_present is True


def test_a_bbp_stuck_busy_is_polled_20_times_then_logged_and_the_write_skipped(monkeypatch, caplog):
    """rt2x00usb_regbusy_read: udelay(REGISTER_BUSY_DELAY) per try, then rt2x00_err (rt2x00usb.c:155-163)."""
    sleeps = []
    monkeypatch.setattr(bbp.time, "sleep", sleeps.append)
    dev = MagicMock()
    dev.ctrl_transfer.return_value = C.BBP_CSR_CFG_BUSY.to_bytes(4, "little")
    bbp.bbp_write(RT5572Transport(dev), 4, 0x40)
    assert dev.ctrl_transfer.call_count == C.REGISTER_USB_BUSY_COUNT
    assert sleeps == [C.REGISTER_BUSY_DELAY / 1_000_000] * C.REGISTER_USB_BUSY_COUNT
    assert [r.levelno for r in caplog.records] == [logging.ERROR]
    assert caplog.records[0].getMessage() == "Indirect register access failed: offset=0x0000101c, value=0x00020000"


def test_a_failed_vendor_request_logs_the_negative_errno(caplog):
    """rt2x00usb_vendor_request logs the status with %d (rt2x00usb.c:75-77)."""
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe", errno=errno.EPIPE)
    RT5572Transport(dev).write32(0x1000, 1)
    assert [r.getMessage() for r in caplog.records] == [
        "Vendor Request 0x06 failed for offset 0x1000 with error -32"]


def test_a_stuck_mcu_is_polled_20_times_then_logged_and_the_command_dropped(monkeypatch, caplog):
    """WAIT_FOR_MCU is rt2x00usb_regbusy_read; on failure rt2800_mcu_request skips both writes
    (rt2800lib.c:62-64, :533-544; rt2x00usb.c:154-163)."""
    sleeps = []
    monkeypatch.setattr(firmware.time, "sleep", sleeps.append)
    dev = MagicMock()
    dev.ctrl_transfer.return_value = C.H2M_MAILBOX_CSR_OWNER.to_bytes(4, "little")
    firmware.mcu_request(RT5572Transport(dev), C.MCU_WAKEUP, token=0xFF, arg0=0, arg1=2)
    assert dev.ctrl_transfer.call_count == C.REGISTER_USB_BUSY_COUNT
    assert sleeps == [C.REGISTER_BUSY_DELAY / 1_000_000] * C.REGISTER_USB_BUSY_COUNT
    assert [r.levelno for r in caplog.records] == [logging.ERROR]
    assert caplog.records[0].getMessage() == "Indirect register access failed: offset=0x00007010, value=0xff000000"


def test_a_stuck_efuse_kick_is_polled_20_times_then_logged_and_the_data_read_anyway(monkeypatch, caplog):
    """rt2800_efuse_read ignores rt2800_regbusy_read's result (rt2800lib.c:10940-10950)."""
    sleeps = []
    monkeypatch.setattr(eeprom.time, "sleep", sleeps.append)
    dev = MagicMock()
    dev.ctrl_transfer.return_value = eeprom.EFUSE_CTRL_KICK.to_bytes(4, "little")
    chunk = eeprom._efuse_read_chunk(RT5572Transport(dev), 0)
    assert dev.ctrl_transfer.call_count == 1 + 1 + C.REGISTER_USB_BUSY_COUNT + 4
    assert chunk == eeprom.EFUSE_CTRL_KICK.to_bytes(4, "little") * 4
    assert sleeps == [C.REGISTER_BUSY_DELAY / 1_000_000] * C.REGISTER_USB_BUSY_COUNT
    assert [r.levelno for r in caplog.records] == [logging.ERROR]
    assert caplog.records[0].getMessage() == "Indirect register access failed: offset=0x00000580, value=0x40000000"
