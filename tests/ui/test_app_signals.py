import os
import signal
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from wifit3.ui.app import WifiteApp

if sys.platform == "win32":
    pytest.skip("POSIX signals only", allow_module_level=True)


@pytest.mark.usefixtures("no_usb_devices")  # ui/conftest.py
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP], ids=lambda s: s.name)
async def test_termination_signal_closes_cards_before_exit(sig):
    app = WifiteApp()
    array = MagicMock(close=AsyncMock())
    async with app.run_test() as pilot:
        app.array = array
        os.kill(os.getpid(), sig)
        await pilot.pause(0.1)
    array.close.assert_awaited_once()
    assert app.return_code == 0


@pytest.mark.usefixtures("no_usb_devices")
async def test_second_termination_signal_falls_back_to_default():
    app = WifiteApp()
    app.action_quit = AsyncMock()  # a quit wedged on a card: never exits
    async with app.run_test() as pilot:
        os.kill(os.getpid(), signal.SIGTERM)
        await pilot.pause(0.1)
        app.action_quit.assert_awaited_once()
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_DFL


@pytest.mark.usefixtures("no_usb_devices")
async def test_exit_restores_the_handlers_it_replaced():
    before = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)
    async with WifiteApp().run_test() as pilot:
        assert signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, before[0])
        await pilot.pause(0)
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)) == before
