from pathlib import Path
from typing import Any, ClassVar, Optional

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label

from wifit3.persist.config import Config, ConfigError, CpuCrackSettings, HashcatSettings
from wifit3.ui.path_picker import BROWSE_GLYPH, PathPickerModal


class _RemoveButton(Button):
    """The `X` beside one chosen wordlist; ``path`` is the wordlist it removes."""

    def __init__(self, path: str) -> None:
        super().__init__("X", variant="error", compact=True, classes="wordlist-remove")
        self.path = path


class WordlistConfigModal(ModalScreen[dict]):
    """Launch modal for a tool that cracks with the user's chosen wordlists; dismisses with the
    tool's ``job.config``, or None when cancelled."""

    dialog_title: ClassVar[str]

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, confirm_label: str = "Launch") -> None:
        """``confirm_label`` "Save" configures the tool without the caller launching a job."""
        super().__init__()
        self._confirm_label = confirm_label
        self._wordlists: list[str] = list(self._settings().wordlists)

    # CSS, not DEFAULT_CSS: the app's `Button { min-width: 12 }` outranks any DEFAULT_CSS rule.
    # No type prefix: each subclass loads this scoped to its own name.
    CSS = """
    #wordlist-clear, .wordlist-remove { min-width: 0; }
    """

    DEFAULT_CSS = """
    WordlistConfigModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.5);
    }
    #wordlist-dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: solid $primary;
    }
    #wordlist-title {
        width: 1fr;
        text-align: center;
        text-style: bold;
        color: $accent;
    }
    #wordlist-choose-row {
        height: auto;
    }
    #wordlist-choose {
        width: 1fr;
    }
    #wordlist-clear {
        width: 7;
    }
    .wordlist-remove {
        width: 3;
    }
    #wordlists {
        height: auto;
        max-height: 10;
    }
    .wordlist-row {
        height: 1;
    }
    .wordlist-name {
        width: 1fr;
        margin-left: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    #wordlist-buttons {
        margin-top: 2;
        align: right middle;
        height: auto;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="wordlist-dialog"):
            yield Label(self.dialog_title, id="wordlist-title")
            yield from self._compose_fields()
            with Horizontal(id="wordlist-buttons"):
                yield Button("Cancel", variant="error", id="wordlist-cancel")
                yield Button(self._confirm_label, variant="success", id="wordlist-launch")

    def _compose_fields(self) -> ComposeResult:
        yield from self._compose_wordlists()

    def _compose_wordlists(self) -> ComposeResult:
        with Horizontal(id="wordlist-choose-row"):
            yield Button(f"{BROWSE_GLYPH} Choose wordlists", "primary", id="wordlist-choose")
            yield Button("Clear", variant="error", id="wordlist-clear")
        yield VerticalScroll(id="wordlists")

    async def on_mount(self) -> None:
        await self._show_wordlists()

    async def _show_wordlists(self) -> None:
        self.query_one("#wordlist-clear", Button).display = bool(self._wordlists)
        rows = self.query_one("#wordlists", VerticalScroll)
        await rows.remove_children()
        if not self._wordlists:
            await rows.mount(Label("[dim]none chosen[/dim]"))
            return
        await rows.mount_all(
            Horizontal(_RemoveButton(path),
                       Label(f"[bold]{number}:[/bold] {escape(Path(path).name)}  "
                             f"[dim]{escape(str(Path(path).parent))}[/dim]",
                             classes="wordlist-name"),
                       classes="wordlist-row")
            for number, path in enumerate(self._wordlists, 1))

    @on(Button.Pressed, ".wordlist-remove")
    async def _remove_wordlist(self, event: Button.Pressed) -> None:
        event.stop()
        assert isinstance(event.button, _RemoveButton)
        self._wordlists.remove(event.button.path)
        await self._show_wordlists()

    @on(Button.Pressed, "#wordlist-clear")
    async def _clear_wordlists(self, event: Button.Pressed) -> None:
        event.stop()
        self._wordlists = []
        await self._show_wordlists()

    @on(Button.Pressed, "#wordlist-choose")
    def _choose_wordlists(self, event: Button.Pressed) -> None:
        event.stop()
        start = self._wordlists[0] if self._wordlists else str(Path.home())
        self.app.push_screen(
            PathPickerModal(start, title="Select wordlist(s)", multiple=True,
                            checked=[Path(path) for path in self._wordlists]),
            self._wordlists_chosen,
        )

    async def _wordlists_chosen(self, paths: Optional[list[Path]]) -> None:
        if paths is None:
            return
        self._wordlists = [str(path) for path in paths]
        await self._show_wordlists()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#wordlist-cancel")
    def _cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()

    @on(Button.Pressed, "#wordlist-launch")
    def _launch(self, event: Button.Pressed) -> None:
        event.stop()
        config = self._launch_config()
        if config is None:
            return
        self._remember(config)
        self.dismiss(config)

    def _launch_config(self) -> Optional[dict[str, Any]]:
        if not self._wordlists:
            self.notify("Please choose at least one wordlist", severity="error")
            return None
        return {"wordlists": list(self._wordlists)}

    def _remember(self, config: dict[str, Any]) -> None:
        """Persist the launch settings so the next job starts pre-filled."""
        self._store_settings(config)
        try:
            Config.save()
        except ConfigError as e:
            self.notify(str(e), title="Config Error", severity="warning")

    def _settings(self) -> CpuCrackSettings | HashcatSettings:
        """The Config table this tool's wordlists are saved in."""
        raise NotImplementedError

    def _store_settings(self, config: dict[str, Any]) -> None:
        self._settings().wordlists = list(config["wordlists"])
