import pytest

from wifit3.persist.config import Config
from wifit3.updates import UpdateInfo
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.splash import SplashView
from wifit3.ui.screens.scanner import ScannerView
from textual.widgets import RichLog, DataTable, Label



@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")  # ui/conftest.py
async def test_app_layout_and_boot():
    """Verify the app boots and registers the required screens."""
    app = WifiteApp()
    async with app.run_test() as pilot:
        # Check Title
        assert pilot.app.title.startswith("wifit3")
        
        # Verify we start on the Splash screen
        assert isinstance(pilot.app.screen, SplashView)
        
        # Check Splash Screen Components
        ascii_art = pilot.app.screen.query_one("#ascii-art")
        assert ascii_art is not None
        device_list = pilot.app.screen.query_one("#device-list")
        assert device_list is not None
        
        # Manually transition to Scanner View
        pilot.app.push_screen("scanner")
        await pilot.pause(0)
        
        assert isinstance(pilot.app.screen, ScannerView)
        
        # Check Scanner Screen Components
        table = pilot.app.screen.query_one("#ap-table", DataTable)
        assert table is not None
        log = pilot.app.screen.query_one("#system-log", RichLog)
        assert log is not None
        
        # Check that FocusViewV2 is registered (but requires target_ap to mount properly without escaping immediately, so we won't push it here)
        assert "focus" in pilot.app._installed_screens


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_app_exposes_check_updates_palette_command(monkeypatch):
    app = WifiteApp()
    calls = []
    monkeypatch.setattr(app, "action_check_for_updates", lambda: calls.append(True))

    async with app.run_test() as pilot:
        commands = {command.title: command for command in app.get_system_commands(pilot.app.screen)}
        commands["Check for updates"].callback()

    assert calls == [True]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_app_toasts_update_available_on_splash(monkeypatch):
    calls = []
    monkeypatch.setattr("wifit3.ui.app.AUTO_CHECK_UPDATES_DEFAULT", True)
    monkeypatch.setattr("wifit3.ui.app.check_for_updates", lambda _version: calls.append(_version) or UpdateInfo(
        "0.1.3", "0.1.4", True, "https://example/release", True, False))
    app = WifiteApp()

    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        assert isinstance(pilot.app.screen, SplashView)
        toast = pilot.app.screen.query_one("#update-toast")
        label = pilot.app.screen.query_one("#update-toast-label", Label)
        assert calls
        assert app._pending_update is not None
        assert app._pending_update.latest_version == "0.1.4"
        assert toast.display is True
        assert str(label.content) == "update 0.1.4 available"


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_update_confirm_uses_force_for_dev_version(monkeypatch):
    app = WifiteApp()
    update = UpdateInfo("0.1.3-dev", "0.1.4", True, "https://example/release", False, False)
    prompts = []
    updates = []

    async def confirm(prompt):
        prompts.append(prompt)
        return True

    monkeypatch.setattr(app, "push_screen_wait", confirm)
    monkeypatch.setattr(app, "perform_update_and_restart", lambda *, force=False: updates.append(force))

    async with app.run_test() as pilot:
        await pilot.pause(0)
        assert isinstance(pilot.app.screen, SplashView)
        pilot.app.screen.show_update_available(update)
        await pilot.app.screen._confirm_update()

    assert len(prompts) == 2
    assert updates == [True]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_update_confirm_does_not_force_known_version(monkeypatch):
    app = WifiteApp()
    update = UpdateInfo("0.1.3", "0.1.4", True, "https://example/release", True, False)
    prompts = []
    updates = []

    async def confirm(prompt):
        prompts.append(prompt)
        return True

    monkeypatch.setattr(app, "push_screen_wait", confirm)
    monkeypatch.setattr(app, "perform_update_and_restart", lambda *, force=False: updates.append(force))

    async with app.run_test() as pilot:
        await pilot.pause(0)
        assert isinstance(pilot.app.screen, SplashView)
        pilot.app.screen.show_update_available(update)
        await pilot.app.screen._confirm_update()

    assert len(prompts) == 1
    assert updates == [False]


@pytest.mark.usefixtures("no_usb_devices")
def test_app_persists_theme_on_change(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr("wifit3.persist.config._PATH", config_path)
    Config.theme = "textual-dark"
    app = WifiteApp()

    app.watch_theme("textual-light")

    assert Config.theme == "textual-light"


