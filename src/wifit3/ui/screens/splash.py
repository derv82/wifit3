import logging
import sys
from pathlib import Path
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import (
    Static, ListView, ListItem, Label, Header, Footer, Button, SelectionList)
from textual.widgets.selection_list import Selection
from textual.containers import Vertical, Center, Horizontal
from textual import events, work
from rich.text import Text

from typing import TYPE_CHECKING

from wifit3.ui.ansi_art import make_black_transparent, recolor_logo
from wifit3.ui.screens.setup_error import SetupErrorDialog
from wifit3.device.manager import Status

if TYPE_CHECKING:
    from wifit3.ui.app import WifiteApp

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
            return make_black_transparent(Text.from_ansi(logo_path.read_text(encoding="utf-8")))
    except Exception:
        pass

    # Fallback
    return Text.from_markup("[bold green]Wifit3[/bold green]\n[dim green]// Wireless Auditor[/dim green]")

LOGO = load_logo()

class SplashView(Screen):
    """Splash + device picker: the logo, the list of live cards, Start and Uninstall buttons. START
    and Uninstall delegate the whole bring-up / setup flow to ``app.device_manager``; the splash only
    picks the cards and reports the terminal result."""

    app: "WifiteApp"

    BINDINGS = [
        ("q", "app.quit", "Quit"),
        Binding("enter", "enter", "Start", priority=True),
    ]

    def __init__(self):
        super().__init__()
        self._is_initializing = False
        # DeviceIDs from the last render (the app's DeviceWatch feeds them), indexed to the rows.
        self._devices = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="splash-container"):
            with Center():
                yield Static(self._logo(), id="ascii-art")
            with Center():
                yield Label("Scanning for compatible hardware…", id="status-label")
            with Center():
                # Persistent failure line. render_devices only touches #status-label, so an error
                # parked here survives the next device refresh (the status line gets overwritten).
                yield Label("", id="error-label")
            with Center():
                with Horizontal(id="device-row"):
                    # One card: a plain highlighted list. 2+ cards: a checkbox list (default all
                    # checked) so the user picks the subset to bring up. render_devices shows one.
                    yield ListView(id="device-list")
                    yield SelectionList(id="device-select")
                    with Vertical(id="button-col"):
                        yield Button("START", id="start-btn", variant="success")
                        # Reverses wifit3's driver/access changes for the highlighted card.
                        yield Button("Uninstall", id="uninstall-btn", variant="error")
        yield Footer()

    def _both_lists(self):
        """The (single_list, multi_list) pair: the ListView shown for one card, the SelectionList
        checkbox list shown for 2+. render_devices displays exactly one at a time."""
        return (self.query_one("#device-list", ListView),
                self.query_one("#device-select", SelectionList))

    def _logo(self) -> Text:
        theme = self.app.current_theme
        return recolor_logo(LOGO, theme.variables, dark=theme.dark)

    def refresh_theme_art(self) -> None:
        logo = self.query_one("#ascii-art", Static)
        logo.update(self._logo())

    def _enter_scanning_mode(self) -> None:
        """The 'pick a card' resting state."""
        self._is_initializing = False
        self._devices = []
        self.query_one("#error-label").display = False
        single_list, multi_list = self._both_lists()
        single_list.clear()
        single_list.disabled = False
        single_list.display = True
        multi_list.clear_options()
        multi_list.disabled = False
        multi_list.display = False
        self.query_one("#start-btn", Button).disabled = True
        self.query_one("#uninstall-btn", Button).disabled = True
        self.query_one("#status-label", Label).update("Scanning for compatible hardware…")

    async def on_mount(self) -> None:
        uninstall = self.query_one("#uninstall-btn", Button)
        if sys.platform == "darwin":
            # macOS has no install step, so there's nothing to uninstall.
            uninstall.display = False
        else:
            hint = "the WinUSB driver" if sys.platform == "win32" else "the udev/modprobe rules"
            uninstall.tooltip = f"Uninstall {hint} for the highlighted card"
        self.app.theme_changed_signal.subscribe(self, lambda _theme: self.refresh_theme_art())
        self._enter_scanning_mode()

    def reset_for_reentry(self) -> None:
        """Returning to splash (adapter lost): the installed screen only resumes (on_mount doesn't
        re-run) so restore the scanning state, resume the device watch perform_start paused, and
        render the currently-present cards right away (not on the next 0.5s tick)."""
        self._enter_scanning_mode()
        self.app.device_watch.resume()
        self.render_devices(self.app.device_watch.present())

    def render_devices(self, devices) -> None:
        """Render the current device list. Called by the app's DeviceWatch on plug/unplug. One card
        shows a plain ListView; 2+ show a default-all-checked SelectionList so the user picks a subset."""
        if self._is_initializing:
            return
        self._devices = devices
        single_list, multi_list = self._both_lists()
        labels = device_list_labels(devices)
        multi = len(devices) >= 2
        if multi:
            multi_list.clear_options()
            multi_list.add_options([Selection(labels[i], i, initial_state=True)
                                    for i in range(len(devices))])
            single_list.display = False
            multi_list.display = True
        else:
            single_list.clear()
            for i, label in enumerate(labels):
                single_list.append(ListItem(Label(label), name=str(i)))
            multi_list.display = False
            single_list.display = True

        status = self.query_one("#status-label", Label)
        start_btn = self.query_one("#start-btn", Button)
        uninstall_btn = self.query_one("#uninstall-btn", Button)
        if devices:
            status.update(self._ready_prompt())
            start_btn.disabled = False
            uninstall_btn.disabled = False
            if multi:
                if multi_list.highlighted is None:
                    multi_list.highlighted = 0
                multi_list.focus()
            else:
                # clear() reset index to None; re-arm the highlight so START has a target.
                if single_list.index is None:
                    single_list.index = 0
                single_list.focus()
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

    def _using_multi(self) -> bool:
        return len(self._devices) >= 2

    def _ready_prompt(self) -> str:
        """The 'ready to go' status line: only 2+ cards need a 'select' step, one card is pre-armed."""
        prefix = "Select card(s) and " if self._using_multi() else ""
        return f"[bold $text-success]{prefix}Press START to begin scanning[/]"

    def _start_targets(self) -> list:
        """The DeviceIDs to bring up: the checked rows (2+ cards) or the single present card."""
        if self._using_multi():
            sl = self.query_one("#device-select", SelectionList)
            return [self._devices[i] for i in sorted(sl.selected) if i < len(self._devices)]
        return list(self._devices)

    def _highlighted_device(self):
        """The DeviceID of the cursor row (what Uninstall acts on), or None."""
        if self._using_multi():
            index = self.query_one("#device-select", SelectionList).highlighted
        else:
            index = self.query_one("#device-list", ListView).index
        if index is None or index >= len(self._devices):
            return None
        return self._devices[index]

    def action_enter(self) -> None:
        """Enter dispatch: uninstall the highlighted card when the Uninstall button is focused, else
        start the checked cards. Keeps Enter working from anywhere without stealing it from Uninstall."""
        if self._is_initializing:
            return
        focused = self.app.focused
        if focused is not None and focused.id == "uninstall-btn":
            dev = self._highlighted_device()
            if dev is not None:
                self.perform_uninstall(dev)
            return
        self.action_start()

    def action_start(self) -> None:
        """START: bring up the checked cards. Clicking a row only toggles it (no auto-start)."""
        if self._is_initializing:
            return
        targets = self._start_targets()
        if not targets:
            if self._devices:                 # 2+ cards present but none checked
                self.notify("Select at least one card.", severity="warning")
            return
        self.perform_start(targets)

    def on_click(self, event: events.Click) -> None:
        """Double-click the single card to start it (a third way in, alongside Enter and START). A
        single click only highlights. Multi-card uses checkboxes, so this is single-card only."""
        if event.chain < 2 or self._is_initializing or self._using_multi():
            return
        clicked = event.widget
        single_list = self.query_one("#device-list", ListView)
        if clicked is not None and single_list in clicked.ancestors_with_self:
            self.action_start()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self._is_initializing:
            return
        if event.button.id == "start-btn":
            self.action_start()
        elif event.button.id == "uninstall-btn":
            dev = self._highlighted_device()
            if dev is not None:
                self.perform_uninstall(dev)

    def _enter_busy(self) -> None:
        self._is_initializing = True
        self.app.device_watch.pause()     # freeze the device watch so the list can't churn mid-bring-up
        single_list, multi_list = self._both_lists()
        single_list.disabled = True
        multi_list.disabled = True
        self.query_one("#start-btn", Button).disabled = True
        self.query_one("#uninstall-btn", Button).disabled = True

    def _exit_busy(self) -> None:
        self._is_initializing = False
        self.app.device_watch.resume()
        single_list, multi_list = self._both_lists()
        single_list.disabled = False
        multi_list.disabled = False
        self.query_one("#start-btn", Button).disabled = False
        self.query_one("#uninstall-btn", Button).disabled = False
        (multi_list if self._using_multi() else single_list).focus()

    @work(exclusive=True)
    async def perform_start(self, devices) -> None:
        """Bring up each checked card in turn through the engine; enter the scanner if any came up. The
        engine owns the per-card progress modal, the install/replug dialogs, and the platform branching.
        A per-card failure is a toast; a card whose install the user declines (CANCELLED) is skipped."""
        self._clear_error()
        self._enter_busy()
        pooled = 0
        failures = []
        try:
            for dev in devices:
                res = await self.app.device_manager.bringup(dev)
                if res.status is Status.READY:
                    pooled += 1
                elif res.status is Status.FAILED:
                    failures.append(res.message)
        finally:
            self._exit_busy()

        if pooled > 0:
            if failures:
                self.notify(f"{len(failures)} card(s) failed to start.", severity="warning")
            self.app.switch_screen("scanner")
        elif failures:
            self._show_error(failures[-1])
        else:  # all declined / nothing checked
            self.query_one("#status-label", Label).update(self._ready_prompt())

    @work(exclusive=True)
    async def perform_uninstall(self, device_id) -> None:
        """Reverse wifit3's driver/access for the selected card via the engine."""
        self._clear_error()
        self._enter_busy()
        try:
            res = await self.app.device_manager.uninstall(device_id)
        finally:
            self._exit_busy()

        status = self.query_one("#status-label", Label)
        if res.ok:
            status.update(f"[bold green]{res.message}[/bold green]")
            self.notify(f"[green]✓[/green] {res.message}", title="Uninstalled",
                        severity="information")
        elif res.cancelled:
            status.update(self._ready_prompt())
        else:
            status.update("[bold red]Uninstall failed.[/bold red]")
            self.app.push_screen(SetupErrorDialog("Uninstall failed", res.message, res.detail))
