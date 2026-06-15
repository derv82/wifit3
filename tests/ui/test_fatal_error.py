"""No USB backend -> the Quit-only fatal modal, not Textual's traceback crash."""
import pytest
import usb.core

from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.fatal_error import FatalErrorModal


def _raise_no_backend(*args, **kwargs):
    raise usb.core.NoBackendError("No backend available")


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
