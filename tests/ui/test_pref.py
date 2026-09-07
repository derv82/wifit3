"""The Ctrl+P preferences modal (ui/pref.py): Save survives a failing Config.save()."""
import pytest
from textual.app import App

from wifit3.persist.config import Config, ConfigError
from wifit3.ui.pref import PreferencesModal


class _Host(App):
    """A bare app to host the modal (no USB, no splash)."""


def _raise_config_error() -> None:
    raise ConfigError("disk full")


@pytest.mark.asyncio
async def test_save_notifies_instead_of_crashing_on_config_error(monkeypatch):
    monkeypatch.setattr(Config, "save", staticmethod(_raise_config_error))
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(PreferencesModal())
        await pilot.pause(0)
        modal = app.screen
        toasts = []
        monkeypatch.setattr(modal, "notify", lambda *a, **k: toasts.append((a, k)))

        await pilot.click("#save")   # would propagate ConfigError if save_pressed didn't catch it
        await pilot.pause(0)

        assert toasts, "a failed save should surface a toast"
        assert toasts[0][1].get("title") == "Config Error"
        assert not isinstance(app.screen, PreferencesModal)   # dismissed anyway
