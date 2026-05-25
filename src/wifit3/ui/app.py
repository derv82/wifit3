import logging
from textual.app import App
from typing import Optional

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
        padding: 0 1;            /* no top blank — title flush under the border, like the bottom row */
    }
    #ap-info-panel {
        /* Tight to the content: border(2) + title + up to 5 rows. WEP tops out
           at 4 (enc/fake-auth/crack/crack-info) but a WPA3-transition SECURITY
           can hit 5 (enc/WPS/PMF/WPA3/SAE-groups), so 8 (not 7) avoids clipping
           that 5th row while still dropping a trailing blank. */
        height: 8;
    }
    /* TARGET INFO holds SSID/BSSID/channel — wide enough for a full 32-char
       ESSID ("ESSID: " + 32 = 39 + border/padding). SECURITY + CAPTURE share
       the rest (1fr each), and centre their contents under the title. */
    #panel-target {
        width: 43;
    }
    /* Centre the content BLOCK (left-aligned lines sharing a left edge), not
       each line independently: the .panel-body fills the width and
       align-horizontal centres its labels AS A GROUP (the group's box is the
       widest row; the labels keep their left edge). The full-width title is a
       sibling OUTSIDE this wrapper, so it still spans the whole panel. */
    .panel-body {
        width: 1fr;
        height: auto;
        align-horizontal: center;
    }
    #client-panel {
        height: 1fr;
        min-height: 6;
    }
    #bottom-row {
        height: 12;
    }
    /* Bottom-row action panels get a blank line between the title and the
       first button (breathing room); EVENT LOG keeps its title flush against
       the log text. */
    #deauth-panel .panel-title, #attack-panel .panel-title {
        margin-bottom: 1;
    }
    #deauth-panel {
        width: 16;
        height: 100%;
        border: solid $primary;
        padding: 0 1;
    }
    #deauth-panel Button {
        width: 100%;
        min-width: 0;
        margin: 0 0 1 0;
    }
    #attack-panel {
        width: 32;
        height: 100%;
        border: solid $primary;
        padding: 0 1;
    }
    #attack-panel Button {
        width: 13;
        min-width: 0;
    }
    #event-log-panel {
        width: 1fr;
        height: 100%;
        border: solid $primary;
        padding: 0 1;
    }
    #focus-event-log {
        height: 1fr;
        border: none;
    }
    .button-row {
        height: auto;
        align-horizontal: center;   /* symmetric L/R padding in every state */
        margin: 0 0 1 0;            /* gap below each row; none above (flush under title, like DEAUTH) */
    }
    Button {
        margin-right: 1;
        min-width: 12;
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

