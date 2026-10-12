"""The Ctrl+P preferences modal (ui/pref.py): Save survives a failing Config.save(),
and Consolidate has moved out of Prefs into the Vault screen."""
import pytest
from textual.app import App
from textual.widgets import Button, Checkbox, Input

from wifit3.persist.config import Config, ConfigError
from wifit3.persist.vault import Vault
from wifit3.ui.pref import HashtopolisSetting, PreferencesModal

_HT_ATTRS = ("hashtopolis_url", "hashtopolis_token", "hashtopolis_access_group_id",
             "hashtopolis_auto_submit", "hashtopolis_trusted_agents_only")


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
async def test_hashtopolis_section_persists_token_only_settings(monkeypatch):
    for n in _HT_ATTRS:
        monkeypatch.setattr(Config, n, getattr(Config, n))
    monkeypatch.setattr(Config, "save", staticmethod(lambda: None))
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(PreferencesModal())
        await pilot.pause(0)
        modal = app.screen
        modal.query_one("#ht_url", Input).value = "http://ht:8080"
        modal.query_one("#ht_token", Input).value = "tok"
        modal.query_one("#ht_auto_submit", Checkbox).value = True
        modal.query_one("#ht_trusted", Checkbox).value = False
        modal.query_one("#save", Button).press()
        await pilot.pause(0)
    assert Config.hashtopolis_url == "http://ht:8080"
    assert Config.hashtopolis_token == "tok"
    assert Config.hashtopolis_auto_submit is True
    assert Config.hashtopolis_trusted_agents_only is False


@pytest.mark.asyncio
async def test_hashtopolis_url_is_saved_sanitized(monkeypatch):
    for n in _HT_ATTRS:
        monkeypatch.setattr(Config, n, getattr(Config, n))
    monkeypatch.setattr(Config, "save", staticmethod(lambda: None))
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(PreferencesModal())
        await pilot.pause(0)
        modal = app.screen
        modal.query_one("#ht_url", Input).value = "http://user:secret@ht.local/?token=abc"
        modal.query_one("#ht_token", Input).value = "tok"
        modal.query_one("#save", Button).press()
        await pilot.pause(0)
    assert Config.hashtopolis_url == "http://ht.local"
    assert "secret" not in (Config.hashtopolis_url or "")


@pytest.mark.asyncio
async def test_hashtopolis_section_has_only_masked_token_auth():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(PreferencesModal())
        await pilot.pause(0)
        section = app.screen.query_one(HashtopolisSetting)
        assert section.query_one("#ht_token", Input).password is True
        assert len(section.query("#ht_auth_mode")) == 0
        assert len(section.query("#ht_username")) == 0
        assert len(section.query("#ht_password")) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_url", ["", "http://host:notaport", "http://[::1"])
async def test_hashtopolis_test_connection_bad_config_toasts(bad_url):
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(PreferencesModal())
        await pilot.pause(0)
        section = app.screen.query_one(HashtopolisSetting)
        toasts = []
        section.notify = lambda *a, **k: toasts.append((a, k))
        section.query_one("#ht_url", Input).value = bad_url
        section.query_one("#ht_token", Input).value = "tok"
        section.query_one("#ht_test", Button).press()
        await pilot.pause(0)
        assert toasts and toasts[0][1].get("severity") == "error"
