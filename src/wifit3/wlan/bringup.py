"""The bring-up engine: turn a discovered DeviceID into a live, pooled WlanInterface.

One coherent function (``run``): try connect first (a 1-shot; a probe would bork the card), and only
on a fixable access failure run the platform's ``Setup`` and retry. No ``sys.platform`` here and no
Textual: the OS lives behind ``Setup``, the UI behind a ``Prompter`` (the real one is
``ui.bringup_prompter.BringupPrompter``). On success the card is attached to the app's ``WlanArray``;
the caller only reads the ``BringupResult``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum, auto

from wifit3.errors import BringUpError, BringUpPermissionsError
from wifit3.setup.base import Setup, SetupResult
from wifit3.wlan.array import WlanArray
from wifit3.wlan.discovery import build_interface, find_devices

logger = logging.getLogger(__name__)


class Status(Enum):
    READY = auto()
    CANCELLED = auto()
    FAILED = auto()


@dataclass
class BringupResult:
    """The terminal outcome of a bring-up. The live interface lives in the WlanArray, not here."""
    status: Status
    message: str = ""

    @classmethod
    def ready(cls) -> "BringupResult":
        return cls(Status.READY)

    @classmethod
    def cancelled(cls, message: str = "") -> "BringupResult":
        return cls(Status.CANCELLED, message)

    @classmethod
    def failed(cls, message: str) -> "BringupResult":
        return cls(Status.FAILED, message)


class BringupManager:
    """Owns the bring-up + setup flow. One per app; call ``run(device_id)`` from any screen."""

    def __init__(self, app, *, setup: Setup | None = None, prompter=None) -> None:
        self.app = app
        self.setup = setup or Setup.for_platform()
        if prompter is None:
            from wifit3.ui.bringup_prompter import BringupPrompter
            prompter = BringupPrompter(app)
        self.prompter = prompter
        self._name_counter = 0

    async def run(self, device_id) -> BringupResult:
        """Bring ``device_id`` up (installing setup if the card can't be opened yet) and pool it.
        Shows the progress modal for the whole flow."""
        await self.prompter.open(f"Bringing up {device_id.description}…")
        try:
            return await self._run(device_id)
        finally:
            self.prompter.close()

    async def _run(self, device_id) -> BringupResult:
        try:
            await self._connect_and_pool(device_id)
            return BringupResult.ready()
        except BringUpPermissionsError:
            pass                                          # fixable: fall through to setup
        except BringUpError as e:
            return BringupResult.failed(self._fault_message(device_id, e))

        if not await self.setup.install(device_id, self.prompter):
            return BringupResult.cancelled()              # declined or failed (setup already reported)

        try:
            await self._connect_and_pool(device_id)
            return BringupResult.ready()
        except BringUpError as e:
            return BringupResult.failed(self._fault_message(device_id, e))

    async def uninstall(self, device_id) -> SetupResult:
        """Reverse a prior setup for ``device_id`` (the splash's ✕ button)."""
        return await self.setup.uninstall(device_id, self.prompter)

    async def _connect_and_pool(self, device_id) -> None:
        iface = build_interface(device_id, name=self._next_name())
        if iface is None:
            raise BringUpError("discover", "card not present")
        await iface.connect(progress_cb=self.prompter.status_progress)
        self._ensure_array().attach(iface)
        await self._pool_ready_others(exclude=device_id)

    def _ensure_array(self) -> WlanArray:
        if self.app.array is None:
            array = WlanArray()
            array.register_disconnect_callback(self.app.notify_device_lost)
            self.app.array = array
        return self.app.array

    async def _pool_ready_others(self, *, exclude) -> None:
        """Best-effort: bring up every other present, already-openable supported card into the pool
        so a multi-card session starts merged. Sequential (two cards driving RF over USB at once can
        collide). A card needing setup raises and is skipped; it can be started on a later pass. Two
        identical cards (same VID:PID) pool only one for now: DeviceID can't tell them apart."""
        array = self.app.array
        pooled = {(m.vid, m.pid) for m in array.members}
        for other in find_devices():
            key = (other.vid, other.pid)
            if key == (exclude.vid, exclude.pid) or key in pooled:
                continue
            iface = build_interface(other, name=self._next_name())
            if iface is None:
                continue
            try:
                if await iface.connect():
                    array.attach(iface)
                    pooled.add(key)
                    self.app.notify(f"{other.description} joined the pool", timeout=4)
            except Exception:
                logger.info("secondary card %s failed to join", other.description, exc_info=True)

    def _next_name(self) -> str:
        name = f"wlan{self._name_counter}"
        self._name_counter += 1
        return name

    @staticmethod
    def _fault_message(device_id, e: BringUpError) -> str:
        chip = device_id.description.split(" (")[0]
        return f"{chip}: {e.stage} failed" + (f": {e.detail}" if e.detail else "")
