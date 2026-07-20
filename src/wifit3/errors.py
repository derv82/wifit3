"""Error types wifit3 surfaces to the user (in the TUI), not via bare exceptions or logs."""
from __future__ import annotations

import os
import re
import traceback

import usb.core

# LIBUSB_ERROR_NO_DEVICE (-4) is the unambiguous "adapter was unplugged" code; the libusb1
# backend maps it to errno ENODEV (19). On Windows+WinUSB it's the *first* error an unplug
# raises, after which the pipe streams LIBUSB_ERROR_IO (errno 5), too broad to key on, so
# we match only NO_DEVICE here and let the RX reader's consecutive-error give-up absorb the
# messier IO streak. (backend_error_code is already read this way in mt76x0u/driver.py.)
_LIBUSB_NO_DEVICE = -4


def is_device_gone(exc: BaseException) -> bool:
    """True if ``exc`` is a USBError that means the adapter was physically removed."""
    if not isinstance(exc, usb.core.USBError):
        return False
    return (getattr(exc, "backend_error_code", None) == _LIBUSB_NO_DEVICE
            or getattr(exc, "errno", None) == 19)


def _scrub_paths(text: str) -> str:
    """Strip user-identifying absolute paths out of a formatted traceback."""
    text = re.sub(r'(File ")[^"]*?(wifit3[\\/])', r"\1\2", text)
    home = os.path.expanduser("~")
    if home and len(home) > 3 and home != "~":
        text = text.replace(home, "~")
    return text


class BringUpError(Exception):
    """A recoverable failure while bringing up one card's driver (claim, firmware, init, …)."""

    def __init__(self, stage: str, detail: str = "") -> None:
        self.stage = stage
        self.detail = detail
        super().__init__(f"{stage}: {detail}" if detail else stage)


class BringUpPermissionsError(BringUpError):
    """A bring-up failure caused by missing device-access permissions, recoverable via the
    one-time install."""


class WifiteFatalError(Exception):
    """An unrecoverable condition the user must fix before wifit3 can run (e.g. no USB backend)."""

    def __init__(self, title: str, message: str) -> None:
        self.title = title
        self.message = message
        super().__init__(f"{title}: {message}")

    @property
    def trace(self) -> str:
        raw = "".join(traceback.format_exception(type(self), self, self.__traceback__))
        return _scrub_paths(raw)


class WifiteDeviceLostError(Exception):
    """The active adapter vanished mid-run (unplug / USB pipe wedged past recovery)."""

    def __init__(self, name: str = "the wireless adapter") -> None:
        self.title = "Adapter disconnected"
        self.message = (
            f"Lost contact with {name}: it was unplugged or the USB link dropped.\n"
            "Replug the card, then press Back to Splash to reconnect."
        )
        super().__init__(f"{self.title}: {self.message}")
