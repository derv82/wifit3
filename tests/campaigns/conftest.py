import asyncio

import pytest

_real_sleep = asyncio.sleep


@pytest.fixture(autouse=True)
def _collapse_sleep(monkeypatch):
    # Campaign sleeps are pacing; the tests assert ordering, so collapse them to a bare yield.
    async def fast_sleep(delay, *a, **k):
        return await _real_sleep(0, *a, **k)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
