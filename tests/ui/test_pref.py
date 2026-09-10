"""The Ctrl+P preferences modal (ui/pref.py): Save survives a failing Config.save()."""
import pytest
from textual.app import App

from textual.widgets import Button, Label

from wifit3.persist.config import Config, ConfigError
from wifit3.ui.pref import ConsolidateModal, PreferencesModal


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


@pytest.mark.asyncio
async def test_consolidate_button_hidden_when_no_legacy_files(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "captures_dir", str(tmp_path))
    monkeypatch.setattr(Config, "save", lambda: None)
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(PreferencesModal())
        await pilot.pause(0)
        assert len(app.screen.query("#consolidate")) == 0


@pytest.mark.asyncio
async def test_consolidate_button_shown_when_legacy_files_exist(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "captures_dir", str(tmp_path))
    monkeypatch.setattr(Config, "save", lambda: None)
    (tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1700000001_handshake.hc22000").write_text("WPA*02*...\n")

    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(PreferencesModal())
        await pilot.pause(0)
        btn = app.screen.query_one("#consolidate", Button)
        assert btn is not None
        lbl = app.screen.query_one("#legacy_label", Label)
        assert "1 legacy split file(s) found" in str(lbl.render())


@pytest.mark.asyncio
async def test_click_consolidate_confirms_and_merges(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "captures_dir", str(tmp_path))
    monkeypatch.setattr(Config, "save", lambda: None)

    legacy_file = tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1700000001_handshake.hc22000"
    legacy_file.write_text("WPA*02*01*aabbccddeeff*112233445566*5465737431*11111111111111111111111111111111**\n")

    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(PreferencesModal())
        await pilot.pause(0)

        await pilot.click("#consolidate")
        await pilot.pause(0)

        assert isinstance(app.screen, ConsolidateModal)

        await pilot.click("#confirm")
        await pilot.pause(0)

        assert not legacy_file.exists()
        assert (tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff.hc22000").exists()
        assert isinstance(app.screen, PreferencesModal)
        assert len(app.screen.query("#legacy_done")) == 1


@pytest.mark.asyncio
async def test_click_consolidate_cancels(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "captures_dir", str(tmp_path))
    monkeypatch.setattr(Config, "save", lambda: None)

    legacy_file = tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1700000001_handshake.hc22000"
    legacy_file.write_text("WPA*02*01*aabbccddeeff*112233445566*5465737431*11111111111111111111111111111111**\n")

    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(PreferencesModal())
        await pilot.pause(0)

        await pilot.click("#consolidate")
        await pilot.pause(0)

        assert isinstance(app.screen, ConsolidateModal)

        await pilot.click("#cancel")
        await pilot.pause(0)

        assert legacy_file.exists()
        assert not (tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff.hc22000").exists()
        assert isinstance(app.screen, PreferencesModal)
        assert len(app.screen.query("#consolidate")) == 1

