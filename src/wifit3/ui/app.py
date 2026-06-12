import logging
import os
from textual.app import App
from typing import Optional

from wifit3.chips import log_trace
from wifit3.wlan.manager import WlanDeviceManager
from wifit3.engine.models import AccessPoint

from .screens.splash import SplashView
from .screens.scanner import ScannerView
from .screens.focus import FocusView

logger = logging.getLogger(__name__)

# Set once so repeated WifiteApp() instances (the test suite makes many) don't
# stack duplicate handlers or re-truncate the log.
_FILE_LOGGING_CONFIGURED = False


def _configure_file_logging() -> None:
    """Opt-in file logging for hardware debugging — off by default.

    The TUI owns the terminal, so stderr logging is invisible (and there's no
    handler anyway): the interface's ``[NEW AP]`` / ``[M1]`` / ``[PMKID]`` frame
    trace goes nowhere during a normal run. Set ``WIFIT3_LOG=1`` to capture INFO
    (``WIFIT3_LOG=debug`` for DEBUG incl. frame bytes; ``WIFIT3_LOG=trace`` for the
    per-USB-transfer firehose) to ``wifit3.log`` in the CWD. Truncated per run so
    each session's trace stands alone.
    """
    global _FILE_LOGGING_CONFIGURED
    level_env = os.environ.get("WIFIT3_LOG", "").strip().lower()
    if not level_env or _FILE_LOGGING_CONFIGURED:
        return
    level = log_trace.level_from_env(level_env)
    handler = logging.FileHandler("wifit3.log", mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    ))
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    _FILE_LOGGING_CONFIGURED = True
    logger.info("File logging enabled (level=%s) → wifit3.log",
                logging.getLevelName(level))

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
    /* Splash device picker: the card list and the START button sit side by side,
       centered as a group. */
    #device-row {
        width: auto;
        height: auto;
        align: center middle;
        margin-top: 1;
    }
    #start-btn {
        height: 3;
        margin-left: 2;             /* gap between the card list and START */
    }
    #uninstall-btn {
        height: 3;
        width: 7;                   /* compact ✕ — reverses wifit3's driver/access change */
        min-width: 7;
        margin-left: 1;
    }
    #status-label {
        content-align: center middle;
        margin-bottom: 1;
    }
    ListView {
        width: 52;                  /* fits the longest card name; keeps the picker compact */
        height: auto;
        max-height: 12;
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
       title-less bordered panel just adds clutter. Height auto so the panel
       takes only its button row(s) — 1 for WEP (Replay/Chop), 2 for WPA — and
       the CLIENTS list below claims all the freed space. */
    #attack-panel { height: auto; border: none; margin-top: 1; }
    #client-panel { height: 1fr; min-height: 6; }
    /* Deauth buttons sit at the bottom of the CLIENTS panel (no own title). */
    #deauth-row { height: auto; margin-top: 1; }

    /* RIGHT column: SECURITY | CAPTURE summary row (forms the TARGET | SECURITY |
       CAPTURE header), then the tall EVENT LOG. */
    #right-col { width: 1fr; }
    #top-right { height: 8; }
    #panel-security { width: 38; }
    #panel-capture  { width: 38; }
    /* Live packet dashboard fills the dead space right of CAPTURE. Bordered +
       titled like SECURITY/CAPTURE; 1fr + min-width:0 lets it claim leftover
       width on a wide terminal and collapse toward nothing on a narrow one.
       Now fits the 8-tall row: border(2) + title(1) + ≤5 class rows (beacon,
       data, one of wep-iv/eapol, inject, deauth — the two are encryption-
       gated so never both show). */
    #panel-activity-box { width: 1fr; min-width: 0; }
    #panel-activity { height: 1fr; }
    #event-log-panel { height: 1fr; }
    #focus-event-log { height: 1fr; border: none; }
    /* ESSID chip reads like a centered subtitle under the TARGET INFO title
       (static per target, so no jitter); BSSID/channel stay left-aligned. */
    #lbl-ssid { width: 100%; text-align: center; }

    /* Buttons: fat (height 3), narrow — Button defaults to min-width:16, which
       ballooned/clipped them. Width 13 fits "Stop Replay"/"Stop Chop" in the
       wide left column; rows touch vertically so the WPA set's 2 fit the
       8-tall panel (WEP uses a single Replay/Chop row, vertically centered). */
    .button-row { height: auto; align-horizontal: center; }
    #attack-panel Button { width: 13; min-width: 0; }
    /* DEAUTH buttons are flat (no border), 2 rows high for the stacked
       "Deauth / Selected" label. Each is 1fr so the pair fills the row edge
       to edge; the left one carries the only gap so they stay symmetric
       (overrides the global Button margin-right). */
    #deauth-row Button { width: 1fr; min-width: 0; height: 2; border: none; margin: 0; content-align: center middle; text-align: center; }
    #deauth-row Button#btn-deauth-sel { margin-right: 1; }
    Button {
        margin-right: 1;
        min-width: 12;
    }
    """

    def __init__(self):
        _configure_file_logging()
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

