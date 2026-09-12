"""Ctrl+P preferences modal."""
from rich.padding import Padding
from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Horizontal, Vertical, VerticalGroup
from textual.events import Event
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Checkbox, Input, Label, Select

from pathlib import Path

from wifit3.persist.common import LEGACY_CAPTURE_RE, bssid_to_dashed, safe_ssid
from wifit3.persist.config import Config
from wifit3.persist.save import consolidate_hc_files


class ThemeSetting(VerticalGroup):
    DEFAULT_CSS = """
    ThemeSetting { border: round $primary }
    """
    def compose(self) -> ComposeResult:
        self.border_title = "Theme"
        yield Select(self._theme_options(), id="theme",
                     value=self.app.theme, allow_blank=False)

    @on(Select.Changed, "#theme")
    def select_theme(self, event: Select.Changed) -> None:
        self.app.theme = event.value

    def _theme_options(self) -> list[tuple[Text, str]]:
        options = []
        themes = sorted(self.app.available_themes.items(), key=self._sort_key)
        for name, theme in themes:
            fg = Color.parse(theme.primary).rich_color if theme.primary else None
            bg = Color.parse(theme.background).rich_color if theme.background else None
            styled_fg = Text(name, style=Style(color=fg, bold=True))
            styled_option = Padding(styled_fg, 0, style=Style(bgcolor=bg))
            options.append((styled_option, name))
        return options

    def _sort_key(self, key_value: tuple[str, Theme]):
        name, theme = key_value
        if 'wifit3' in name:
            return '0' + name
        return '1' + name if theme.dark else '2' + name


class SortDelaySetting(VerticalGroup):
    DEFAULT_CSS = """
    SortDelaySetting { border: round $primary }
    """

    OPTIONS: list[tuple[str, float]] = [
        ("Instant", 0.0),
        ("0.25 seconds", 0.25),
        ("1 second", 1.0),
        ("2 seconds", 2.0),
        ("3 seconds", 3.0),
        ("5 seconds", 5.0),
        ("Never", -1.0),
    ]

    def compose(self) -> ComposeResult:
        self.border_title = "Sort Delay"
        current = Config.scanner_sort_delay
        values = [val for _, val in self.OPTIONS]
        value = current if current in values else 2.0
        yield Select(self.OPTIONS, id="sort_delay", value=value, allow_blank=False)

    @on(Select.Changed, "#sort_delay")
    def select_sort_delay(self, event: Select.Changed) -> None:
        if event.value is not None:
            Config.scanner_sort_delay = float(event.value)


class CapturesDirSetting(VerticalGroup):
    DEFAULT_CSS = """
    CapturesDirSetting { border: round $primary }
    """
    def compose(self) -> ComposeResult:
        self.border_title = "Save directory"
        yield Input(Config.captures_dir, id="captures_dir")


class SaveFooter(Horizontal):
    DEFAULT_CSS = """
    SaveFooter {
        height: auto; margin: 0;
        align: right middle;
        background: transparent; }
    SaveFooter Button { height: auto }
    """

    def compose(self) -> ComposeResult:
        yield Button(Text("Save"), "primary", id="save")
        yield Button(Text("Cancel"), "default", id="cancel")

    def cancel_pressed(self, event: Event):
        self.app.pop_screen()


