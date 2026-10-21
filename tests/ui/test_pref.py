"""The Ctrl+P preferences modal (ui/pref.py): Save survives a failing Config.save(),
and Consolidate has moved out of Prefs into the Vault screen."""
import pytest
from textual.app import App
from textual.widgets import Button, Select

from wifit3.models import ToolName
from wifit3.persist import config
from wifit3.persist.config import Config, ConfigError
from wifit3.persist.vault import Vault
from wifit3.ui.pref import PreferencesModal
from wifit3.ui.vault.modals.cpu_crack import CpuCrackConfigModal
from wifit3.ui.vault.modals.hashcat import HashcatConfigModal


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


async def _open_prefs(app, pilot) -> None:
    app.push_screen(PreferencesModal())
    await pilot.pause(0)


@pytest.mark.asyncio
async def test_auto_crack_tool_is_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(Config.hashcat, "path", "/usr/bin/hashcat")
    monkeypatch.setattr(Config.hashcat, "wordlists", ["/lists/a.txt"])
    app = _Host()
    async with app.run_test() as pilot:
        await _open_prefs(app, pilot)
        app.screen.query_one("#auto_crack", Select).value = "hashcat"
        await pilot.pause(0)
        assert isinstance(app.screen, PreferencesModal), "a configured tool opens no modal"
        app.screen.query_one("#save", Button).press()
        await pilot.pause(0)

    assert Config.auto_crack is ToolName.HASHCAT
    assert "auto_crack = 'hashcat'" in (tmp_path / "config.toml").read_text("utf-8")


@pytest.mark.asyncio
async def test_auto_crack_off_hides_configure_and_saves_off(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_PATH", tmp_path / "config.toml")
    app = _Host()
    async with app.run_test() as pilot:
        await _open_prefs(app, pilot)
        configure = app.screen.query_one("#auto_crack_configure", Button)
        assert configure.display
        app.screen.query_one("#auto_crack", Select).value = ""
        await pilot.pause(0)
        assert not configure.display
        app.screen.query_one("#save", Button).press()
        await pilot.pause(0)

    assert Config.auto_crack is None


@pytest.mark.asyncio
async def test_choosing_an_unconfigured_tool_opens_its_modal_in_save_mode(monkeypatch):
    monkeypatch.setattr(Config.hashcat, "path", None)
    app = _Host()
    async with app.run_test() as pilot:
        await _open_prefs(app, pilot)
        app.screen.query_one("#auto_crack", Select).value = "hashcat"
        await pilot.pause()

        assert isinstance(app.screen, HashcatConfigModal)
        assert str(app.screen.query_one("#wordlist-launch", Button).label) == "Save"


@pytest.mark.asyncio
async def test_cancelling_an_unconfigured_tools_modal_restores_the_previous_choice(monkeypatch):
    monkeypatch.setattr(Config, "auto_crack", ToolName.CPU_CRACK)
    monkeypatch.setattr(Config.hashcat, "path", None)
    app = _Host()
    async with app.run_test() as pilot:
        await _open_prefs(app, pilot)
        app.screen.query_one("#auto_crack", Select).value = "hashcat"
        await pilot.pause()
        app.screen.query_one("#wordlist-cancel", Button).press()
        await pilot.pause()

        assert isinstance(app.screen, PreferencesModal)
        assert app.screen.query_one("#auto_crack", Select).value == "cpu_crack"
        app.screen.query_one("#save", Button).press()
        await pilot.pause()

    assert Config.auto_crack is ToolName.CPU_CRACK


@pytest.mark.asyncio
async def test_configure_button_opens_the_chosen_tools_modal():
    app = _Host()
    async with app.run_test() as pilot:
        await _open_prefs(app, pilot)
        app.screen.query_one("#auto_crack_configure", Button).press()
        await pilot.pause()

        assert isinstance(app.screen, CpuCrackConfigModal)
        assert str(app.screen.query_one("#wordlist-launch", Button).label) == "Save"
