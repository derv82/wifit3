import asyncio
import logging
from textual.app import App
from typing import Dict, Optional

from wifit3.wlan.manager import WlanDeviceManager
from wifit3.engine.models import AccessPoint

from .screens.splash import SplashView
from .screens.scanner import ScannerView
from .screens.focus import FocusView

logger = logging.getLogger(__name__)

class WifiteApp(App):
    """wifit3 TUI Main App."""

    TITLE = "wifit3 - Wireless Auditor"
    
    # Textual supports extensive CSS for styling components.
    # We removed the global green/black override so the default Textual theme (which has visible scrollbars) works properly.
    CSS = """
    #ascii-art {
        content-align: center middle;
        margin-bottom: 2;
    }
    #os-warning {
        content-align: center middle;
        margin-bottom: 1;
    }
    #status-label {
        content-align: center middle;
        margin-bottom: 1;
    }
    ListView {
        width: 60%;
        height: auto;
    }
    DataTable {
        width: 100%;
        height: 1fr;
    }
    RichLog {
        height: 10;
        border-top: solid $primary;
    }
    #focus-container {
        padding: 1;
    }
    .panel-title {
        text-style: bold;
        width: 100%;
        content-align: center middle;
        background: $boost;
    }
    .info-box {
        border: solid $primary;
        width: 1fr;
        padding: 1;
    }
    #ap-info-panel {
        height: 8;
    }
    #client-panel {
        height: 1fr;
    }
    #attack-panel {
        height: auto;
        border: solid $primary;
        padding: 1;
    }
    .button-row {
        height: auto;
        content-align: center middle;
        margin-top: 1;
    }
    Button {
        margin-right: 2;
    }
    """

    def __init__(self):
        super().__init__()
        self.device_manager = WlanDeviceManager()
        self.active_interface = None
        self.target_ap: Optional[AccessPoint] = None
        self.theme = "textual-dark"

    def on_mount(self) -> None:
        """Register screens and push the initial SplashView."""
        self.install_screen(SplashView(self.device_manager), name="splash")
        self.install_screen(ScannerView(), name="scanner")
        self.install_screen(FocusView(), name="focus")
        
        # Start with the splash screen
        self.push_screen("splash")

    async def action_quit(self):
        if self.active_interface:
            await self.active_interface.close()
        self.exit()

