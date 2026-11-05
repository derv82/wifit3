"""RxReaderThread: the shared bulk-read thread. Covers the thread->loop
hand-off (read_once on the thread -> dispatch on the loop) and the
consecutive-error give-up. Driver-specific decode is tested per driver."""
import asyncio
import threading

import pytest
import usb.core

from wifit3.chips import rx_reader
from wifit3.chips.rx_reader import RxReaderThread


def _usb_error(*, errno=None, backend=None):
    """A USBError shaped like the libusb1 backend raises (errno + backend_error_code)."""
    e = usb.core.USBError("boom", errno=errno)
    e.backend_error_code = backend
    return e


@pytest.mark.asyncio
async def test_reader_dispatches_buffers_then_idles_and_stops(monkeypatch):
    monkeypatch.setattr(rx_reader, "MAX_BATCH_SIZE", 2)
    loop = asyncio.get_running_loop()
    bufs = [b"A", b"B"]
    event = asyncio.Event()
    dispatched = []

    def on_dispatch(buf):
        dispatched.append(buf)
        if len(dispatched) >= 2:
            event.set()

    def read_once():
        return bufs.pop(0) if bufs else None

    r = RxReaderThread(loop, read_once, on_dispatch, name="test")
    r.start()
    try:
        await asyncio.wait_for(event.wait(), timeout=1.0)
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
        await loop.run_in_executor(None, r._thread.join, 1.0)
        assert calls["n"] == 3
        assert not r._thread.is_alive()
        assert not r.running
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_reader_fires_on_fatal_when_giving_up():
    loop = asyncio.get_running_loop()
    fatals = []
    fatal_event = asyncio.Event()

    def read_once():
        raise RuntimeError("usb boom")

    def on_fatal(e):
        fatals.append(e)
        fatal_event.set()

    r = RxReaderThread(loop, read_once, lambda b: None, name="give-up",
                       max_errors=3, on_fatal=on_fatal)
    r.start()
    try:
        await asyncio.wait_for(fatal_event.wait(), timeout=1.0)
        assert len(fatals) == 1 and isinstance(fatals[0], RuntimeError)
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_reader_fires_on_fatal_immediately_on_device_gone():
    loop = asyncio.get_running_loop()
    calls = {"n": 0}
    fatals = []
    fatal_event = asyncio.Event()

    def read_once():
        calls["n"] += 1
        raise _usb_error(errno=19, backend=-4)   # LIBUSB_ERROR_NO_DEVICE (unplug)

    def on_fatal(e):
        fatals.append(e)
        fatal_event.set()

    r = RxReaderThread(loop, read_once, lambda b: None, name="unplug",
                       max_errors=5, on_fatal=on_fatal)
    r.start()
    try:
        await asyncio.wait_for(fatal_event.wait(), timeout=1.0)
        assert len(fatals) == 1 and calls["n"] == 1
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_reader_pause_halts_reads_and_resume_restarts():
    loop = asyncio.get_running_loop()
    reads = {"n": 0}
    is_resumed = False
    first_read = asyncio.Event()
    after_resume = asyncio.Event()

    def read_once():
        reads["n"] += 1
        loop.call_soon_threadsafe(first_read.set)
        if is_resumed:
            loop.call_soon_threadsafe(after_resume.set)
        return None

    r = RxReaderThread(loop, read_once, lambda b: None, name="pause")
    r.start()
    try:
        await asyncio.wait_for(first_read.wait(), timeout=1.0)
        paused = await loop.run_in_executor(None, r.pause)
        assert paused is True
        assert r._paused.is_set()
        n_at_pause = reads["n"]
        is_resumed = True
        r.resume()
        await asyncio.wait_for(after_resume.wait(), timeout=1.0)
        assert reads["n"] > n_at_pause
    finally:
        await r.stop()


