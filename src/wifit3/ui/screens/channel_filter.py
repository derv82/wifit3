from typing import List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, SelectionList
from textual.widgets.selection_list import Selection


class ChannelFilterDialog(ModalScreen[Optional[List[int]]]):
    """Modal dialog that lets the user pick which channels the hopper visits.

    Dismisses with a sorted ``list[int]`` of selected channels, or ``None`` if
    the user cancelled (in which case the caller should keep the existing
    filter unchanged).
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("a", "select_all", "All", show=True),
        Binding("2", "select_24ghz", "2.4 GHz", show=True),
        Binding("5", "select_5ghz", "5 GHz", show=True),
        Binding("n", "select_none", "None", show=True),
        Binding("enter", "confirm", "OK", show=True, priority=True),
    ]

    DEFAULT_CSS = """
    ChannelFilterDialog {
        align: center middle;
    }
    ChannelFilterDialog #dialog {
        width: 48;
        height: auto;
        max-height: 90%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    ChannelFilterDialog #title {
        content-align: center middle;
        margin-bottom: 1;
        text-style: bold;
    }
    ChannelFilterDialog #hotkeys {
        content-align: center middle;
    }
    ChannelFilterDialog #hint {
        color: $text-muted;
        content-align: center middle;
        margin-bottom: 1;
    }
    /* The list fills whatever height the dialog gives it (1fr), so on a short
       terminal it shrinks and scrolls instead of pushing the buttons off — and
       it never overflows its container. _resize_dialog caps the *dialog* at its
       natural content height, so on a roomy screen the 1fr has no slack to leave
       and the list simply hugs its channels. */
    ChannelFilterDialog SelectionList {
        height: 1fr;
        min-height: 3;
        overflow-y: auto;
        margin-bottom: 1;
    }
    /* Dock the buttons to the dialog's bottom edge so a tall channel list (or a
       short terminal) can never push them out of view — the list scrolls in the
       space that remains above. */
    ChannelFilterDialog #button-row {
        dock: bottom;
        height: auto;
        align: center middle;
    }
    """

    def __init__(
        self,
        supported_channels: List[int],
        current_filter: Optional[List[int]] = None,
    ) -> None:
        super().__init__()
        self._supported: List[int] = sorted(set(supported_channels))
        # Treat a missing filter as "everything is selected"
        initial = set(current_filter) if current_filter else set(self._supported)
        self._initial_selected: set[int] = initial & set(self._supported)

    def compose(self) -> ComposeResult:
        selections = [
            Selection(
                self._format_label(ch),
                ch,
                initial_state=(ch in self._initial_selected),
            )
            for ch in self._supported
        ]

        with Vertical(id="dialog"):
            yield Label("Channel Filter", id="title")
            yield Label(
                r"\[[bold green]a[/]]ll  \[[bold cyan]2[/]]G  \[[bold cyan]5[/]]G  "
                r"\[[bold red]n[/]]one",
                id="hotkeys",
            )
            yield Label(
                "[cyan]Space:[/cyan]toggle · [cyan]Enter:[/cyan]confirm · [cyan]Esc:[/cyan]cancel",
                id="hint",
            )
            yield SelectionList[int](*selections, id="channel-list")
            with Horizontal(id="button-row"):
                yield Button("OK", variant="primary", id="btn-ok")
                yield Button("Cancel", variant="default", id="btn-cancel")

    # Rows the dialog spends on everything *except* the channel list's own box:
    # thick border (2) + vertical padding (2) + title+margin (2) + hotkeys (1)
    # + hint+margin (2) + list margin-bottom (1) + button row (3). Keep in sync
    # with DEFAULT_CSS. The list's own box adds one row per channel + a 2-row
    # border, so the dialog's natural height is len(channels) + 2 + _CHROME_ROWS.
    _CHROME_ROWS = 13

    def on_mount(self) -> None:
        self._resize_dialog()
        self.query_one("#channel-list", SelectionList).focus()

    def on_resize(self) -> None:
        self._resize_dialog()

    def _resize_dialog(self) -> None:
        """Cap the dialog at its natural content height, but never past ~90% of
        the screen.

        Capping at the natural height leaves the 1fr list no slack, so the dialog
        hugs its channels (no dead space below the list). When the channels can't
        all fit in ~90% of the screen, the ceiling wins and the list scrolls.
        """
        natural = len(self._supported) + 2 + self._CHROME_ROWS
        ceiling = int(self.app.size.height * 0.9)
        self.query_one("#dialog", Vertical).styles.max_height = min(natural, ceiling)

    @staticmethod
    def _format_label(channel: int) -> str:
        band = "2.4 GHz" if channel <= 14 else "5 GHz"
        return f"CH {channel:>3}   ({band})"

    # --- Quick-filter actions -------------------------------------------------

    def _selection_list(self) -> SelectionList:
        return self.query_one("#channel-list", SelectionList)

    def action_select_all(self) -> None:
        self._selection_list().select_all()

    def action_select_none(self) -> None:
        self._selection_list().deselect_all()

    def action_select_24ghz(self) -> None:
        sl = self._selection_list()
        sl.deselect_all()
        for ch in self._supported:
            if ch <= 14:
                sl.select(ch)

    def action_select_5ghz(self) -> None:
        sl = self._selection_list()
        sl.deselect_all()
        for ch in self._supported:
            if ch > 14:
                sl.select(ch)

    # --- Confirm / cancel -----------------------------------------------------

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_confirm(self) -> None:
        selected = sorted(self._selection_list().selected)
        # Empty selection is treated as cancel — hopping with zero channels
        # would silently stall the scanner.
        self.dismiss(selected if selected else None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-ok":
            self.action_confirm()
        else:
            self.action_cancel()
