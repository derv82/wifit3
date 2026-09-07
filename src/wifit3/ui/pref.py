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

from wifit3.persist.config import Config


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


class PreferencesModal(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    PreferencesModal { align: center middle; }
    PreferencesModal #dialog {
        width: 40; height: auto;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    PreferencesModal #dialog > * { width: 100% }
    PreferencesModal #title {
        text-style: bold; text-align: center;
        margin-bottom: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Preferences", id="title")
            yield ThemeSetting()
            yield CapturesDirSetting()
            yield Checkbox("Save .pcap handshakes", value=Config.save_pcap, id="save_pcap")
            yield Checkbox("Save .hc22000 files", value=True, disabled=True)
            yield SaveFooter()

    def on_mount(self) -> None:
        self._original_theme = self.app.theme

    @on(Button.Pressed, "#save")
    def save_pressed(self, event: Event):
        Config.theme = self.app.theme
        Config.captures_dir = self.query_one("#captures_dir", Input).value
        Config.save_pcap = self.query_one("#save_pcap", Checkbox).value
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
        self.dismiss()
