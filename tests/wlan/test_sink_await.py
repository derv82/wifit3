"""The sink await-API: next_frame (push, cross-card) and wait_until (state poll)."""
import asyncio
from types import SimpleNamespace

from wifit3.wlan.sink import WlanSink


async def test_next_frame_resolves_on_first_match():
    s = WlanSink()
    task = asyncio.create_task(s.next_frame(lambda p: p.type == "eapol", timeout=1.0))
    await asyncio.sleep(0)                                    # let the waiter register
    s.dispatch_rx(SimpleNamespace(type="beacon"))            # non-match, ignored
    s.dispatch_rx(SimpleNamespace(type="eapol", tag="hit"))  # match
    got = await task
    assert got.tag == "hit"
    assert s._waiters == []                                  # cleaned up on resolve


async def test_next_frame_times_out_to_none():
    s = WlanSink()
    got = await s.next_frame(lambda p: False, timeout=0.05)
    assert got is None
    assert s._waiters == []                                  # cleaned up on timeout


async def test_wait_until_polls_condition():
    s = WlanSink()
    flag = {"v": False}

    async def flip():
        await asyncio.sleep(0.02)
        flag["v"] = True

    asyncio.create_task(flip())
    assert await s.wait_until(lambda: flag["v"], timeout=1.0) is True
    assert await s.wait_until(lambda: False, timeout=0.05) is False
