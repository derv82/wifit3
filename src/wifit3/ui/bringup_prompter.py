"""BringupPrompter: the Textual implementation of ``setup.base.Prompter``, and the only object in the
bring-up path that touches Textual. It carries no decisions (the "humble object"): every method
pushes a screen or updates a label. BringupManager owns its lifecycle via open()/close()."""
from __future__ import annotations

from wifit3.ui.screens.bringup_progress import BringupProgressModal
from wifit3.ui.screens.replug import ReplugModal
from wifit3.ui.screens.setup_error import SetupErrorDialog
from wifit3.wlan.discovery import wait_for_presence


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

    async def wait_replug(self, device_id) -> bool:
        async def _present(present: bool) -> bool:
            return await wait_for_presence(device_id.vid, device_id.pid, present=present)
        outcome = await self._app.push_screen_wait(ReplugModal(device_id.description, _present))
        return outcome == "replugged"
