"""Ctrl+P preferences modal."""
import logging

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

from wifit3.hashtopolis.client import HashtopolisClient, HashtopolisError, normalize_url
from wifit3.persist.config import Config
from wifit3.ui.path_picker import PathInput

logger = logging.getLogger(__name__)


def _sanitized_url(raw: str) -> str | None:
    """A valid URL stored without embedded credentials or query; a non-empty invalid entry is
    kept verbatim so the user can see and correct it."""
    if not raw:
        return None
    try:
        return normalize_url(raw)
    except HashtopolisError:
        return raw


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


class HashtopolisSetting(VerticalGroup):
    DEFAULT_CSS = """
    HashtopolisSetting { border: round $primary; height: auto; }
    HashtopolisSetting Label { margin-top: 1; }
    HashtopolisSetting #ht_test { margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        self.border_title = "Hashtopolis"
        yield Label("Server URL")
        yield Input(Config.hashtopolis_url or "", placeholder="http://host:8080", id="ht_url")
        yield Label("API token")
        yield Input(Config.hashtopolis_token or "", password=True, id="ht_token")
        yield Label("Access group id (optional)")
        yield Input("" if Config.hashtopolis_access_group_id is None
                    else str(Config.hashtopolis_access_group_id),
                    placeholder="auto when only one", id="ht_access_group")
        yield Checkbox("Auto-submit new WPA captures",
                       value=Config.hashtopolis_auto_submit, id="ht_auto_submit")
        yield Checkbox("Only trusted agents (secret hashlists)",
                       value=Config.hashtopolis_trusted_agents_only, id="ht_trusted")
        yield Button("Test connection", id="ht_test")

    @on(Button.Pressed, "#ht_test")
    def _test(self, event: Event) -> None:
        event.stop()
        try:
            client = self._build_client()
        except HashtopolisError as exc:
            self.notify(f"Hashtopolis: {exc}", title="Hashtopolis", severity="error")
            return
        self.app.run_worker(lambda: self._run_test(client), thread=True,
                            exclusive=True, group="ht-test")

    def _run_test(self, client: HashtopolisClient) -> None:
        try:
            info = client.test_connection()
            msg = f"OK: connected, access group {info['access_group_id']}"
            severity = "information"
        except HashtopolisError as exc:
            msg = str(exc)
            severity = "error"
            try:
                groups = client.list_access_groups()
                if len(groups) > 1:
                    msg += " | groups: " + ", ".join(f"{g.id}={g.name}" for g in groups)
            except HashtopolisError:
                pass
        except Exception:
            logger.exception("Hashtopolis connection test failed unexpectedly")
            msg = "Unexpected error during connection test (see logs)"
            severity = "error"
        self.app.call_from_thread(self.app.notify, msg, title="Hashtopolis", severity=severity)

    def _build_client(self) -> HashtopolisClient:
        group_raw = self.query_one("#ht_access_group", Input).value.strip()
        return HashtopolisClient(
            self.query_one("#ht_url", Input).value.strip(),
            token=self.query_one("#ht_token", Input).value.strip(),
            access_group_id=int(group_raw) if group_raw.isdigit() else None,
            is_secret=self.query_one("#ht_trusted", Checkbox).value,
        )

    def apply_to_config(self) -> None:
        group_raw = self.query_one("#ht_access_group", Input).value.strip()
        Config.hashtopolis_url = _sanitized_url(self.query_one("#ht_url", Input).value.strip())
        Config.hashtopolis_token = self.query_one("#ht_token", Input).value.strip() or None
        Config.hashtopolis_access_group_id = int(group_raw) if group_raw.isdigit() else None
        Config.hashtopolis_auto_submit = self.query_one("#ht_auto_submit", Checkbox).value
        Config.hashtopolis_trusted_agents_only = self.query_one("#ht_trusted", Checkbox).value


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
        overflow-y: auto;
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
            yield HashtopolisSetting()
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
        Config.scanner_sort_delay = float(self.query_one("#sort_delay", Select).value)
        self.query_one(HashtopolisSetting).apply_to_config()
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