class ConsolidateModal(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    ConsolidateModal { align: center middle; }
    ConsolidateModal #dialog {
        width: 54; height: auto;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    ConsolidateModal #prompt { margin-bottom: 1; }
    ConsolidateModal Horizontal { align: right middle; height: auto; }
    ConsolidateModal Button { margin-left: 1; }
    """

    def __init__(self, legacy_count: int, target_count: int) -> None:
        super().__init__()
        self.legacy_count = legacy_count
        self.target_count = target_count

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("[bold]Consolidate .hc22000 Captures[/]", id="title")
            dupes = self.legacy_count - self.target_count
            msg = Text.from_markup(
                f"\nYou have [bold orange1]{self.legacy_count}[/] separate .hc22000 files from [bold green]{self.target_count}[/] APs.\n\n"
                f"Do you want to condense these into single files per AP?\n\n"
                f"    [bold green]{self.target_count} files will be created/updated[/]\n"
                f"    [bold red]{dupes} duplicate files will be removed[/]"
            )
            yield Label(msg, id="prompt")
            with Horizontal():
                yield Button(Text("Yes"), "primary", id="confirm")
                yield Button(Text("No"), "default", id="cancel")

    @on(Button.Pressed, "#confirm")
    def on_confirm(self, event: Event) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def on_cancel(self, event: Event) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class LegacyCapturesSetting(VerticalGroup):
    DEFAULT_CSS = """
    LegacyCapturesSetting {
        height: auto; margin-top: 0; margin-bottom: 0;
        align: center middle;
    }
    LegacyCapturesSetting Button {
        margin-top: 0; width: 100%;
    }
    """

    def compose(self) -> ComposeResult:
        captures_path = Path(Config.captures_dir)
        legacy_files = [
            p for p in captures_path.iterdir()
            if p.is_file() and (m := LEGACY_CAPTURE_RE.match(p.name)) and m.group("ext") == "hc22000"
        ] if captures_path.is_dir() else []
        if legacy_files:
            yield Label(f"[dim]{len(legacy_files)} legacy split file(s) found[/dim]", id="legacy_label")
            btn = Button(Text("Consolidate Captures"), "warning", id="consolidate")
            btn.tooltip = "Merge separate timestamped .hc22000 files into 1 file per AP"
            yield btn

    @on(Button.Pressed, "#consolidate")
    def consolidate_pressed(self, event: Event) -> None:
        captures_path = Path(Config.captures_dir)
        legacy_files = [
            p for p in captures_path.iterdir()
            if p.is_file() and (m := LEGACY_CAPTURE_RE.match(p.name)) and m.group("ext") == "hc22000"
        ] if captures_path.is_dir() else []
        if not legacy_files:
            return

        targets = {
            f"{safe_ssid(m.group('ssid'))}_{bssid_to_dashed(m.group('bssid'))}"
            for p in legacy_files if (m := LEGACY_CAPTURE_RE.match(p.name))
        }

        def after_confirm(confirmed: bool | None) -> None:
            if confirmed:
                migrated, deleted = consolidate_hc_files(captures_path)
                self.notify(f"Consolidated {deleted} files into {migrated} AP files.", title="Captures Consolidated")
                self.remove_children()
                self.mount(Label("[bold green]Captures consolidated[/]", id="legacy_done"))

        self.app.push_screen(ConsolidateModal(len(legacy_files), len(targets)), after_confirm)


class PreferencesModal(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    PreferencesModal { align: center middle; }
    PreferencesModal #dialog {
        width: 44; height: auto;
        max-height: 100%;
        border: thick $primary; background: $surface; padding: 0 2;
    }
    PreferencesModal #dialog > * { width: 100% }
    PreferencesModal #title {
        text-style: bold; text-align: center;
        margin: 0;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Preferences", id="title")
            yield ThemeSetting()
            yield SortDelaySetting()
            yield CapturesDirSetting()
            yield Checkbox("Save .pcap handshakes", value=Config.save_pcap, id="save_pcap")
            yield LegacyCapturesSetting()
            yield SaveFooter()

    def on_mount(self) -> None:
        self._original_theme = self.app.theme
        self._original_sort_delay = Config.scanner_sort_delay

    @on(Button.Pressed, "#save")
    def save_pressed(self, event: Event):
        Config.theme = self.app.theme
        Config.captures_dir = self.query_one("#captures_dir", Input).value
        Config.save_pcap = self.query_one("#save_pcap", Checkbox).value
        Config.scanner_sort_delay = float(self.query_one("#sort_delay", Select).value)
        self._save_and_dismiss()

    def _save_and_dismiss(self) -> None:
        try:
            Config.save()
        except Exception as e:
            self.notify(str(e), title="Config Error")
        self.dismiss()

    @on(Button.Pressed, "#cancel")
    def cancel_pressed(self, event: Event):
        self.action_cancel()

    def action_cancel(self) -> None:
        self.app.theme = self._original_theme
        Config.scanner_sort_delay = self._original_sort_delay
        self.dismiss()

