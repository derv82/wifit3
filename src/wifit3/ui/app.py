import asyncio
import logging
import os
import signal
import sys
from textual import events, work
from textual.app import App
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Header
from textual.widgets._header import HeaderClock, HeaderIcon, HeaderTitle
from typing import Optional

from wifit3 import __version__
from wifit3.chips import log_trace
from textual.reactive import reactive
from typing import List, Set
from wifit3.models.jobs import JobState, ToolStatus
from wifit3.persist.config import Config, ConfigError
from wifit3.persist.vault import Vault
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
from .screens.vault_drawer import VaultDrawer
from .screens.vault_table import VaultTable
from .clipboard import copy_macos_text, copy_windows_text
from .pref import PreferencesModal
from .themes import register_app_themes

logger = logging.getLogger(__name__)

Header.ALLOW_SELECT = False
HeaderTitle.ALLOW_SELECT = False
HeaderIcon.ALLOW_SELECT = False
HeaderClock.ALLOW_SELECT = False


class WifiteApp(App):
    """wifit3 TUI Main App."""

    TITLE = f"wifit3 v{__version__} - derv82"

    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+p", "preferences", "Prefs"),
        Binding("v", "toggle_vault", "Vault")
    ]

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
    RichLog, SelectableRichLog {
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
    /* File/path picker buttons are 1 space each side. */
    PathInput Button { min-width: 6; margin-right: 0; }
    PathPickerModal #picker-roots Button { min-width: 0; margin-right: 1; }
    """

    class JobCracked(Message):
        """An auto-crack job recovered a key; posted to every screen on the stack."""

        def __init__(self, job: JobState) -> None:
            super().__init__()
            self.job = job

    active_jobs: reactive[List[JobState]] = reactive([], always_update=True)
    vault_open: reactive[bool] = reactive(False)


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
        self.vault = Vault()
        self.vault.subscribe(self._refresh_vault_table)
        self.vault.set_probed_ssids_source(self._probed_ssids)
        self._job_status: dict[str, ToolStatus] = {}
        self.pbc_enabled: bool = Config.wps_auto_pbc
        register_app_themes(self)
        self.theme = Config.theme

    def _probed_ssids(self) -> Set[str]:
        return self.array.probed_ssids if self.array is not None else set()

    def copy_to_clipboard(self, text: str) -> None:
        """Copy through Win32 or pbcopy, falling back to terminal OSC 52."""
        if not copy_windows_text(text) and not copy_macos_text(text):
            super().copy_to_clipboard(text)

    def on_text_selected(self, event: events.TextSelected) -> None:
        selected = self.screen.get_selected_text()
        if not selected:
            return
        self.copy_to_clipboard(selected)
        chars = len(selected)
        noun = "char" if chars == 1 else "chars"
        self.notify(f"Copied {chars} {noun} to clipboard")

    def persist_config(self) -> None:
        try:
            Config.save()
        except ConfigError as e:
            self.notify(str(e), severity="error", title="Config")

    def on_mount(self) -> None:
        """Register screens, push the splash, and start the always-on device watch."""
        if self._config_error:
            self.notify(self._config_error, severity="error", title="Config")
        for msg in self.vault.errors:
            self.notify(msg, severity="warning", title="Vault")
        self.install_screen(SplashView(), name="splash")
        self.install_screen(ScannerView(), name="scanner")
        self.install_screen(FocusViewV2(), name="focus")
        
        self.push_screen("splash")
        self._device_timer = self.set_interval(0.5, self.device_watch.poll)
        self.set_interval(2.0, self._poll_jobs)
        self.call_after_refresh(self.device_watch.poll)
        self.call_after_refresh(self._poll_jobs)
        self._quit_on_termination_signals()

    def _quit_on_termination_signals(self) -> None:
        if sys.platform == "win32":
            return
        loop = asyncio.get_running_loop()
        signals = (signal.SIGTERM, signal.SIGHUP)

        def quit_on_signal(signum: int, _frame) -> None:
            # Not loop.add_signal_handler: a dead terminal blocks Textual's loop, which would then
            # swallow every signal. Restoring the defaults here lets the next one kill.
            for s in signals:
                signal.signal(s, signal.SIG_DFL)
            loop.call_soon_threadsafe(self._quit_after_signal, signal.Signals(signum))

        self._handlers_before_mount = {sig: signal.signal(sig, quit_on_signal) for sig in signals}

    def on_unmount(self) -> None:
        for sig, handler in getattr(self, "_handlers_before_mount", {}).items():
            signal.signal(sig, handler)

    def _quit_after_signal(self, sig: signal.Signals) -> None:
        logger.info("Received %s, closing the cards before exit", sig.name)
        self.call_later(self.action_quit)

    def _poll_jobs(self) -> None:
        self.vault.manager.poll_jobs()
        self.active_jobs = self.vault.manager.get_active_jobs()
        self._notify_completions()

    def _notify_completions(self) -> None:
        """Toast each job the first time it finishes this session (auto-crack jobs only on SUCCESS);
        jobs already terminal when the app started are recorded silently."""
        terminal = (
            ToolStatus.SUCCESS, ToolStatus.FAILURE, ToolStatus.ERROR, ToolStatus.STOPPED,
        )
        for job in self.active_jobs:
            prev = self._job_status.get(job.job_id)
            if job.status in terminal and prev is not None and prev not in terminal:
                succeeded = job.status == ToolStatus.SUCCESS
                if succeeded or not job.auto_crack:
                    self._toast_job(job)
                if succeeded and job.auto_crack:
                    for screen in self.screen_stack:
                        screen.post_message(self.JobCracked(job))
            self._job_status[job.job_id] = job.status
        live = {job.job_id for job in self.active_jobs}
        self._job_status = {k: v for k, v in self._job_status.items() if k in live}

    def _refresh_vault_table(self) -> None:
        """Reload the vault table if its drawer is open."""
        if isinstance(self.screen, VaultDrawer):
            try:
                self.screen.query_one("#vault-table", VaultTable).reload_table()
            except Exception:
                logger.debug("Vault table refresh skipped", exc_info=True)

    def _toast_job(self, job: JobState) -> None:
        if job.status == ToolStatus.SUCCESS:
            self.notify(f"SUCCESS  PSK: {job.key}", title=job.display_name, severity="information")
        elif job.status == ToolStatus.FAILURE:
            self.notify(f"NOT FOUND  {job.progress_msg}", title=job.display_name, severity="warning")
        elif job.status == ToolStatus.STOPPED:
            self.notify("STOPPED  by user", title=job.display_name, severity="information")
        else:
            self.notify(f"ERROR  {job.progress_msg}", title=job.display_name, severity="error")

    def stop_job(self, job_id: str) -> bool:
        """Stop a running job, then refresh the tracker immediately."""
        stopped = self.vault.manager.stop_job(job_id)
        if stopped:
            self._poll_jobs()
        return stopped

    def skip_job_wordlist(self, job_id: str) -> bool:
        """Abandon the wordlist a running job is on and start it on the next one."""
        skipped = self.vault.manager.skip_wordlist(job_id)
        if skipped:
            self._poll_jobs()
        return skipped

    def pause_job(self, job_id: str) -> bool:
        paused = self.vault.manager.pause_job(job_id)
        if paused:
            self._poll_jobs()
        return paused

    def resume_job(self, job_id: str) -> bool:
        resumed = self.vault.manager.resume_job(job_id)
        if resumed:
            self._poll_jobs()
        return resumed

    def clear_job(self, job_id: str) -> None:
        """Remove a finished job, then refresh the tracker immediately."""
        self.vault.manager.clear_job(job_id)
        self.active_jobs = self.vault.manager.get_active_jobs()

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
        self.vault.manager.kill_all_running()
        if self.array:
            await self.array.close()
        self.exit()

    def action_toggle_vault(self) -> None:
        """Open the vault drawer on the AP the current screen has selected."""
        if isinstance(self.screen, VaultDrawer):
            return
        self.vault_open = True

        def _on_dismiss(_=None):
            self.vault_open = False

        ap: Optional[AccessPoint] = None
        if isinstance(self.screen, ScannerView):
            ap = self.screen.selected_ap()
        elif isinstance(self.screen, FocusViewV2):
            ap = self.target_ap
        self.push_screen(VaultDrawer(ap.bssid if ap is not None else None),
                         callback=_on_dismiss)

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

