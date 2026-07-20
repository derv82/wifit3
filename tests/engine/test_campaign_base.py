"""Unit tests for the Campaign base lifecycle — engine-pure, no Textual.

Pins the framework contract the per-campaign migrations rely on: the class-level
``active`` radio mutex, start/stop, exit-driven teardown (on completion, stop, AND
crash).
"""
import asyncio

import pytest

from wifit3.campaigns.campaign import Campaign


@pytest.fixture(autouse=True)
def _reset_active():
    """``active`` is a class var — a leaked campaign would poison sibling tests."""
    Campaign.active = None
    yield
    Campaign.active = None


class _Looper(Campaign):
    """run() loops until stopped; records that it ran + tore down."""
    key = "loop"

    def __init__(self):
        super().__init__(ap=None, iface=None)
        self.ran = False
        self.tore_down = False
        self.loops = 0

    async def _loop(self):
        self.ran = True
        while not self.stopped:
            self.loops += 1
            await asyncio.sleep(0.001)

    async def teardown(self):
        self.tore_down = True


async def test_start_claims_radio_then_stop_releases_it():
    c = _Looper()
    assert c.run() is True
    assert Campaign.active is c
    await asyncio.sleep(0.005)
    assert c.ran and c.loops > 0
    await c.stop()
    assert c.done and c.tore_down
    assert Campaign.active is None


async def test_mutex_refuses_a_second_campaign():
    a, b = _Looper(), _Looper()
    assert a.run() is True
    assert b.run() is False           # radio owned → no-op
    assert Campaign.active is a
    assert b._task is None and not b.ran
    await a.stop()
    assert b.run() is True             # freed → now b may run
    await b.stop()


async def test_teardown_runs_on_natural_completion_without_stop():
    class OneShot(Campaign):
        def __init__(self):
            super().__init__(None, None)
            self.tore_down = False

        async def _loop(self):
            return                       # finishes on its own

        async def teardown(self):
            self.tore_down = True

    c = OneShot()
    c.run()
    await c._task
    assert c.tore_down                   # fired even though nobody called stop()
    assert Campaign.active is None       # radio released on its own


async def test_crash_in_run_is_contained_and_releases_radio(caplog):
    class Boom(Campaign):
        key = "boom"

        def __init__(self):
            super().__init__(None, None)
            self.tore_down = False

        async def _loop(self):
            raise RuntimeError("kaboom")

        async def teardown(self):
            self.tore_down = True

    c = Boom()
    c.run()
    await c._task                        # does NOT raise — backstop swallows it
    assert c.tore_down                   # teardown still ran
    assert Campaign.active is None       # mutex released
    assert "crashed in _loop()" in caplog.text


async def test_crash_in_teardown_still_releases_radio(caplog):
    class BadTeardown(Campaign):
        key = "badtd"

        async def _loop(self):
            return

        async def teardown(self):
            raise RuntimeError("teardown boom")

    c = BadTeardown(None, None)
    c.run()
    await c._task
    assert Campaign.active is None       # released despite teardown crash
    assert "crashed in teardown()" in caplog.text


async def test_request_stop_frees_slot_synchronously_and_drains():
    c = _Looper()
    c.run()
    await asyncio.sleep(0.003)
    c.request_stop()
    assert Campaign.active is None           # freed immediately, no await
    assert c.stopped is True
    await c._task                            # the task drains + tears down on its own
    assert c.tore_down


async def test_draining_teardown_does_not_clobber_a_new_campaign():
    a = _Looper()
    a.run()
    await asyncio.sleep(0.003)
    a.request_stop()                         # frees the slot; a is still draining
    b = _Looper()
    assert b.run() is True                   # b claims the freed slot
    assert Campaign.active is b
    await a._task                            # a's teardown finally must NOT null b's slot
    assert Campaign.active is b
    await b.stop()


async def test_stop_is_idempotent_after_completion():
    c = _Looper()
    c.run()
    await c.stop()
    await c.stop()                       # already done — must not raise
    assert c.done
