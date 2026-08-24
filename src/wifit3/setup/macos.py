"""macOS device setup: no privileged step — but the cards split in two.

macOS ships no driver for the RTL/MT/RT/AR chips wifit3 supports, so nothing binds those cards
and libusb can claim them directly: no Zadig/WinUSB binding (Windows), no udev rule + modprobe
blacklist (Linux). ``install`` therefore performs no privileged action — it just hands the card
back for one retry of the connect, so a transient first-attempt failure (a card just plugged in,
a previous process still holding the handle) recovers on its own, and a genuine open failure
surfaces through the normal bring-up fault message instead of a silent cancel.

Broadcom USB silicon is the exception: the macOS kernel ships a built-in driver that claims it,
and libusb cannot detach kernel drivers on Darwin (Apple kexts are SIP-protected too), so there
is no userland unbind. A driver that declares ``MACOS_KERNEL_BINDS_CHIP`` gets tailored
messaging: install explains that the kernel may hold the card before the retry, uninstall
explains that nothing can be removed from userland.
"""
from __future__ import annotations

from wifit3.chips.driver import DeviceID
from wifit3.device.manager import driver_for
from wifit3.setup.base import Prompter, Setup, SetupResult

_NOOP_MSG = "macOS needs no driver setup — retrying the connection…"
_NOOP_UNINSTALL_MSG = ("Nothing is installed on macOS: no driver binding or access rules "
                       "to remove.")
_KERNEL_BOUND_MSG = ("This card's chipset is bound by a built-in macOS driver, which libusb "
                     "cannot detach. Retrying the connection — if the card fails to open, "
                     "macOS must release it before wifit3 can drive it.")
_KERNEL_BOUND_UNINSTALL_MSG = ("macOS has no install to reverse: the card's built-in driver is "
                               "an Apple kext, which libusb cannot detach and SIP protects from "
                               "userland unload.")


class SetupMacOS(Setup):
    """macOS device setup: a no-op install (nothing to bind or unblock) that returns the card
    for one connect retry, and an uninstall that reports there is nothing to remove. Chips the
    macOS kernel binds itself (Broadcom USB silicon) get tailored messaging instead."""

    @staticmethod
    def _kernel_binds(device_id: DeviceID) -> bool:
        """Whether the registered driver for ``device_id`` declares that the macOS kernel claims
        it (see :attr:`wifit3.chips.driver.Driver.MACOS_KERNEL_BINDS_CHIP`)."""
        got = driver_for(device_id.vid, device_id.pid)
        return bool(got and getattr(got[0], "MACOS_KERNEL_BINDS_CHIP", False))

    async def install(self, device_id: DeviceID, ui: Prompter) -> DeviceID | None:
        ui.status(_KERNEL_BOUND_MSG if self._kernel_binds(device_id) else _NOOP_MSG)
        return device_id

    async def uninstall(self, device_id: DeviceID, ui: Prompter) -> SetupResult:
        message = (_KERNEL_BOUND_UNINSTALL_MSG if self._kernel_binds(device_id)
                   else _NOOP_UNINSTALL_MSG)
        return SetupResult(ok=True, message=message)