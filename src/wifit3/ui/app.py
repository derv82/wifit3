import logging
import os
from textual.app import App
from typing import Optional

from wifit3.chips import log_trace
from wifit3.wlan.manager import WlanDeviceManager
from wifit3.engine.models import AccessPoint

from .screens.splash import SplashView
from .screens.scanner import ScannerView
from .screens.focus_v2 import FocusViewV2

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
        color: white;
        text-style: bold;
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
        self.install_screen(FocusViewV2(), name="focus")
        self.push_screen("splash")

    async def action_quit(self):
        if self.active_interface:
            await self.active_interface.close()
        self.exit()

