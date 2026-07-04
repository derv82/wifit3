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


def _configure_file_logging(default: Optional[str] = None) -> None:
    """File logging for hardware debugging → ``wifit3.log`` in the CWD.

    The TUI owns the terminal, so stderr logging is invisible (and there's no
    handler anyway): the interface's ``[NEW AP]`` / ``[M1]`` / ``[PMKID]`` frame
    trace goes nowhere during a normal run — a file is the only place it lands.

    The real launch (``__main__.main``) passes ``default="debug"`` so a released
    build always leaves a DEBUG trace behind for bug reports; bare ``WifiteApp()``
    construction (the test suite, the ``--smoke`` self-test) passes no default and
    stays silent so runs don't litter ``wifit3.log`` or force the root logger to
    DEBUG. ``WIFIT3_LOG`` overrides either way: ``off``/``0``/``none`` disables,
    ``1`` is INFO, ``debug`` is DEBUG (incl. frame bytes), ``trace`` is the
    per-USB-transfer firehose. Truncated per run so each session's trace stands alone.
    """
    global _FILE_LOGGING_CONFIGURED
    if _FILE_LOGGING_CONFIGURED:
        return
    setting = os.environ.get("WIFIT3_LOG", "").strip().lower() or (default or "")
    if setting in ("", "off", "0", "none"):
        return
    level = log_trace.level_from_env(setting)
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
    /* Force single-line header to avoid Textual's "click to expand" behavior */
    Header { height: 1 !important; }
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

    def __init__(self, default_log_level: Optional[str] = None):
        _configure_file_logging(default_log_level)
        super().__init__()
        self.device_manager = WlanDeviceManager()
        self.active_interface = None
        self.target_ap: Optional[AccessPoint] = None
        # WPS PBC auto-invade preference, shared across screens (Scanner + Focus
        # both read/toggle it via 'w'). On by default — the one active-TX exception
        # to passive-by-default (auto-captures a PSK when any AP's button is pressed).
        self.pbc_enabled: bool = True
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

