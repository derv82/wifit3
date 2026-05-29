"""`McuChannel.drain_response_queue` — flushes stale MCU responses left in
EP_IN_CMD_RESP from a previous session.

The root cause of the ~1-in-10 warm-boot failure: chip firmware still running,
leftover response in the bulk-IN queue → first wait_resp MCU command sees
the stale seq, retries, times out.
"""
import struct
from unittest.mock import AsyncMock, MagicMock

import pytest

from wifit3.chips.mt76x2u import constants as C
from wifit3.chips.mt76x2u.mcu import McuChannel


def _stale_response(seq: int = 4, evt: int = 0) -> bytes:
    """Build a fake RXFCE header — only the first 4 bytes are inspected.
    Mirrors the encoding used by McuChannel._next_seq + the wait_resp parser."""
    rxfce = ((seq & 0xF) << 16) | ((evt & 0xF) << 20)
    return struct.pack("<I", rxfce) + b"\x00" * 12


def _make_mcu_with_reads(read_results: list) -> McuChannel:
    """Build a McuChannel whose `transport.async_read_bulk` returns the
    given results in order. Each result is either bytes (returned as-is)
    or a callable side_effect like `raises(TimeoutError)`."""
    transport = MagicMock()
    transport.async_read_bulk = AsyncMock(side_effect=read_results)
    return McuChannel(transport)


# ---------------------------------------------------------------------------
# drain_response_queue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_returns_zero_when_queue_is_empty():
    """First read raises a timeout → queue empty → return 0 without retry."""
    mcu = _make_mcu_with_reads([TimeoutError("read timed out")])
    drained = await mcu.drain_response_queue()
    assert drained == 0
    # Only one read was issued.
    assert mcu.transport.async_read_bulk.call_count == 1


@pytest.mark.asyncio
async def test_drain_returns_zero_when_first_read_empty():
    """Empty data also signals end-of-queue (not a timeout, just no data)."""
    mcu = _make_mcu_with_reads([b""])
    drained = await mcu.drain_response_queue()
    assert drained == 0


@pytest.mark.asyncio
async def test_drain_one_stale_response_then_empty():
    """The bug-fix scenario: exactly one stale seq=4 response sitting in the
    queue from the previous session."""
    mcu = _make_mcu_with_reads([
        _stale_response(seq=4, evt=0),
        TimeoutError("queue empty"),
    ])
    drained = await mcu.drain_response_queue()
    assert drained == 1


@pytest.mark.asyncio
async def test_drain_multiple_stale_responses():
    """Multiple stale responses get drained in order."""
    mcu = _make_mcu_with_reads([
        _stale_response(seq=3),
        _stale_response(seq=4),
        _stale_response(seq=5),
        TimeoutError("queue empty"),
    ])
    drained = await mcu.drain_response_queue()
    assert drained == 3


@pytest.mark.asyncio
async def test_drain_caps_at_max_drain():
    """Safety cap — if the chip is wedged feeding responses, we don't loop
    forever. Cap defaults to 16."""
    # 20 stale responses then nothing (but cap should stop at 16).
    reads = [_stale_response(seq=i % 16) for i in range(20)]
    mcu = _make_mcu_with_reads(reads)
    drained = await mcu.drain_response_queue(max_drain=16)
    assert drained == 16


@pytest.mark.asyncio
async def test_drain_respects_custom_max_drain():
    mcu = _make_mcu_with_reads([_stale_response() for _ in range(10)])
    drained = await mcu.drain_response_queue(max_drain=3)
    assert drained == 3


@pytest.mark.asyncio
async def test_drain_passes_short_timeout_to_each_read():
    """Each read uses the per_read_timeout_ms knob (default 20 ms)."""
    mcu = _make_mcu_with_reads([TimeoutError()])
    await mcu.drain_response_queue(per_read_timeout_ms=15)
    args, kwargs = mcu.transport.async_read_bulk.call_args
    # First positional arg is the endpoint, second is the buffer size.
    assert args[0] == C.EP_IN_CMD_RESP
    assert kwargs.get("timeout_ms") == 15
