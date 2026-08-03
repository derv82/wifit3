import asyncio

import pytest

_real_sleep = asyncio.sleep


@pytest.fixture(autouse=True)
def _collapse_sleep(monkeypatch):
    # Campaign pacing/backoff uses real asyncio.sleep; the tests assert event ordering,
    # not wall-clock, so collapse every sleep(delay>0) to a single event-loop yield.
    # (wait_for timeouts, e.g. the silent-AP path, aren't sleeps and still elapse.)
    async def fast_sleep(delay, *a, **k):
        return await _real_sleep(0, *a, **k)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
