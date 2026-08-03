"""The mid-session new-device prompt (ui/screens/new_device.py): shows the card, dismisses a bool."""
import pytest
from textual.app import App

from wifit3.ui.screens.new_device import NewDeviceDialog


class _Host(App):
    """A bare app to host the modal (no USB, no splash)."""


async def _prompt(pilot, app):
    result = {}
    app.push_screen(NewDeviceDialog("RTL8812AU (Alfa AWUS036ACH)"),
                    lambda value: result.__setitem__("value", value))
    await pilot.pause(0)
    return result


@pytest.mark.asyncio
async def test_shows_description_and_prompt():
    app = _Host()
    async with app.run_test() as pilot:
        await _prompt(pilot, app)
        modal = app.screen
        assert isinstance(modal, NewDeviceDialog)
        assert "RTL8812AU (Alfa AWUS036ACH)" in str(modal.query_one("#desc").render())
        assert "A new wireless device was detected" in str(modal.query_one("#title").render())


@pytest.mark.asyncio
async def test_yes_dismisses_true():
    app = _Host()
    async with app.run_test() as pilot:
        result = await _prompt(pilot, app)
        await pilot.click("#btn-yes")
        await pilot.pause(0)
        assert result["value"] is True


@pytest.mark.asyncio
async def test_no_dismisses_false():
    app = _Host()
    async with app.run_test() as pilot:
        result = await _prompt(pilot, app)
        await pilot.click("#btn-no")
        await pilot.pause(0)
        assert result["value"] is False


@pytest.mark.asyncio
async def test_escape_declines():
    app = _Host()
    async with app.run_test() as pilot:
        result = await _prompt(pilot, app)
        await pilot.press("escape")
        await pilot.pause(0)
        assert result["value"] is False
