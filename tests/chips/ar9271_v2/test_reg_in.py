"""REG_IN stays read from claim to close, like the C's REG_IN URBs: hif_usb.c:734-800, :964-1002."""
import errno
import threading
import time

import pytest
import usb.core

from wifit3.chips.ar9271_v2.transport import AR9271Transport


class FakeDev:
    """REG_IN returns ``results`` in order (bytes, or an exception to raise), then times out."""

    def __init__(self, results):
        self.results = list(results)
        self.reads = 0
        self.drained = threading.Event()

    def read(self, ep, length, timeout=None):
        self.reads += 1
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return bytearray(result)
        self.drained.set()
        time.sleep(0.001)
        raise usb.core.USBTimeoutError("Operation timed out")


def _started(dev):
    t = AR9271Transport(dev)
    t.alloc_reg_in_urbs()
    assert dev.drained.wait(1)
    return t


def test_frames_are_read_with_no_command_waiting():
    dev = FakeDev([b"\x01", b"\x02"])
    t = _started(dev)
    assert t.reg_in(10) == b"\x01"
    assert t.reg_in(10) == b"\x02"
    t.dealloc_reg_in_urbs()


def test_no_frame_times_out():
    t = _started(FakeDev([]))
    with pytest.raises(usb.core.USBTimeoutError):
        t.reg_in(10)
    t.dealloc_reg_in_urbs()


def test_error_resubmits():
    t = _started(FakeDev([usb.core.USBError("Input/Output Error", errno=errno.EIO), b"\x01"]))
    assert t.reg_in(10) == b"\x01"
    t.dealloc_reg_in_urbs()


def test_device_gone_stops_the_reads_and_marks_removed():
    dev = FakeDev([usb.core.USBError("No such device", errno=errno.ENODEV)])
    t = AR9271Transport(dev)
    t.alloc_reg_in_urbs()
    t.reg_in_thread.join(1)
    assert not t.reg_in_thread.is_alive()
    assert dev.reads == 1
    with pytest.raises(usb.core.USBError) as exc_info:
        t.reg_in(10)
    assert exc_info.value.errno == errno.ENODEV


def test_dealloc_stops_the_reads_and_reg_in_reads_directly():
    dev = FakeDev([])
    t = _started(dev)
    t.dealloc_reg_in_urbs()
    reads = dev.reads
    dev.results.append(b"\x03")
    assert t.reg_in(10) == b"\x03"
    assert dev.reads == reads + 1
