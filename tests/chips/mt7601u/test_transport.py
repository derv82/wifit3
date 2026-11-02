"""Transport tests for the MT7601U port.

No hardware: PyUSB's device is faked. These pin the shapes PyUSB actually returns,
which differ from the shapes a naive reading of the C suggests.
"""
from __future__ import annotations

from array import array


from wifit3.chips.mt7601u import constants as C
from wifit3.chips.mt7601u.transport import MT7601UTransport


class FakeUsbDevice:
    """Returns whatever the test asks for, so the transport sees real PyUSB shapes."""

    def __init__(self, in_return=None, out_return: int = 0) -> None:
        self.in_return = in_return
        self.out_return = out_return
        self.transfers: list[dict] = []

    def ctrl_transfer(self, **kwargs):
        self.transfers.append(kwargs)
        if kwargs["bmRequestType"] & 0x80:          # device -> host
            return self.in_return
        return self.out_return


def make_device(fake: FakeUsbDevice) -> MT7601UTransport:
    tp = MT7601UTransport.__new__(MT7601UTransport)
    tp.dev = fake
    tp.timeout_ms = 300
    tp._in_buf = bytearray(C.MT_VEND_BUF if hasattr(C, "MT_VEND_BUF") else 0x40)
    return tp


class TestReadControlTransferReturnShape:
    """PyUSB returns an array.array of bytes for an IN transfer, not a length."""

    def test_an_array_return_is_read_as_a_full_register(self) -> None:
        """An array of MT_VEND_BUF bytes is a successful read."""
        from wifit3.chips.mt7601u.transport import MT_VEND_BUF
        fake = FakeUsbDevice(in_return=array("B", [0x78, 0x56, 0x34, 0x12] + [0] * (MT_VEND_BUF - 4)))
        tp = make_device(fake)
        tp._in_buf = bytearray(MT_VEND_BUF)
        assert tp.rr(0x0013B0) == 0x12345678

    def test_an_array_return_never_raises_a_type_error(self) -> None:
        """The bug this pins: `elif got > 0` compared an array to an int."""
        from wifit3.chips.mt7601u.transport import MT_VEND_BUF
        fake = FakeUsbDevice(in_return=array("B", [0] * MT_VEND_BUF))
        tp = make_device(fake)
        tp._in_buf = bytearray(MT_VEND_BUF)
        tp.rr(0x000500)                    # must not raise

    def test_a_short_read_returns_all_ones(self) -> None:
        """usb.c:137 returns ~0 when the transfer did not deliver a full word."""
        from wifit3.chips.mt7601u.transport import MT_VEND_BUF
        fake = FakeUsbDevice(in_return=array("B", [0xAB, 0xCD]))
        tp = make_device(fake)
        tp._in_buf = bytearray(MT_VEND_BUF)
        assert tp.rr(0x000500) == 0xFFFFFFFF

    def test_an_integer_return_still_works(self) -> None:
        """A device that returns a plain length must not regress."""
        fake = FakeUsbDevice(in_return=4, out_return=0)
        tp = make_device(fake)
        tp._in_buf = bytearray(0x40)
        tp.rr(0x000500)


class TestWriteSplitsHalves:
    def test_a_register_write_is_two_out_transfers(self) -> None:
        fake = FakeUsbDevice(in_return=None, out_return=0)
        tp = make_device(fake)
        tp.wr(0x0013B0, 0x2F2F0009)
        outs = [t for t in fake.transfers if not t["bmRequestType"] & 0x80]
        assert [t["wValue"] for t in outs] == [0x0009, 0x2F2F]
        assert [t["wIndex"] for t in outs] == [0x13B0, 0x13B2]

    def test_the_request_type_matches_the_register_write_constant(self) -> None:
        from wifit3.chips.mt7601u.transport import MT_VEND_WRITE
        fake = FakeUsbDevice(in_return=None, out_return=0)
        tp = make_device(fake)
        tp.wr(0x0013B0, 0x1)
        assert fake.transfers[0]["bRequest"] == MT_VEND_WRITE


class FakeRxDevice(FakeUsbDevice):
    """A device whose bulk-IN read either returns a frame buffer or raises, as told."""

    def __init__(self, error: Exception) -> None:
        super().__init__(in_return=None, out_return=0)
        self.error = error
        self.read_timeouts: list = []

    def read(self, endpoint, size, timeout=None):
        self.read_timeouts.append(timeout)
        raise self.error


def rx_device(error: Exception) -> MT7601UTransport:
    tp = make_device(FakeRxDevice(error))
    tp.in_eps = {0: 0x84}
    return tp


class TestBulkInRxIsInterruptible:
    """The RX read must be finite and must not report a quiet interval as a fault.

    An infinite read leaves the reader thread blocked past close(), and the release
    that follows then surfaces as a spurious ENODEV unplug on an orderly shutdown.
    """

    def test_the_read_timeout_is_finite(self) -> None:
        from wifit3.chips.mt7601u.transport import _RX_TIMEOUT_MS
        assert 0 < _RX_TIMEOUT_MS < 1500, "must expire well inside the reader's join timeout"

    def test_a_quiet_interval_returns_no_bytes_rather_than_raising(self) -> None:
        import usb.core
        fake = FakeRxDevice(usb.core.USBTimeoutError("timed out", 110, 110))
        tp = make_device(fake)
        tp.in_eps = {0: 0x84}
        assert tp.bulk_in_rx(0x8000) == b""

    def test_a_real_disconnect_still_propagates(self) -> None:
        import pytest
        import usb.core
        gone = usb.core.USBError("no device", 19, 19)
        gone.errno = 19
        with pytest.raises(usb.core.USBError):
            rx_device(gone).bulk_in_rx(0x8000)

    def test_the_read_is_bounded_by_the_rx_timeout(self) -> None:
        import usb.core
        fake = FakeRxDevice(usb.core.USBTimeoutError("timed out", 110, 110))
        tp = make_device(fake)
        tp.in_eps = {0: 0x84}
        tp.bulk_in_rx(0x8000)
        assert fake.read_timeouts == [200]
