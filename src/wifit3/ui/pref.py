"""Ctrl+P preferences modal."""
from typing import Callable

from rich.padding import Padding
from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Horizontal, VerticalGroup, VerticalScroll
from textual.events import Event
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Checkbox, Input, Label, Select

from wifit3.models import ToolName
from wifit3.persist.config import Config
from wifit3.ui.path_picker import PathInput
from wifit3.ui.vault.tools_ui import UI_TOOLS
from wifit3.vault.tools.base import VaultTool


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
        yield PathInput(Config.captures_dir, directories_only=True, prefer_relative=True,
                        title="Save directory", id="captures_dir")


class AutoCrackSetting(VerticalGroup):
    DEFAULT_CSS = """
    AutoCrackSetting { border: round $primary }
    AutoCrackSetting Horizontal { height: auto }
    AutoCrackSetting Select { width: 1fr }
    """

    def compose(self) -> ComposeResult:
        self.border_title = "Auto-crack new captures with"
        self._chosen = Config.auto_crack or ""
        tools = sorted(self.app.vault.manager.tools.values(), key=lambda t: t.label)
        options = [("Off", "")] + [(tool.label, tool.name) for tool in tools]
        with Horizontal():
            yield Select(options, id="auto_crack", value=self._chosen, allow_blank=False)
            yield Button("≡", id="auto_crack_configure")

    def on_mount(self) -> None:
        self.query_one("#auto_crack_configure", Button).display = bool(self._chosen)

    @on(Select.Changed, "#auto_crack")
    def select_tool(self, event: Select.Changed) -> None:
        if event.value == self._chosen:
            return
        previous = self._chosen
        self._choose(str(event.value))
        tool = self.app.vault.manager.tools.get(self._chosen)
        if tool is not None and tool.auto_crack_config() is None:
            self._configure(lambda _: self._revert_if_unconfigured(tool, previous))

    @on(Button.Pressed, "#auto_crack_configure")
    def configure_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._configure()

    def _choose(self, tool_name: str) -> None:
        self._chosen = tool_name
        self.query_one("#auto_crack", Select).value = tool_name
        self.query_one("#auto_crack_configure", Button).display = bool(tool_name)

    def _revert_if_unconfigured(self, tool: VaultTool, previous: str) -> None:
        if tool.auto_crack_config() is None:
            self._choose(previous)

    def _configure(self, on_dismiss: Callable[[dict | None], None] | None = None) -> None:
        modal = UI_TOOLS.get(ToolName(self._chosen)) if self._chosen else None
        if modal is None:
            return
        self.app.push_screen(modal(confirm_label="Save",
                                   probed_ssids=self.app.vault.probed_ssids()), on_dismiss)

    @property
    def tool(self) -> ToolName | None:
        return ToolName(self._chosen) if self._chosen else None


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


class PreferencesModal(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    PreferencesModal { align: center middle; }
    PreferencesModal #dialog {
        width: 44; height: auto;
        max-height: 100%;
        border: thick $primary; background: $surface; padding: 0 2;
        overflow-y: auto;
    }
    PreferencesModal #dialog > * { width: 100% }
    PreferencesModal #title {
        text-style: bold; text-align: center;
        margin: 0;
    }
    """

    # CSS, not DEFAULT_CSS: the app's `Button { min-width: 12 }` outranks any DEFAULT_CSS rule.
    CSS = """
    #auto_crack_configure { min-width: 0; width: 5; margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label("Preferences", id="title")
            yield ThemeSetting()
            yield SortDelaySetting()
            yield CapturesDirSetting()
            yield Checkbox("Save .pcap handshakes", value=Config.save_pcap, id="save_pcap")
            yield AutoCrackSetting()
            yield SaveFooter()

    def on_mount(self) -> None:
        self._original_theme = self.app.theme
        self._original_sort_delay = Config.scanner_sort_delay

    @on(Button.Pressed, "#save")
    def save_pressed(self, event: Event):
        Config.theme = self.app.theme
        Config.captures_dir = self.query_one("#captures_dir", Input).value
        self.app.vault.refresh()
        Config.save_pcap = self.query_one("#save_pcap", Checkbox).value
        Config.auto_crack = self.query_one(AutoCrackSetting).tool
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

