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

    CSS = """
    #ascii-art {
        content-align: center middle;
        margin-bottom: 2;
    }
    #device-row {
        width: auto;
        height: auto;
        align: center middle;
        margin-top: 1;
    }
    #start-btn {
        height: 3;
        margin-left: 2;
    }
    #uninstall-btn {
        height: 3;
        width: 7;
        min-width: 7;
        margin-left: 1;
    }
    #status-label {
        content-align: center middle;
        margin-bottom: 1;
    }
    ListView {
        width: 52;                  /* fits the longest card name */
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
        background: $primary;
        color: auto;
    }
    .info-box {
        border: solid $primary;
        padding: 0 1;
    }
    .panel-body { width: 1fr; height: auto; align-horizontal: center; }

    /* main row: left action column | right summary + log */
    #main-row { height: 1fr; }

    /* 40 wide so "Stop Replay" fits and TARGET/CLIENTS line up below */
    #left-col { width: 40; }
    #panel-target { height: 8; }          /* aligns with the SECURITY|CAPTURE row */
    /* no box: the buttons carry their own borders; height auto frees space for CLIENTS */
    #attack-panel { height: auto; border: none; margin-top: 1; }
    #client-panel { height: 1fr; min-height: 6; }
    #deauth-row { height: auto; margin-top: 1; }

    /* right column: SECURITY | CAPTURE row, then the tall EVENT LOG */
    #right-col { width: 1fr; }
    #top-right { height: 8; }
    #panel-security { width: 38; }
    #panel-capture  { width: 38; }
    /* live packet dashboard; fills the space right of CAPTURE */
    #panel-activity-box { width: 1fr; min-width: 0; }
    #panel-activity { height: 1fr; }
    #event-log-panel { height: 1fr; }
    #focus-event-log { height: 1fr; border: none; }
    #lbl-ssid { width: 100%; text-align: center; }

    .button-row { height: auto; align-horizontal: center; }
    /* 13 fits "Stop Replay"/"Stop Chop"; min-width:0 beats Button's 16 default */
    #attack-panel Button { width: 13; min-width: 0; }
    /* flat; height 2 for the stacked "Deauth / Selected" label */
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
        self.push_screen("splash")

    async def action_quit(self):
        if self.active_interface:
            await self.active_interface.close()
        self.exit()

