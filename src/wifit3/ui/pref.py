"""Ctrl+P preferences modal."""
from rich.padding import Padding
from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Horizontal, Vertical, VerticalGroup
from textual.events import Event
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Checkbox, Input, Label, Select

from wifit3.persist.config import Config
from wifit3.persist.repair import RepairResult, repair_vault
from wifit3.ui.path_picker import PathInput
from wifit3.ui.screens.vault_item import ConfirmModal


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


class VaultMaintenanceSetting(VerticalGroup):
    DEFAULT_CSS = """
    VaultMaintenanceSetting { border: round $primary; height: auto; padding: 0 1; }
    VaultMaintenanceSetting Button { width: 100%; }
    """

    def compose(self) -> ComposeResult:
        self.border_title = "Vault maintenance"
        yield Button("Repair Vault data", id="repair-vault")


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
            yield Checkbox("Auto-crack captures using CPU", value=Config.auto_crack,
                           id="auto_crack")
            yield VaultMaintenanceSetting()
            yield SaveFooter()

    def on_mount(self) -> None:
        self._original_theme = self.app.theme
        self._original_sort_delay = Config.scanner_sort_delay
        self._conversion_offered = False
        self.call_after_refresh(self._offer_legacy_conversion)

    def _offer_legacy_conversion(self) -> None:
        missing = self.app.vault.missing_metadata_count()
        if missing == 0 or self._conversion_offered:
            return
        self._conversion_offered = True
        self.app.push_screen(
            ConfirmModal(self._conversion_warning(missing)),
            self._repair_confirmed,
        )

    @staticmethod
    def _conversion_warning(missing: int | None = None) -> str:
        prefix = (
            f"Found [bold]{missing}[/] saved APs with missing v2 metadata. "
            if missing is not None else "Repair existing Vault data? "
        )
        return (
            prefix
            + "Generate v2 metadata and rebuild derived HC22000 and safe SSID filenames from "
            "existing HC22000 and pcap data?\n\n[bold]Original HC22000 and pcap sources will "
            "be preserved in a v1 backup directory. Saved passwords, WPS PINs, WEP keys, and "
            "credential files will not be changed.[/]"
        )

    @on(Button.Pressed, "#repair-vault")
    def repair_vault_pressed(self, event: Event) -> None:
        event.stop()
        self.app.push_screen(
            ConfirmModal(self._conversion_warning()),
            self._repair_confirmed,
        )

    def _repair_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        self.query_one("#repair-vault", Button).disabled = True
        self.notify("Repairing Vault data…")
        self._run_vault_repair()

    @work(thread=True, exclusive=True)
    def _run_vault_repair(self) -> None:
        result = repair_vault()
        self.app.call_from_thread(self._repair_finished, result)

    def _repair_finished(self, result: RepairResult) -> None:
        self.query_one("#repair-vault", Button).disabled = False
        self.app.vault.refresh()
        message = (
            f"Repaired {result.aps} APs, {result.hash_lines} hash lines; "
            f"read {result.pcaps_read} pcaps and renamed {result.files_renamed} files"
        )
        if result.backup_dir is not None:
            message += f". Originals: {result.backup_dir.name}"
        if result.errors:
            message += f". First error: {result.errors[0]}"
            self.notify(message, severity="warning", title=f"{len(result.errors)} repair errors")
        else:
            self.notify(message, title="Vault repair complete")

    @on(Button.Pressed, "#save")
    def save_pressed(self, event: Event):
        Config.theme = self.app.theme
        Config.captures_dir = self.query_one("#captures_dir", Input).value
        self.app.vault.refresh()
        Config.save_pcap = self.query_one("#save_pcap", Checkbox).value
        Config.auto_crack = self.query_one("#auto_crack", Checkbox).value
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

