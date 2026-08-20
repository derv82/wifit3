import pytest

from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.splash import SplashView
from wifit3.ui.screens.scanner import ScannerView
from textual.widgets import RichLog, DataTable

@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")  # ui/conftest.py
async def test_app_layout_and_boot():
    """Verify the app boots and registers the required screens."""
    app = WifiteApp()
    async with app.run_test() as pilot:
        # Check Title
        assert pilot.app.title == "wifit3 - Wireless Auditor - derv82", "App title is incorrect"
        
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

