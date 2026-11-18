import errno
from unittest.mock import MagicMock

import pytest
import usb.core

from wifit3.chips.mt7601u.mcu import MCU_RESP_RETRIES, McuTimeout, MT7601UMcu
from wifit3.chips.mt7601u.transport import MT7601UTransport
from wifit3.chips.mt7601u.tx_ring import TxQueue
from wifit3.errors import is_device_gone


def _transport(dev) -> MT7601UTransport:
    tp = MT7601UTransport.__new__(MT7601UTransport)
    tp.dev = dev
    tp.timeout_ms = 300
    tp.removed = False
    tp._in_buf = bytearray(0x40)
    return tp


def test_a_read_that_fails_every_retry_reads_all_ones_and_carries_on():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("pipe", errno=errno.EPIPE)
    tp = _transport(dev)
    assert tp.rr(0x1000) == 0xFFFFFFFF
    assert tp.removed is False


def test_enodev_marks_the_device_removed_and_later_reads_skip_usb():
    dev = MagicMock()
    dev.ctrl_transfer.side_effect = usb.core.USBError("no device", errno=errno.ENODEV)
    tp = _transport(dev)
    with pytest.raises(usb.core.USBError):
        tp.rr(0x1000)
    dev.ctrl_transfer.reset_mock()
    with pytest.raises(usb.core.USBError) as excinfo:
        tp.rr(0x1000)
    assert is_device_gone(excinfo.value)
    dev.ctrl_transfer.assert_not_called()


def test_wait_resp_retries_a_response_timeout():
    """mt7601u_mcu_wait_resp retries a timed-out response read (mcu.c:74-79)."""
    mcu = MT7601UMcu.__new__(MT7601UMcu)
    mcu.tp = MagicMock()
    mcu.tp.bulk_in_resp.side_effect = usb.core.USBTimeoutError("timeout", errno=errno.ETIMEDOUT)
    with pytest.raises(McuTimeout):
        mcu.wait_resp(1)
    assert mcu.tp.bulk_in_resp.call_count == MCU_RESP_RETRIES


def test_a_failed_tx_submit_is_dropped_but_an_unplug_raises():
    transport = MagicMock()
    queue = TxQueue(transport, MagicMock())
    transport.bulk_out_tx.side_effect = usb.core.USBError("pipe", errno=errno.EPIPE)
    assert queue.submit(bytes(24)) is False
    transport.bulk_out_tx.side_effect = usb.core.USBError("no device", errno=errno.ENODEV)
    with pytest.raises(usb.core.USBError):
        queue.submit(bytes(24))
