"""BringupPrompter: the Textual implementation of ``setup.base.Prompter``, and the only object in the
bring-up path that touches Textual. It carries no decisions (the "humble object"): every method
pushes a screen or updates a label. BringupManager owns its lifecycle via open()/close()."""
from __future__ import annotations

from typing import TYPE_CHECKING

from wifit3.ui.screens.bringup_progress import BringupProgressModal
from wifit3.ui.screens.replug import ReplugModal
from wifit3.ui.screens.setup_error import SetupErrorDialog
from wifit3.wlan.discovery import wait_for_arrival, wait_for_departure

if TYPE_CHECKING:
    from wifit3.chips.driver import DeviceID


class BringupPrompter:
    def __init__(self, app) -> None:
        self._app = app
        self._modal: BringupProgressModal | None = None

    async def open(self, title: str) -> None:
        """Show the unified progress modal for the whole bring-up; close() dismisses it."""
        self._modal = BringupProgressModal(title)
        await self._app.push_screen(self._modal)

    def close(self) -> None:
        if self._modal is not None:
            self._modal.dismiss()
            self._modal = None

    def status(self, message: str) -> None:
        if self._modal is not None:
            self._modal.set_status(message)

    def status_progress(self, fraction: float, message: str) -> None:
        """connect()'s progress_cb: (fraction 0..1, message)."""
        if self._modal is not None:
            self._modal.set_progress(fraction)
            self._modal.set_status(message)

    def error(self, title: str, body: str) -> None:
        # Take down the progress modal first so the error dialog isn't buried under it.
        self.close()
        self._app.push_screen(SetupErrorDialog(title, body))

    async def ask(self, dialog):
        return await self._app.push_screen_wait(dialog)

    async def wait_replug(self, device_id) -> DeviceID | None:
        """The re-enumerated card after the unplug/replug (new bus/address), or None if skipped."""
        replugged = None

        async def _present(present: bool) -> bool:
            nonlocal replugged
            if not present:                               # phase 1: this card leaves the bus
                return await wait_for_departure(device_id)
            replugged = await wait_for_arrival(device_id)  # phase 2: a fresh instance returns
            return replugged is not None

        outcome = await self._app.push_screen_wait(ReplugModal(device_id.description, _present))
        return replugged if outcome == "replugged" else None

    def begin_assistant(self, greeting: tuple[str, ...], messages: list[tuple[str, ...]],
                        *, intro_delay: float = 2.0) -> None:
        if self._modal is not None:
            self._app.run_worker(
                self._modal.show_assistant(greeting, messages, intro_delay=intro_delay),
                name="wiffy-show")

    async def end_assistant(self, ok: bool) -> None:
        if self._modal is not None:
            await self._modal.hide_assistant(ok)
