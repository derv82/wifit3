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
        background: $primary;    /* a visible "window" title bar */
        color: auto;             /* auto-contrast text on the bar */
    }
    .info-box {
        border: solid $primary;
        padding: 0 1;            /* title flush under the top border */
    }
    /* A left-aligned block centered as a group (TARGET's detail lines). */
    .panel-body { width: 1fr; height: auto; align-horizontal: center; }

    /* ---- Layout v2 (Option A): left action column | right summary+log --- */
    #main-row { height: 1fr; }

    /* LEFT column: TARGET / ATTACKS / CLIENTS / CLIENT DEAUTH, one shared width.
       40 wide so the attack buttons fit "Stop Replay", CLIENTS fits PKTS, and
       TARGET lines up with everything below it (and emphasizes the ESSID). */
    #left-col { width: 40; }
    #panel-target { height: 8; }          /* aligns with the SECURITY|CAPTURE row */
    /* No box around the attack buttons — they have their own borders, and a
       title-less bordered panel just adds clutter. */
    #attack-panel { height: 8; border: none; }
    #client-panel { height: 1fr; min-height: 4; }
    #deauth-panel { height: auto; }

    /* RIGHT column: SECURITY | CAPTURE summary row (forms the TARGET | SECURITY |
       CAPTURE header), then the tall EVENT LOG. */
    #right-col { width: 1fr; }
    #top-right { height: 8; }
    #panel-security { width: 38; }
    #panel-capture  { width: 38; }
    #event-log-panel { height: 1fr; }
    #focus-event-log { height: 1fr; border: none; }
    /* ESSID chip reads like a centered subtitle under the TARGET INFO title
       (static per target, so no jitter); BSSID/channel stay left-aligned. */
    #lbl-ssid { width: 100%; text-align: center; }

    /* Buttons: fat (height 3), narrow — Button defaults to min-width:16, which
       ballooned/clipped them. Width 13 fits "Stop Replay"/"Stop Frag" in the
       wide left column; rows touch vertically so 2 fit the 8-tall panel. */
    .button-row { height: auto; align-horizontal: center; }
    #attack-panel Button { width: 13; min-width: 0; }
    #deauth-panel Button { width: 13; min-width: 0; }
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

