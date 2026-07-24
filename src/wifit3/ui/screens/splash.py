import logging
from pathlib import Path
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static, ListView, ListItem, Label, Header, Footer, Button
from textual.containers import Vertical, Center, Horizontal
from textual import work
from rich.text import Text

from wifit3.ui.ansi_art import make_black_transparent
from wifit3.ui.screens.setup_error import SetupErrorDialog
from wifit3.wlan.bringup import Status

logger = logging.getLogger(__name__)

# Suffix appended to a chipset name when 2+ of the same chip are present, so a multi-card
# list doesn't read as a wall of identical names. Flip the glyph here (e.g. "_{n}", "·{n}").
_DUP_SUFFIX = " #{n}"
# A left buffer so chipset names don't butt against the list edge. Widen here for more indent.
_LEFT_MARGIN = " "


def _alpha_head(chipset: str) -> str:
    """The leading non-digit run of a chipset name (``"RTL"`` of ``"RTL8812AU"``)."""
    i = 0
    while i < len(chipset) and not chipset[i].isdigit():
        i += 1
    return chipset[:i]


def device_list_labels(devices) -> list:
    """One Splash interface-list label per device: ``chipset[ #n] · vendor product``. Two-axis
    alignment keeps a multi-card list scannable: the alpha prefix (RTL/MT/RT/AR) is left-padded so
    the model digits line up, and the chipset column is right-padded so the ``·`` separators line
    up. ``#n`` shows only when 2+ cards share a chipset; the ``·`` tail only when a brand is known.
    Alignment is relative to the cards present now, so it re-flows on plug/unplug."""
    if not devices:
        return []
    chip_counts: dict = {}
    for d in devices:
        chip_counts[d.chipset] = chip_counts.get(d.chipset, 0) + 1

    prefix_w = max(len(_alpha_head(d.chipset)) for d in devices)
    seen: dict = {}
    heads = []
    for dev in devices:
        seen[dev.chipset] = seen.get(dev.chipset, 0) + 1
        head = " " * (prefix_w - len(_alpha_head(dev.chipset))) + dev.chipset
        if chip_counts[dev.chipset] > 1:
            head += _DUP_SUFFIX.format(n=seen[dev.chipset])
        heads.append(head)
    head_w = max(len(h) for h in heads)

    labels = []
    for dev, head in zip(devices, heads):
        brand = " ".join(x for x in (dev.vendor, dev.product_name) if x)
        body = f"{head.ljust(head_w)} · {brand}" if brand else head
        labels.append(_LEFT_MARGIN + body)
    return labels


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
        self._is_initializing = False
        # DeviceIDs from the last render (the app's DeviceListener feeds them), indexed to the rows.
        self._devices = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="splash-container"):
            with Center():
                yield Static(LOGO, id="ascii-art")
            with Center():
                yield Label("Scanning for compatible hardware…", id="status-label")
            with Center():
                # Persistent failure line. render_devices only touches #status-label, so an error
                # parked here survives the next device refresh (the status line gets overwritten).
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

    def reset_for_reentry(self) -> None:
        """Returning to splash (adapter lost): the installed screen only resumes (on_mount doesn't
        re-run) so restore the scanning state, resume the device watch perform_start paused, and
        render the currently-present cards right away (not on the next 0.5s tick)."""
        self._enter_scanning_mode()
        self.app.devices.resume()
        self.render_devices(self.app.devices.present())

    def render_devices(self, devices) -> None:
        """Render the current device list. Called by the app's DeviceListener on plug/unplug."""
        if self._is_initializing:
            return
        self._devices = devices
        list_view = self.query_one("#device-list", ListView)
        list_view.clear()
        for i, label in enumerate(device_list_labels(devices)):
            list_view.append(ListItem(Label(label), name=str(i)))

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
        self.app.devices.pause()          # freeze the device watch so the list can't churn mid-bring-up
        self.query_one("#device-list", ListView).disabled = True
        self.query_one("#start-btn", Button).disabled = True
        self.query_one("#uninstall-btn", Button).disabled = True

    def _exit_busy(self) -> None:
        self._is_initializing = False
        self.app.devices.resume()
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
