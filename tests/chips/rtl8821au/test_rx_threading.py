"""RX reader-thread path: the dedicated thread must keep reading and hand
buffers to the loop, which parses + fires the callback — without that thread
blocking on the event loop. Validates the thread->loop->callback flow and a
clean shutdown (thread joins, no leak)."""
import asyncio

import pytest
import usb.core
from unittest.mock import MagicMock

from wifit3.chips.rtl8821au import driver as drv


@pytest.mark.asyncio
async def test_rx_reader_thread_dispatches_and_stops(monkeypatch):
    dev = MagicMock()
    # One real buffer, then perpetual "timeout" so the reader idles instead of
    # busy-spinning — exactly how a quiet channel behaves.
    bufs = [b"BULK"]

    def fake_read(ep, size, timeout):
        if bufs:
            return bufs.pop(0)
        raise usb.core.USBError("Operation timed out", errno=110)

    dev.read.side_effect = fake_read

    d = drv.RTL8821AUDriver(dev)
    d._bulk_in_ep = 0x84

    # Decode exactly one fake frame out of the sentinel buffer.
    monkeypatch.setattr(
        drv, "iter_bulk_frames",
        lambda buf: [(None, b"\x80mpdu", -42)] if buf == b"BULK" else [],
    )
    monkeypatch.setattr(
        drv.WlanFrameParser, "parse_80211_frame",
        staticmethod(lambda mpdu, rssi: {"type": "beacon", "rssi": rssi}),
    )

    received = []
    d.register_rx_callback(received.append)

    d._rx_running = True
    d._rx_task = asyncio.create_task(d._rx_loop())
    try:
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.02)
        assert received, "rx callback never fired — thread->loop dispatch broken"
        assert received[0] == {"type": "beacon", "rssi": -42}
    finally:
        d._rx_running = False
        await asyncio.wait_for(d._rx_task, timeout=2.0)

    # Reader thread exits on its own within ~one read timeout once stopped.
    for _ in range(50):
        if d._rx_thread is None or not d._rx_thread.is_alive():
            break
        await asyncio.sleep(0.02)
    assert d._rx_thread is not None and not d._rx_thread.is_alive()
