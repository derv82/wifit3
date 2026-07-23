"""The device-setup contract: one privileged, OS-specific strategy behind a small ABC.

Bringing a card to a driveable state can need a privileged step the kernel/OS won't do for us:
WinUSB binding on Windows, a udev rule + modprobe blacklist on Linux. ``Setup`` is that step.
``SetupWindows`` / ``SetupLinux`` implement it; the bring-up engine calls :meth:`Setup.for_platform`
once and never branches on the OS again. Every user interaction (confirm, replug, progress, error)
goes through the injected :class:`Prompter`, so a ``Setup`` is testable with no Textual app and no
hardware.
"""
from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol

from wifit3.chips.driver import DeviceID


@dataclass(frozen=True)
class SetupResult:
    """Outcome of a privileged setup/teardown action, for the caller to report to the user."""
    ok: bool
    message: str
    cancelled: bool = False
    detail: str | None = None


class Prompter(Protocol):
    """What a ``Setup`` needs from the UI: show a modal and wait for its answer, drive a replug,
    stream a status line, report an error. The only object that touches Textual; each implementation
    is a dumb adapter with no decisions of its own (see ``ui.bringup_prompter.BringupPrompter``)."""

    async def ask(self, dialog):
        """Show ``dialog`` (a ModalScreen) and return whatever it dismisses with."""
        ...

    async def wait_replug(self, device_id: DeviceID) -> bool: ...
    def status(self, message: str) -> None: ...
    def error(self, title: str, body: str) -> None: ...


class Setup(ABC):
    """The privileged, OS-specific step that makes a card openable from userland."""

    @staticmethod
    def for_platform() -> "Setup":
        """The Setup for the current OS: WinUSB on Windows, udev+modprobe on Linux, a no-op else.
        The only place setup dispatches on ``sys.platform``."""
        if sys.platform == "win32":
            from wifit3.setup.windows import SetupWindows
            return SetupWindows()
        if sys.platform.startswith("linux"):
            from wifit3.setup.linux import SetupLinux
            return SetupLinux()
        return NoSetup()

    @abstractmethod
    async def install(self, device_id: DeviceID, ui: Prompter) -> bool:
        """Make ``device_id`` openable: show the confirm dialog, elevate, and on Linux drive the
        replug. Returns True when the caller should retry connect, False on decline or failure (the
        Prompter has already shown any error)."""
        ...

    @abstractmethod
    async def uninstall(self, device_id: DeviceID, ui: Prompter) -> SetupResult:
        """Reverse a prior install (the splash's ✕ button). Returns a result for the caller to report."""
        ...


class NoSetup(Setup):
    """No privileged setup exists on this OS: nothing to install, nothing to reverse."""

    async def install(self, device_id: DeviceID, ui: Prompter) -> bool:
        return False

    async def uninstall(self, device_id: DeviceID, ui: Prompter) -> SetupResult:
        return SetupResult(ok=True, message="No device setup is needed on this platform.")
