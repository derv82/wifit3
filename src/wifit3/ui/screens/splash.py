import asyncio
import logging
from pathlib import Path
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static, ListView, ListItem, Label, Header, Footer, Button
from textual.containers import Vertical, Center, Horizontal
from textual import work
from rich.text import Text

from wifit3.errors import WifiteFatalError
from wifit3.ui.ansi_art import make_black_transparent
from wifit3.ui.screens.setup_error import SetupErrorDialog
from wifit3.ui.screens.error_modals import FatalErrorModal
from wifit3.wlan.bringup import Status
from wifit3.wlan.discovery import find_devices

logger = logging.getLogger(__name__)


def load_logo() -> Text:
    """Load the ANSI logo from assets."""
    logo_path = Path(__file__).parent.parent / "assets" / "logo_sm.ans"
    try:
        if logo_path.exists():
            return make_black_transparent(
                Text.from_ansi(logo_path.read_text(encoding="utf-8"))
            )
    except Exception:
        pass

    # Fallback
    return Text.from_markup("[bold green]Wifit3[/bold green]\n[dim green]// Wireless Auditor[/dim green]")

LOGO = load_logo()

class SplashView(Screen):
    """Splash + device picker: the logo, the list of live cards, Start and Uninstall buttons. START
    and ✕ delegate the whole bring-up / setup flow to ``app.bringup``; the splash only picks the card
    and reports the terminal result."""

    BINDINGS = [("q", "app.quit", "Quit")]

    def __init__(self):
        super().__init__()
        self._refresh_timer = None
        self._last_signature = None
        self._is_initializing = False
        # Guard so overlapping polls don't stack (a bus scan can outlast the poll interval).
        self._poll_in_flight = False
        # DeviceIDs from the last poll, indexed to match the ListView rows.
        self._devices = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="splash-container"):
            with Center():
                yield Static(LOGO, id="ascii-art")
            with Center():
                yield Label("Scanning for compatible hardware…", id="status-label")
            with Center():
                # Persistent failure line. poll_usb only ever touches #status-label, so an error
                # parked here survives the next bus scan (the status line gets overwritten ~2x/s).
                yield Label("", id="error-label")
            with Center():
                with Horizontal(id="device-row"):
                    yield ListView(id="device-list")
                    yield Button("START", id="start-btn", variant="success")
                    # Compact uninstall: reverses wifit3's driver/access changes.
                    yield Button("✕", id="uninstall-btn", variant="error")
        yield Footer()

    def _enter_scanning_mode(self) -> None:
        """The 'pick a card' resting state."""
        self._is_initializing = False
        self._last_signature = None
        self._devices = []
        self.query_one("#error-label").display = False
        device_list = self.query_one("#device-list", ListView)
        device_list.clear()
        device_list.disabled = False
        self.query_one("#start-btn", Button).disabled = True
        self.query_one("#uninstall-btn", Button).disabled = True
        self.query_one("#status-label", Label).update("Scanning for compatible hardware…")

    async def on_mount(self) -> None:
        self.query_one("#uninstall-btn", Button).tooltip = (
            "Uninstall the wifit3 driver / access rule for the selected card")
        self._enter_scanning_mode()
        self._refresh_timer = self.set_interval(0.5, self.poll_usb)
        self.call_after_refresh(self.poll_usb)

    def reset_for_reentry(self) -> None:
        """Returning to splash (adapter lost): the installed screen only resumes (on_mount doesn't
        re-run) so restore the scanning state and un-pause the poll timer perform_start left paused
        before it navigated to the scanner."""
        self._enter_scanning_mode()
        if self._refresh_timer is not None:
            self._refresh_timer.resume()
        self.call_after_refresh(self.poll_usb)   # repopulate now, not on the next 0.5s tick

    async def poll_usb(self) -> None:
        if self._is_initializing or self._poll_in_flight:
            return
        self._poll_in_flight = True
        try:
            devices = await asyncio.to_thread(find_devices)
            signature = tuple((d.vid, d.pid, d.description) for d in devices)
            if signature == self._last_signature:
                return
            self._last_signature = signature
            self._devices = devices

            list_view = self.query_one("#device-list", ListView)
            list_view.clear()
            for i, dev in enumerate(devices):
                list_view.append(ListItem(Label(dev.description), name=str(i)))

            status = self.query_one("#status-label", Label)
            start_btn = self.query_one("#start-btn", Button)
            uninstall_btn = self.query_one("#uninstall-btn", Button)
            if devices:
                status.update("[bold lightgreen]Select a card and press START[/bold lightgreen]")
                start_btn.disabled = False
                uninstall_btn.disabled = False
                # clear() reset index to None; re-arm the highlight so START has a target.
                if list_view.index is None:
                    list_view.index = 0
                    list_view.focus()
            else:
                status.update("Scanning for compatible hardware…")
                start_btn.disabled = True
                uninstall_btn.disabled = True
        except WifiteFatalError as err:
            # Unrecoverable (e.g. no USB backend) and it surfaces on the very first scan: stop
            # polling and replace the splash with the Quit-only fatal modal.
            self._refresh_timer.stop()
            self.app.push_screen(FatalErrorModal(err))
        finally:
            self._poll_in_flight = False

    def _show_error(self, message: str) -> None:
        """Surface a recoverable bring-up failure: a persistent red label (which poll_usb leaves
        alone, unlike the status line) plus a toast."""
        label = self.query_one("#error-label", Label)
        label.update(f"[bold red]⚠  {message}[/bold red]")
        label.display = True
        self.notify(message, title="Card bring-up failed", severity="error")

    def _clear_error(self) -> None:
        label = self.query_one("#error-label", Label)
        label.update("")
        label.display = False

    def _selected_device(self):
        """The DeviceID of the highlighted row, or None."""
        index = self.query_one("#device-list", ListView).index
        if index is None or index >= len(self._devices):
            return None
        return self._devices[index]

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter on a row starts that card."""
        if self._is_initializing:
            return
        dev = self._selected_device()
        if dev is not None:
            self.perform_start(dev)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self._is_initializing:
            return
        dev = self._selected_device()
        if dev is None:
            return
        if event.button.id == "start-btn":
            self.perform_start(dev)
        elif event.button.id == "uninstall-btn":
            self.perform_uninstall(dev)

    def _enter_busy(self) -> None:
        self._is_initializing = True
        if self._refresh_timer:
            self._refresh_timer.pause()
        self.query_one("#device-list", ListView).disabled = True
        self.query_one("#start-btn", Button).disabled = True
        self.query_one("#uninstall-btn", Button).disabled = True

    def _exit_busy(self) -> None:
        self._is_initializing = False
        if self._refresh_timer:
            self._refresh_timer.resume()
        device_list = self.query_one("#device-list", ListView)
        device_list.disabled = False
        self.query_one("#start-btn", Button).disabled = False
        self.query_one("#uninstall-btn", Button).disabled = False
        device_list.focus()

    @work(exclusive=True)
    async def perform_start(self, device_id) -> None:
        """Bring up the selected card through the engine; enter the scanner on success. The engine
        owns the progress modal, the install/replug dialogs, and the platform branching."""
        self._clear_error()
        self._enter_busy()
        try:
            res = await self.app.bringup.run(device_id)
        finally:
            self._exit_busy()

        if res.status is Status.READY:
            self._last_signature = None
            self.app.switch_screen("scanner")
        elif res.status is Status.FAILED:
            self._show_error(res.message)
        else:  # CANCELLED
            self.query_one("#status-label", Label).update(
                "[bold lightgreen]Select a card and press START[/bold lightgreen]")

    @work(exclusive=True)
    async def perform_uninstall(self, device_id) -> None:
        """Reverse wifit3's driver/access for the selected card via the engine."""
        self._clear_error()
        self._enter_busy()
        try:
            res = await self.app.bringup.uninstall(device_id)
        finally:
            self._exit_busy()

        self._last_signature = None   # re-scan so the list reflects the card's new binding state
        status = self.query_one("#status-label", Label)
        if res.ok:
            status.update(f"[bold green]{res.message}[/bold green]")
            self.notify(f"[green]✓[/green] {res.message}", title="Uninstalled",
                        severity="information")
        elif res.cancelled:
            status.update("[bold lightgreen]Select a card and press START[/bold lightgreen]")
        else:
            status.update("[bold red]Uninstall failed.[/bold red]")
            self.app.push_screen(SetupErrorDialog("Uninstall failed", res.message, res.detail))