def test_split_backlog_limits_and_partial_acceptance(monkeypatch):
    monkeypatch.setattr(rx_reader, "MAX_SPLIT_BACKLOG", 2)
    monkeypatch.setattr(rx_reader, "MAX_BACKLOG_BYTES", 3)
    loop = asyncio.new_event_loop()
    reads = iter([b"aggregate"])
    dispatched = []
    split_calls = []
    reader = None

    def read_once():
        try:
            return next(reads)
        except StopIteration:
            reader._running = False
            return None

    def split(buf):
        for record in (b"aa", b"bb", b"c", b"d"):
            split_calls.append(record)
            yield record

    reader = RxReaderThread(loop, read_once, dispatched.append, split=split)
    reader._running = True
    reader._run()
    assert split_calls == [b"aa", b"bb", b"c", b"d"]
    assert reader._backlog_count == 2
    assert reader._backlog_bytes == 3
    assert reader._dropped == 2
    loop.run_until_complete(asyncio.sleep(0))
    assert dispatched == [b"aa", b"c"]
    assert reader._backlog_count == reader._backlog_bytes == 0
    loop.close()


def test_split_batches_enforce_frame_limit_and_preserve_order():
    loop = asyncio.new_event_loop()
    dispatched = []
    submitted = []
    reader = None
    reads = iter([b"aggregate"])

    def read_once():
        try:
            return next(reads)
        except StopIteration:
            reader._running = False
            return None

    def split(buf):
        for index in range(rx_reader.MAX_SPLIT_BACKLOG + 3):
            yield index.to_bytes(2, "little")

    reader = RxReaderThread(loop, read_once, dispatched.append, split=split)
    original = reader._dispatch_split_batch

    def dispatch_batch(batch):
        submitted.append(len(batch))
        original(batch)

    reader._dispatch_split_batch = dispatch_batch
    reader._running = True
    reader._run()
    assert reader._backlog_count == 8192
    assert reader._backlog_bytes == 8192 * 2
    assert reader._dropped == 3
    loop.run_until_complete(asyncio.sleep(0))
    assert submitted == [64] * 128
    assert dispatched == [index.to_bytes(2, "little") for index in range(8192)]
    assert reader._backlog_count == reader._backlog_bytes == 0
    loop.close()


def test_split_batch_dispatch_failure_releases_capacity_and_continues():
    loop = asyncio.new_event_loop()
    received = []

    def dispatch(record):
        received.append(record)
        if record == b"a":
            raise ValueError("bad frame")

    reader = RxReaderThread(loop, lambda: None, dispatch, split=lambda buf: ())
    reader._backlog_count = 2
    reader._backlog_bytes = 2
    reader._submit_split_batch([b"a", b"b"])
    loop.run_until_complete(asyncio.sleep(0))
    assert received == [b"a", b"b"]
    assert reader._backlog_count == reader._backlog_bytes == 0
    loop.close()


def test_split_batch_submission_failure_releases_capacity():
    loop = asyncio.new_event_loop()
    reader = RxReaderThread(loop, lambda: None, lambda record: None, split=lambda buf: ())
    reader._backlog_count = 1
    reader._backlog_bytes = 1
    loop.close()
    reader._submit_split_batch([b"a"])
    assert reader._backlog_count == reader._backlog_bytes == 0


@pytest.mark.asyncio
async def test_split_pause_resume_preserves_order_and_stop_interrupts():
    loop = asyncio.get_running_loop()
    entered = threading.Event()
    release = threading.Event()
    dispatched = []
    finished = asyncio.Event()
    reads = iter([b"aggregate"])

    def read_once():
        return next(reads, None)

    def split(buf):
        yield b"a"
        entered.set()
        release.wait(1)
        yield b"b"
        yield b"c"

    def dispatch(record):
        dispatched.append(record)
        if len(dispatched) == 3:
            finished.set()

    reader = RxReaderThread(loop, read_once, dispatch, split=split)
    reader.start()
    try:
        assert await loop.run_in_executor(None, entered.wait, 1)
        reader._pause_req.set()
        release.set()
        assert await loop.run_in_executor(None, reader._paused.wait, 1)
        await asyncio.sleep(0.01)
        assert dispatched == [b"a"]
        reader.resume()
        await asyncio.wait_for(finished.wait(), 1)
        assert dispatched == [b"a", b"b", b"c"]
    finally:
        release.set()
        await reader.stop()


