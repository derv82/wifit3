"""ConfirmUninstallDialog return-value contract.

Windows / no-sibling Linux → a single Uninstall button dismissing "narrow". When a Linux card
shares its kernel module with sibling chipsets, the dialog offers the wide radius too, and a card
with no rules of its own (blocked only by a sibling) offers *only* the wide removal.
"""
from textual.app import App
from textual.widgets import Button

from wifit3.ui.screens.confirm_uninstall import ConfirmUninstallDialog


class _Host(App):
    """Push the dialog and capture whatever it dismisses with."""
    def __init__(self, dialog):
        super().__init__()
        self._dialog = dialog
        self.result = "UNSET"

    def on_mount(self) -> None:
        self.push_screen(self._dialog, callback=lambda r: setattr(self, "result", r))


async def test_windows_single_uninstall_returns_narrow():
    app = _Host(ConfirmUninstallDialog("RT5372", "win"))
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        assert len(app.screen.query("#btn-wide")) == 0      # no wide radius without siblings
        await pilot.click("#btn-narrow")
        await pilot.pause()
    assert app.result == "narrow"


async def test_linux_siblings_offer_narrow_and_wide():
    app = _Host(ConfirmUninstallDialog(
        "RT5372", "linux", siblings=["Ralink RT5572 (PAU09)"], has_own_files=True))
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen.query_one("#btn-narrow", Button)
        assert screen.query_one("#btn-wide", Button)
        await pilot.click("#btn-wide")
        await pilot.pause()
    assert app.result == "wide"


async def test_linux_narrow_button_returns_narrow():
    app = _Host(ConfirmUninstallDialog(
        "RT5372", "linux", siblings=["Ralink RT5572 (PAU09)"], has_own_files=True))
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#btn-narrow")
        await pilot.pause()
    assert app.result == "narrow"


async def test_linux_no_own_files_offers_only_wide():
    app = _Host(ConfirmUninstallDialog(
        "RT5572", "linux", siblings=["Ralink RT5372 (Panda)"], has_own_files=False))
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert len(screen.query("#btn-narrow")) == 0        # nothing of its own to remove
        assert screen.query_one("#btn-wide", Button)
        await pilot.click("#btn-wide")
        await pilot.pause()
    assert app.result == "wide"


async def test_cancel_returns_none():
    app = _Host(ConfirmUninstallDialog(
        "RT5372", "linux", siblings=["Ralink RT5572 (PAU09)"], has_own_files=True))
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#btn-cancel")
        await pilot.pause()
    assert app.result is None
