"""BringupProgressModal is a passive surface: set_status / set_progress update its widgets."""
from textual.app import App
from textual.widgets import Label, ProgressBar

from wifit3.ui.screens.bringup_progress import BringupProgressModal


class _Host(App):
    def on_mount(self) -> None:
        self.modal = BringupProgressModal("Bringing up RT5372")
        self.push_screen(self.modal)


async def test_status_and_progress_update():
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        app.modal.set_status("Installing WinUSB…")
        app.modal.set_progress(0.5)
        await pilot.pause()
        assert str(app.modal.query_one("#status", Label).render()) == "Installing WinUSB…"
        assert app.modal.query_one(ProgressBar).progress == 50
