"""No USB backend -> the Quit-only fatal modal, not Textual's traceback crash."""
import asyncio
from types import SimpleNamespace

import pytest
import usb.core
from textual.widgets import Collapsible

from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.fatal_error import FatalErrorModal


def _raise_no_backend(*args, **kwargs):
    raise usb.core.NoBackendError("No backend available")


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_device_lost_from_offloop_context_shows_modal():
    """The RX reader hands the unplug back via loop.call_soon_threadsafe — a bare loop
    callback that runs OUTSIDE Textual's message-pump context. Reproduce that exact hop and
    assert the fatal modal appears (a direct push_screen there crashes with NoActiveAppError;
    notify_device_lost must defer onto the message queue instead)."""
    app = WifiteApp()
    async with app.run_test() as pilot:
        app.active_interface = SimpleNamespace(name="Test Card")
        loop = asyncio.get_running_loop()
        # Exactly how the reader dispatches: a threadsafe hop, not a direct call.
        loop.call_soon_threadsafe(app.notify_device_lost, usb.core.USBError("gone", errno=19))
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, FatalErrorModal)
        assert app.screen._error.title == "Adapter disconnected"
        assert "Test Card" in app.screen._error.message


@pytest.mark.asyncio
async def test_no_usb_backend_shows_fatal_modal(monkeypatch):
    # The broken-udev Linux condition: find() resolves no backend and raises. (Deliberately does
    # NOT use no_usb_devices — that stubs find->[], the success path; here find must raise.)
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
    # the buttons off-screen (Collapsible / VerticalScroll default to *fill* — this guards the
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
