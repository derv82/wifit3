"""Tests for the blocking-error modals."""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import usb.core
from textual.widgets import Button, Collapsible, Label, ListItem, ListView

from wifit3.errors import WifiteDeviceLostError
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.error_modals import FatalErrorModal, RecoverableErrorModal
from wifit3.ui.screens.splash import SplashView


def _raise_no_backend(*args, **kwargs):
    raise usb.core.NoBackendError("No backend available")


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_device_lost_from_offloop_context_shows_recoverable_modal():
    """The RX reader hands an unplug back via loop.call_soon_threadsafe, outside Textual's
    message-pump context. Assert that hop surfaces the recoverable modal (a direct push_screen
    there raises NoActiveAppError, so notify_device_lost must defer onto the message queue)."""
    app = WifiteApp()
    async with app.run_test() as pilot:
        loop = asyncio.get_running_loop()
        # The last card's loss re-emits (exc, remaining=0) via the threadsafe hop.
        loop.call_soon_threadsafe(app.notify_device_lost, usb.core.USBError("gone", errno=19), 0)
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, RecoverableErrorModal)
        assert app.screen._error.title == "Adapter disconnected"


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_back_to_splash_tears_down_interface_and_returns():
    """Back to Splash closes + clears the dead interface and lands on the splash screen."""
    app = WifiteApp()
    async with app.run_test() as pilot:
        closed = {"count": 0}

        async def _close():
            closed["count"] += 1

        app.array = SimpleNamespace(close=_close)
        app.target_ap = object()
        app.push_screen(RecoverableErrorModal(WifiteDeviceLostError("Test Card")))
        await pilot.pause()

        await app.recover_to_splash()
        await pilot.pause()   # let the re-entered splash settle

        assert closed["count"] == 1               # dead pool torn down exactly once
        assert app.array is None
        assert app.target_ap is None
        assert isinstance(app.screen, SplashView)


@pytest.mark.asyncio
async def test_no_usb_backend_shows_fatal_modal(monkeypatch):
    # The broken-udev Linux condition: find() resolves no backend and raises. (Deliberately does
    # NOT use no_usb_devices: that stubs find->[], the success path; here find must raise.)
    monkeypatch.setattr("usb.core.find", _raise_no_backend)

    app = WifiteApp()
    async with app.run_test() as pilot:
        await pilot.pause()   # on_mount -> poll_usb fires, should catch and push the modal
        await pilot.pause()
        assert isinstance(app.screen, FatalErrorModal)
        assert app.screen._error.title == "USB backend unavailable"
        assert "libusb" in app.screen._error.message
        assert app.screen._error.trace.strip()      # non-empty, pasteable


@pytest.mark.asyncio
async def test_fatal_modal_compact_with_details_expanded(monkeypatch):
    # Expanding Details must scroll the trace inside a capped box, not balloon the dialog and shove
    # the buttons off-screen (Collapsible / VerticalScroll default to *fill*: this guards the
    # height fix). At a normal 90x40 terminal the buttons must stay on-screen.
    monkeypatch.setattr("usb.core.find", _raise_no_backend)
    app = WifiteApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, FatalErrorModal)
        modal.query_one(Collapsible).collapsed = False     # expand Details
        await pilot.pause()
        assert modal.query_one("#trace-scroll").size.height <= 10          # trace scrolls, capped
        assert modal.query_one("#button-row").region.bottom <= app.size.height   # buttons on-screen


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_reset_for_reentry_clears_a_frozen_splash():
    """A happy-path connect leaves splash frozen (initializing latched, timer paused, START
    disabled, a stale card listed, progress shown) because it navigates to the scanner without
    cleanup. reset_for_reentry restores the scanning state and resumes the poll timer: the
    installed screen only resumes on return, so on_mount can't."""
    app = WifiteApp()
    async with app.run_test() as pilot:
        splash = app.get_screen("splash", SplashView)
        device_list = splash.query_one("#device-list", ListView)

        # Freeze splash the way a bring-up leaves it (initializing latched, timer paused, START
        # disabled, a stale card listed) before it navigated to the scanner.
        splash._is_initializing = True
        await device_list.append(ListItem(Label("Stale Card"), name="0"))
        device_list.disabled = True
        splash.query_one("#start-btn", Button).disabled = True
        splash._refresh_timer.pause()          # stop the real poll racing the assertions
        splash._refresh_timer = MagicMock()    # spy resume()
        await pilot.pause()

        splash.reset_for_reentry()
        await pilot.pause()

        assert splash._is_initializing is False
        assert len(device_list.children) == 0
        assert device_list.disabled is False
        assert splash.query_one("#start-btn", Button).disabled is True
        splash._refresh_timer.resume.assert_called_once()
