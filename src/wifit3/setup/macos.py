"""macOS device setup: no privileged step. macOS ships no driver for wifit3's RTL/MT/RT/AR chips,
so libusb claims them directly. install returns the device for one connect retry; uninstall
reports there is nothing to remove.
"""
from __future__ import annotations

from wifit3.chips.driver import DeviceID
from wifit3.setup.base import Prompter, Setup, SetupResult

_NOOP_MSG = "macOS needs no driver setup. Retrying the connection…"
_UNINSTALL_MSG = "Nothing is installed on macOS: no driver binding or access rules to remove."


class SetupMacOS(Setup):
    """No-op macOS setup: install returns the device for one connect retry, uninstall reports
    there is nothing to remove."""

    async def install(self, device_id: DeviceID, ui: Prompter) -> DeviceID | None:
        ui.status(_NOOP_MSG)
        return device_id

    async def uninstall(self, device_id: DeviceID, ui: Prompter) -> SetupResult:
        return SetupResult(ok=True, message=_UNINSTALL_MSG)
