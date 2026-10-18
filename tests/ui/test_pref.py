"""The Ctrl+P preferences modal (ui/pref.py): Save survives a failing Config.save(),
and Consolidate has moved out of Prefs into the Vault screen."""
import pytest
from textual.app import App
from textual.widgets import Button, Checkbox

from wifit3.persist import config
from wifit3.persist.config import Config, ConfigError
from wifit3.persist.vault import Vault
from wifit3.ui.pref import PreferencesModal
from wifit3.ui.screens.vault_item import ConfirmModal


class _Host(App):
    """A bare app to host the modal (no USB, no splash)."""
    def __init__(self):
        super().__init__()
        self.vault = Vault()


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

        app.screen.query_one("#save", Button).press()   # would propagate ConfigError if uncaught
        await pilot.pause(0)

        assert toasts, "a failed save should surface a toast"
        assert toasts[0][1].get("title") == "Config Error"
        assert not isinstance(app.screen, PreferencesModal)   # dismissed anyway


@pytest.mark.asyncio
async def test_prefs_no_longer_hosts_consolidate(tmp_path):
    """Consolidate moved to the Vault screen; Prefs must not show it, even with legacy files."""
    (tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1700000001_handshake.hc22000").write_text("WPA*02*...\n")
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(PreferencesModal())
        await pilot.pause(0)
        assert len(app.screen.query("#consolidate")) == 0


@pytest.mark.asyncio
async def test_preferences_offers_v1_conversion_when_metadata_is_missing(tmp_path):
    (tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1700000000_wpa_psk.txt").write_text(
        "SSID: HomeNet\nBSSID: aa:bb:cc:dd:ee:ff\nPSK: password1\n",
        encoding="utf-8",
    )
    app = _Host()
    async with app.run_test() as pilot:
        modal = PreferencesModal()
        notices = []
        modal.notify = lambda *args, **kwargs: notices.append((args, kwargs))
        app.push_screen(modal)
        await pilot.pause()

        assert app.screen is modal
        assert "1 saved AP is missing v2 metadata" in notices[0][0][0]


@pytest.mark.asyncio
async def test_vault_repair_requires_destructive_change_confirmation(tmp_path):
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(PreferencesModal())
        await pilot.pause(0)

        app.screen.query_one("#repair-vault", Button).press()
        await pilot.pause()

        assert isinstance(app.screen, ConfirmModal)
        prompt = str(app.screen.query_one("#prompt").render())
        assert "Original HC22000 and pcap sources will be preserved" in prompt
        assert "Saved passwords" in prompt


@pytest.mark.asyncio
async def test_auto_crack_checkbox_is_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(Config, "auto_crack", True)
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(PreferencesModal())
        await pilot.pause(0)
        app.screen.query_one("#auto_crack", Checkbox).value = False
        app.screen.query_one("#save", Button).press()
        await pilot.pause(0)

    assert Config.auto_crack is False
    assert "auto_crack = false" in (tmp_path / "config.toml").read_text("utf-8")
