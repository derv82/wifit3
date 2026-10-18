"""Transport: vendor control-transfer wire format, timeouts, transfer serialisation."""
from __future__ import annotations

import threading
import time

from wifit3.chips.rtl8192eu.constants import (
    USB_CMD_REQ,
    USB_CONTROL_TIMEOUT_MS,
    USB_REQTYPE_READ,
    USB_REQTYPE_WRITE,
    USB_VENQT_CMD_IDX,
)
from wifit3.chips.rtl8192eu.transport import RTL8192EUTransport


class FakeDevice:
    """Records `ctrl_transfer` calls and flags any overlapping transfer."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.overlap = False
        self.on_transfer = None
        self._in_flight = threading.Lock()

    def ctrl_transfer(self, request_type, request, value, index, data_or_length, timeout):
        if not self._in_flight.acquire(blocking=False):
            self.overlap = True
            raise AssertionError("ctrl_transfer entered while another was in flight")
        try:
            self.calls.append(
                (request_type, request, value, index, data_or_length, timeout)
            )
            if self.on_transfer is not None:
                self.on_transfer()
            time.sleep(0.0002)
            if isinstance(data_or_length, int):
                return b"\x00" * data_or_length
            return len(data_or_length)
        finally:
            self._in_flight.release()


class RecordingLock:
    """RLock that counts acquisitions and reports whether it is held."""

    def __init__(self) -> None:
        self._inner = threading.RLock()
        self.entries = 0

    def __enter__(self) -> "RecordingLock":
        self._inner.acquire()
        self.entries += 1
        return self

    def __exit__(self, *exc_info) -> None:
        self._inner.release()

    def _is_owned(self) -> bool:
        return self._inner._is_owned()


def test_read_uses_the_vendor_request_wire_format():
    dev = FakeDevice()
    t = RTL8192EUTransport(dev, timeout_ms=123)

    assert t.read32(0x1234) == 0

    assert dev.calls == [
        (USB_REQTYPE_READ, USB_CMD_REQ, 0x1234, USB_VENQT_CMD_IDX, 4, 123)
    ]
    assert USB_REQTYPE_READ == 0xC0
    assert USB_CMD_REQ == 0x05
    assert USB_VENQT_CMD_IDX == 0x00


def test_write_uses_the_vendor_request_wire_format_and_default_timeout():
    dev = FakeDevice()
    t = RTL8192EUTransport(dev)

    t.write16(0x4321, 0xBEEF)

    request_type, request, value, index, payload, timeout = dev.calls[0]
    assert (request_type, request, value, index) == (
        USB_REQTYPE_WRITE,
        USB_CMD_REQ,
        0x4321,
        USB_VENQT_CMD_IDX,
    )
    assert bytes(payload) == b"\xef\xbe"
    assert timeout == USB_CONTROL_TIMEOUT_MS
    assert USB_REQTYPE_WRITE == 0x40


def test_reads_and_writes_take_the_same_lock_around_the_transfer():
    dev = FakeDevice()
    t = RTL8192EUTransport(dev)
    lock = RecordingLock()
    t._lock = lock
    held_during_transfer = []
    dev.on_transfer = lambda: held_during_transfer.append(lock._is_owned())

    t.read8(0x1000)
    t.write8(0x1000, 0)

    assert lock.entries == 2
    assert held_during_transfer == [True, True]


def test_concurrent_reads_and_writes_never_overlap():
    dev = FakeDevice()
    t = RTL8192EUTransport(dev)
    start = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(read_only: bool) -> None:
        start.wait()
        try:
            for i in range(20):
                if read_only:
                    t.read8(0x1000 + i)
                else:
                    t.write8(0x2000 + i, i)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(kind,)) for kind in (True, False)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert dev.overlap is False
