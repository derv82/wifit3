import pytest

from wifit3.chips.driver import DeviceID
from wifit3.persist.config import Config
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.splash import SplashView
from wifit3.ui.screens.scanner import ScannerView
from textual.widgets import Button, RichLog

from wifit3.ui.ap_table import APTable


def test_desktop_notification_requires_blurred_enabled_app(mocker):
    app = WifiteApp()
    send = mocker.patch("wifit3.ui.app.send_system_notification", return_value=True)
    Config.desktop_notifications = True

    assert app.desktop_notify("Title", "Body") is False
    app._app_focused = False
    assert app.desktop_notify("Title", "Body") is True
    send.assert_called_once_with("Title", "Body")

    Config.desktop_notifications = False
    assert app.desktop_notify("Disabled", "Body") is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_narrow_splash_moves_buttons_below_device_list():
    app = WifiteApp()
    async with app.run_test(size=(60, 33)) as pilot:
        splash = app.screen
        splash.render_devices([DeviceID(0x2357, 0x0106, "RTL8814AU", "TP-Link", "T9UH")])
        await pilot.pause(0)

        device_list = splash.query_one("#device-list")
        start = splash.query_one("#start-btn", Button)
        uninstall = splash.query_one("#uninstall-btn", Button)
        assert splash.query_one("#ascii-art").display is True
        assert device_list.region.x > 0
        assert start.region.y >= device_list.region.bottom
        assert uninstall.region.y == start.region.y
        assert start.region.x == device_list.region.x
        assert uninstall.region.right == device_list.region.right


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_splash_device_list_scrolls_when_cards_exceed_available_height():
    app = WifiteApp()
    async with app.run_test(size=(60, 33)) as pilot:
        splash = app.screen
        splash.render_devices([
            DeviceID(0x148F, 0x3000 + index, f"RT{3000 + index}")
            for index in range(20)
        ])
        await pilot.pause(0)

        device_select = splash.query_one("#device-select")
        assert device_select.region.height <= 12
        assert device_select.max_scroll_y > 0


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_splash_54x24_keeps_buttons_above_footer_with_many_cards():
    app = WifiteApp()
    async with app.run_test(size=(54, 24)) as pilot:
        splash = app.screen
        splash.render_devices([
            DeviceID(0x148F, 0x3000 + index, f"RT{3000 + index}")
            for index in range(20)
        ])
        await pilot.pause(0)

        device_select = splash.query_one("#device-select")
        start = splash.query_one("#start-btn", Button)
        uninstall = splash.query_one("#uninstall-btn", Button)
        footer = splash.query_one("Footer")
        splash.query_one("GlobalJobTracker").display = True
        await pilot.pause(0)
        assert device_select.region.height <= 4
        assert device_select.max_scroll_y > 0
        assert start.region.bottom <= footer.region.y
        assert uninstall.region.bottom <= footer.region.y


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_short_splash_hides_logo_through_32_rows():
    app = WifiteApp()
    async with app.run_test(size=(80, 32)) as pilot:
        await pilot.pause(0)
        assert app.screen.query_one("#ascii-art").display is False
        compact_logo = app.screen.query_one("#compact-logo")
        assert compact_logo.display is True
        assert len(compact_logo.content.split("\n")) == 8


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_short_splash_expands_scrollable_list_when_height_is_available():
    app = WifiteApp()
    async with app.run_test(size=(54, 32)) as pilot:
        splash = app.screen
        splash.render_devices([
            DeviceID(0x148F, 0x3000 + index, f"RT{3000 + index}")
            for index in range(20)
        ])
        await pilot.pause(0)

        device_select = splash.query_one("#device-select")
        footer = splash.query_one("Footer")
        assert 4 < device_select.region.height <= 8
        assert device_select.max_scroll_y > 0
        assert splash.query_one("#start-btn", Button).region.bottom <= footer.region.y


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_splash_logo_returns_above_32_rows():
    app = WifiteApp()
    async with app.run_test(size=(80, 34)) as pilot:
        await pilot.pause(0)
        assert app.screen.query_one("#ascii-art").display is True
        assert app.screen.query_one("#compact-logo").display is False


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
        table = pilot.app.screen.query_one("#ap-table", APTable)
        assert table is not None
        log = pilot.app.screen.query_one("#system-log", RichLog)
        assert log is not None
        
        # Check that FocusViewV2 is registered (but requires target_ap to mount properly without escaping immediately, so we won't push it here)
        assert "focus" in pilot.app._installed_screens

