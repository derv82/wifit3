"""Tests for the blocking-error modals."""
import asyncio

import pytest
import usb.core

from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.error_modals import FatalErrorModal, RecoverableErrorModal


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
        await pilot.pause(0)
        await pilot.pause(0)
        assert isinstance(app.screen, RecoverableErrorModal)
        assert app.screen._error.title == "Adapter disconnected"


@pytest.mark.asyncio
async def test_no_usb_backend_shows_fatal_modal(monkeypatch):
    # The broken-udev Linux condition: find() resolves no backend and raises. (Deliberately does
    # NOT use no_usb_devices: that stubs find->[], the success path; here find must raise.)
    monkeypatch.setattr("usb.core.find", _raise_no_backend)

    app = WifiteApp()
    async with app.run_test() as pilot:
        await pilot.pause(0)   # on_mount -> poll_usb fires, should catch and push the modal
        await pilot.pause(0)
        assert isinstance(app.screen, FatalErrorModal)
        assert app.screen._error.title == "USB backend unavailable"
        assert "libusb" in app.screen._error.message
        assert app.screen._error.trace.strip()      # non-empty, pasteable


