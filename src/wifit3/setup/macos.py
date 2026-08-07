"""macOS device setup: there is none — the card is openable as-is.

macOS ships no driver for any of the supported vendor-class USB Wi-Fi chips, so nothing ever
binds the card and libusb can claim it directly: no Zadig/WinUSB binding (Windows), no udev
rule + modprobe blacklist (Linux). ``install`` therefore performs no privileged action — it
just hands the card back for one retry of the connect, so a transient first-attempt failure
(a card just plugged in, a previous process still holding the handle) recovers on its own,
and a genuine open failure surfaces through the normal bring-up fault message instead of a
silent cancel.
"""
from __future__ import annotations

from wifit3.chips.driver import DeviceID
from wifit3.setup.base import Prompter, Setup, SetupResult


class SetupMacOS(Setup):
    """macOS device setup: a no-op install (nothing to bind or unblock) that returns the card
    for one connect retry, and an uninstall that reports there is nothing to remove."""

    async def install(self, device_id: DeviceID, ui: Prompter) -> DeviceID | None:
        ui.status("macOS needs no driver setup — retrying the connection…")
        return device_id

    async def uninstall(self, device_id: DeviceID, ui: Prompter) -> SetupResult:
        return SetupResult(ok=True, message="Nothing is installed on macOS: no driver binding "
                                            "or access rules to remove.")
