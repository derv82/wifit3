"""RxReaderThread: the shared bulk-read thread. Covers the thread->loop
hand-off (read_once on the thread -> dispatch on the loop) and the
consecutive-error give-up. Driver-specific decode is tested per driver."""
import asyncio

import pytest
import usb.core

from wifit3.chips.rx_reader import RxReaderThread


def _usb_error(*, errno=None, backend=None):
    """A USBError shaped like the libusb1 backend raises (errno + backend_error_code)."""
    e = usb.core.USBError("boom", errno=errno)
    e.backend_error_code = backend
    return e


@pytest.mark.asyncio
async def test_reader_dispatches_buffers_then_idles_and_stops():
    loop = asyncio.get_running_loop()
    bufs = [b"A", b"B"]

    def read_once():
        return bufs.pop(0) if bufs else None  # None = benign timeout (idle)

    dispatched = []
    r = RxReaderThread(loop, read_once, dispatched.append, name="test")
    r.start()
    try:
        for _ in range(50):
            if len(dispatched) >= 2:
                break
            await asyncio.sleep(0.02)
        # Buffers reached the loop in order, dispatched on the loop thread.
        assert dispatched == [b"A", b"B"]
    finally:
        await r.stop()
    assert r._thread is None and not r.running


@pytest.mark.asyncio
async def test_reader_gives_up_after_consecutive_errors():
    loop = asyncio.get_running_loop()
    calls = {"n": 0}

    def read_once():
        calls["n"] += 1
        raise RuntimeError("usb boom")

    r = RxReaderThread(loop, read_once, lambda b: None, name="err", max_errors=3)
    r.start()
    try:
        for _ in range(100):
            if not (r._thread and r._thread.is_alive()):
                break
            await asyncio.sleep(0.02)
        # Bails after exactly max_errors reads, doesn't spin forever.
        assert calls["n"] == 3
        assert not r._thread.is_alive()
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_reader_fires_on_fatal_when_giving_up():
    loop = asyncio.get_running_loop()

    def read_once():
        raise RuntimeError("usb boom")   # not device-gone → rides the strike count

    fatals = []
    r = RxReaderThread(loop, read_once, lambda b: None, name="give-up",
                       max_errors=3, on_fatal=fatals.append)
    r.start()
    try:
        for _ in range(100):
            if fatals:
                break
            await asyncio.sleep(0.02)
        # on_fatal fired exactly once, carrying the last error, after the give-up.
        assert len(fatals) == 1 and isinstance(fatals[0], RuntimeError)
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_reader_fires_on_fatal_immediately_on_device_gone():
    loop = asyncio.get_running_loop()
    calls = {"n": 0}

    def read_once():
        calls["n"] += 1
        raise _usb_error(errno=19, backend=-4)   # LIBUSB_ERROR_NO_DEVICE (unplug)

    fatals = []
    r = RxReaderThread(loop, read_once, lambda b: None, name="unplug",
                       max_errors=5, on_fatal=fatals.append)
    r.start()
    try:
        for _ in range(100):
            if fatals:
                break
            await asyncio.sleep(0.02)
        # Bailed on the FIRST device-gone read — didn't wait out the 5-strike count.
        assert len(fatals) == 1 and calls["n"] == 1
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_reader_skips_falsy_buffers():
    loop = asyncio.get_running_loop()
    seq = [b"", None, b"real"]

    def read_once():
        return seq.pop(0) if seq else None

    dispatched = []
    r = RxReaderThread(loop, read_once, dispatched.append, name="skip")
    r.start()
    try:
        for _ in range(50):
            if dispatched:
                break
            await asyncio.sleep(0.02)
        assert dispatched == [b"real"]  # empty + None skipped, not dispatched
    finally:
        await r.stop()
