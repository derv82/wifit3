import logging
import os
import sys
from textual import work
from textual.app import App
from textual.binding import Binding
from typing import Optional

from wifit3 import __version__
from wifit3.chips import log_trace
from wifit3.persist.config import Config, ConfigError
from wifit3.errors import WifiteDeviceLostError, WifiteFatalError
from wifit3.device.manager import DeviceManager, Status
from wifit3.device.watch import DeviceWatch
from wifit3.wlan.array import WlanArray
from wifit3.models import AccessPoint

from .screens.splash import SplashView
from .screens.scanner import ScannerView
from .screens.focus_v2 import FocusViewV2
from .screens.error_modals import FatalErrorModal, RecoverableErrorModal
from .screens.new_device import NewDeviceDialog
from .pref import PreferencesModal
from .themes import register_app_themes

logger = logging.getLogger(__name__)


class WifiteApp(App):
    """wifit3 TUI Main App."""

    TITLE = f"wifit3 v{__version__} - derv82"

    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+p", "preferences", "Prefs")]

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
    #button-col {
        width: auto;
        height: auto;
        margin-left: 2;
    }
    #button-col Button {
        width: 13;
        height: 3;
    }
    #button-col Button:focus {
        text-style: bold reverse;   /* clear cue when Tab lands on START / Uninstall */
    }
    #start-btn {
        color: white;
        text-style: bold;
    }
    #uninstall-btn {
        margin-top: 1;              /* stacked under START */
    }
    #status-label {
        content-align: center middle;
        margin-bottom: 1;
    }
    ListView, #device-select {
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
    /* App CSS outranks a widget's DEFAULT_CSS, so lower the global min-width for the
       EvilTwin modal's compact BSSID buttons from here, not the modal. */
    EvilTwinInputModal #bssid-btns Button { min-width: 4; }
    """

    def __init__(self, cli_log_level=None):
        super().__init__()
        self._config_error: Optional[str] = None
        try:
            Config.load()
        except ConfigError as e:
            self._config_error = str(e)
        _configure_file_logging(cli_log_level)
        self.array: Optional[WlanArray] = None
        self.device_manager = DeviceManager(self)
        self.device_watch = DeviceWatch(device_manager=self.device_manager,
                                        on_change=self._on_devices_changed,
                                        on_fatal=self._on_usb_fatal)
        self.target_ap: Optional[AccessPoint] = None
        self.pbc_enabled: bool = True
        register_app_themes(self)
        self.theme = Config.theme

    def persist_config(self) -> None:
        try:
            Config.save()
        except ConfigError as e:
            self.notify(str(e), severity="error", title="Config")

    def on_mount(self) -> None:
        """Register screens, push the splash, and start the always-on device watch."""
        if self._config_error:
            self.notify(self._config_error, severity="error", title="Config")
        self.install_screen(SplashView(), name="splash")
        self.install_screen(ScannerView(), name="scanner")
        self.install_screen(FocusViewV2(), name="focus")
        self.push_screen("splash")
        self._device_timer = self.set_interval(0.5, self.device_watch.poll)
        self.call_after_refresh(self.device_watch.poll)

    def _on_devices_changed(self, current, arrived, departed) -> None:
        """DeviceWatch fired. On Splash, refresh the card list; mid-session, prompt to bring up
        each newly-plugged card."""
        if any(isinstance(s, SplashView) for s in self.screen_stack):
            self.get_screen("splash", SplashView).render_devices(current)
            return
        # Ignore already-attached devices
        fresh = [d for d in arrived if not (self.array and self.array.contains(d))]
        if fresh:
            if isinstance(self.screen, RecoverableErrorModal):
                self.screen.dismiss()
            self.device_watch.pause()     # pause synchronously so the next tick can't stack a prompt
            self._prompt_hotplug(fresh)

    @work(exclusive=True)
    async def _prompt_hotplug(self, arrived) -> None:
        """Mid-session: ask per new card, and bring up the ones the user confirms (only that card,
        and on Windows never a disruptive mid-session install)."""
        try:
            for dev in arrived:
                if await self.push_screen_wait(NewDeviceDialog(dev.description)):
                    res = await self.device_manager.bringup(
                        dev, bail_at_permissions=(sys.platform == "win32"))
                    if res.status is Status.FAILED:
                        self.notify(res.message, severity="error")
                    elif res.status is Status.READY:
                        self.notify(f"{dev.description} added", severity="information")
        finally:
            self.device_watch.resume()

    def _on_usb_fatal(self, err: WifiteFatalError) -> None:
        """The bus scan hit an unrecoverable backend error: stop watching + show the Quit-only modal."""
        self._device_timer.stop()
        self.push_screen(FatalErrorModal(err))

    def notify_device_lost(self, exc: Exception, remaining: int) -> None:
        """A pooled card vanished mid-run (the array re-emits this with the surviving card count).

        Arrives on the event-loop thread via the RX reader's ``call_soon_threadsafe`` hop, which
        runs OUTSIDE Textual's message-pump context (``active_app`` unset), so a direct
        ``push_screen`` here crashes in the modal's compose (NoActiveAppError). Defer it onto the
        app's message queue via ``call_later``; that callback runs in-context."""
        self.call_later(self._show_device_lost, exc, remaining)

    def _show_device_lost(self, exc: Exception, remaining: int) -> None:
        # Survivors remain: keep running, just toast how many are left.
        if remaining > 0:
            self.notify(f"A wireless card was lost. {remaining} still active.",
                        title="Card unplugged", severity="warning")
            return
        # Last card gone: fall back to the recoverable modal → splash.
        if isinstance(self.screen, (FatalErrorModal, RecoverableErrorModal)):
            return
        self.push_screen(RecoverableErrorModal(WifiteDeviceLostError("the wireless adapter")))

    async def recover_to_splash(self) -> None:
        """Return to the splash screen after the last card was lost."""
        array = self.array
        # Unwind to the base default screen (kept by `> 1`), then re-push splash onto it.
        while len(self.screen_stack) > 1:
            await self.pop_screen()
        await self.push_screen("splash")
        # The installed splash only resumes (on_mount won't re-run), so reset its state explicitly.
        self.get_screen("splash", SplashView).reset_for_reentry()
        # Close the dead pool only once scanner/focus are gone, so their teardown can't read a
        # half-closed interface.
        if array is not None:
            try:
                await array.close()
            except Exception:
                logger.debug("Closing the lost pool failed (already gone)", exc_info=True)
        self.array = None
        self.target_ap = None

    def action_preferences(self) -> None:
        self.push_screen(PreferencesModal())

    async def action_quit(self):
        self.persist_config()
        if self.array:
            await self.array.close()
        self.exit()


_FILE_LOGGING_CONFIGURED = False  # Avoid duplicate loggers


def _get_log_level(cli_log_level: Optional[str]) -> Optional[int]:
    """Level from ``WIFIT3_LOG`` or ``Config.log_level``, trace/debug/info/quiet."""
    env = os.environ.get("WIFIT3_LOG", cli_log_level)
    if env is None:
        env = Config.log_level
    env = env.strip().lower()
    if env == "trace":
        return log_trace.TRACE
    if env == "debug":
        return logging.DEBUG
    if env == "info":
        return logging.INFO
    return None  # any other value ("quiet") skips logging

def _configure_file_logging(cli_log_level: Optional[str]) -> None:
    """Files logged to ``wifit3.log`` in the CWD."""
    global _FILE_LOGGING_CONFIGURED
    if _FILE_LOGGING_CONFIGURED:
        return

    level = _get_log_level(cli_log_level)
    if level is None:
        return

    handler = logging.FileHandler("wifit3.log", mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    ))
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    _FILE_LOGGING_CONFIGURED = True
    logger.info(f"Logging enabled (level={logging.getLevelName(level)})")