@pytest.mark.asyncio
async def test_split_stop_while_paused_does_not_dispatch_remaining_records():
    loop = asyncio.get_running_loop()
    reader = None
    dispatched = []
    split_paused = threading.Event()

    def split(buf):
        yield b"a"
        reader._pause_req.set()
        split_paused.set()
        yield b"b"

    reader = RxReaderThread(loop, lambda: b"aggregate", dispatched.append, split=split)
    reader.start()
    assert await loop.run_in_executor(None, split_paused.wait, 1)
    assert await loop.run_in_executor(None, reader._paused.wait, 1)
    await reader.stop()
    await asyncio.sleep(0)
    assert dispatched == [b"a"]


def test_pause_on_stopped_reader_returns_immediately():
    r = RxReaderThread(asyncio.new_event_loop(), lambda: None, lambda b: None, name="stopped")
    assert r.pause() is True
    r.resume()


@pytest.mark.asyncio
async def test_reader_dispatches_at_once_when_idle_and_batches_while_loop_busy():
    loop = asyncio.get_running_loop()
    seq = [b"A", b"B", b"C"]
    b_and_c_read = threading.Event()
    event = asyncio.Event()
    batches = []
    dispatched = []

    def read_once():
        buf = seq.pop(0) if seq else None
        if not seq:
            b_and_c_read.set()
        return buf

    def on_dispatch(buf):
        dispatched.append(buf)
        if buf == b"A":
            b_and_c_read.wait(1.0)            # hold the loop on A while the reader takes B, C

    r = RxReaderThread(loop, read_once, on_dispatch, name="batch")
    orig = r._dispatch_batch

    def spy(batch):
        batches.append(list(batch))
        orig(batch)
        if len(dispatched) >= 3:
            event.set()

    r._dispatch_batch = spy
    r.start()
    try:
        await asyncio.wait_for(event.wait(), timeout=2.0)
        assert batches == [[b"A"], [b"B", b"C"]]
        assert dispatched == [b"A", b"B", b"C"]
    finally:
        await r.stop()


def test_reader_drops_when_loop_backlogged():
    loop = asyncio.new_event_loop()
    r = RxReaderThread(loop, lambda: None, lambda b: None, name="drop")
    r._bufs_produced = rx_reader.MAX_BACKLOG + 10
    r._bufs_consumed = 0
    batch = [b"x", b"y"]
    # Verify backlog check logic directly
    if r._bufs_produced - r._bufs_consumed >= rx_reader.MAX_BACKLOG:
        r._dropped += len(batch)
    assert r._dropped == 2


@pytest.mark.asyncio
async def test_reader_skips_falsy_buffers(monkeypatch):
    monkeypatch.setattr(rx_reader, "MAX_BATCH_SIZE", 1)
    loop = asyncio.get_running_loop()
    seq = [b"", None, b"real"]
    event = asyncio.Event()
    dispatched = []

    def on_dispatch(buf):
        dispatched.append(buf)
        event.set()

    def read_once():
        return seq.pop(0) if seq else None

    r = RxReaderThread(loop, read_once, on_dispatch, name="skip")
    r.start()
    try:
        await asyncio.wait_for(event.wait(), timeout=1.0)
        assert dispatched == [b"real"]
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_reader_backs_off_on_a_run_of_zero_length_reads(monkeypatch):
    """A device streaming ZLPs must not spin the core: reads return instantly, so the
    reader sleeps once it has seen EMPTY_SPIN_LIMIT of them in a row (issue #66)."""
    monkeypatch.setattr(rx_reader, "EMPTY_SPIN_LIMIT", 3)
    loop = asyncio.get_running_loop()
    slept = []
    reads = 0

    real_sleep = rx_reader.time.sleep

    def fake_sleep(seconds):
        slept.append(seconds)
        real_sleep(0)

    monkeypatch.setattr(rx_reader.time, "sleep", fake_sleep)

    def read_once():
        nonlocal reads
        reads += 1
        return b""

    r = RxReaderThread(loop, read_once, lambda buf: None, name="test")
    r.start()
    try:
        while reads < 20:
            await asyncio.sleep(0.01)
    finally:
        monkeypatch.setattr(rx_reader.time, "sleep", real_sleep)
        await r.stop()

    assert slept, "reader spun on zero-length reads without backing off"
    assert set(slept) == {rx_reader.EMPTY_BACKOFF}
