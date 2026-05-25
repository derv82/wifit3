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
        padding: 0 1;            /* title flush under the top border */
    }

    /* ---- Top row: TARGET | ATTACKS (no title) | CAPTURE ---------------- */
    /* Height = 2 fat button rows (3 each) + border. Fixed-width panels so
       nothing reflows on a wide terminal; content top-aligns. */
    #top-row { height: 8; }
    #panel-target  { width: 30; }   /* SSID chip / BSSID(24) / channel / last-beacon */
    #attack-panel  { width: 26; }   /* 2x2 buttons, no title bar */
    #panel-capture { width: 30; }

    /* ---- Lower row: [SECURITY / CLIENTS / DEAUTH] | EVENT LOG ---------- */
    #lower-row { height: 1fr; }
    #left-col  { width: 34; height: 1fr; }
    #panel-security { height: auto; }
    #client-panel   { height: 1fr; min-height: 4; }
    #deauth-panel   { height: auto; }
    #event-log-panel { width: 1fr; height: 1fr; }
    #focus-event-log { height: 1fr; border: none; }

    /* Buttons: fat (height 3) but narrow — Button defaults to min-width:16,
       which is what ballooned/clipped them. Rows touch vertically (no margin)
       so 2 rows fit the 8-tall top panel exactly. */
    .button-row { height: auto; align-horizontal: center; }
    #attack-panel Button { width: 10; min-width: 0; }
    #deauth-panel Button { width: 12; min-width: 0; }
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

