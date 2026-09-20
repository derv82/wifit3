from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Footer, DataTable
from textual.screen import ModalScreen
from textual import events, on
from textual.binding import Binding

from .vault_table import VaultTable
from .vault_item import VaultItemView
from wifit3.ui.vault.job_pane import JobTrackerPane

class VaultDrawer(ModalScreen):
    """The Vault drawer overlay."""

    BINDINGS = [
        Binding("v", "dismiss_drawer", "Close", show=False),
        Binding("escape", "dismiss_drawer", "Close", show=True),
        Binding("z", "export_zip", "Export Zip", show=True),
        Binding("o", "show_directory", "Show Dir", show=True),
        Binding("q", "app.quit", "Quit", show=True),
    ]

    CSS = """
    VaultDrawer {
        align: center bottom;
        background: rgba(0, 0, 0, 0.4);
    }
    #vault-content {
        width: 100%;
        max-height: 75%;
        background: $surface;
        offset-y: 100%;
        transition: offset 200ms in_out_cubic;
    }
    VaultDrawer.open #vault-content {
        offset-y: 0%;
    }
    """

    def compose(self) -> ComposeResult:
        with Container(id="vault-content"):
            with Horizontal():
                yield VaultTable(id="vault-table")
                yield VaultItemView(id="vault-item")
            yield JobTrackerPane()
            yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(lambda: self.add_class("open"))

    def action_dismiss_drawer(self) -> None:
        if self.has_class("open"):
            self.remove_class("open")
            def _do_dismiss():
                self.dismiss()
            self.set_timer(0.35, _do_dismiss)

    @on(events.Click)
    def on_click(self, event: events.Click) -> None:
        # Dismiss if clicking outside the #vault-content container
        # Since ModalScreen catches clicks outside its children, `event.control == self` means background.
        if event.control == self:
            self.action_dismiss_drawer()

    def _load_widget(self, bssid: str) -> None:
        widget = self.query_one("#vault-item", VaultItemView)
        table = self.query_one("#vault-table", VaultTable)
        entry = table._aps.get(bssid)
        if entry is not None:
            widget.load(bssid, entry[0], entry[1])
        else:
            widget.load("", None, [])

    @on(VaultTable.TableReloaded)
    def _table_reloaded(self, event: VaultTable.TableReloaded) -> None:
        table = self.query_one("#vault-aps", DataTable)
        if table.row_count > 0:
            try:
                key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
                self._load_widget(key)
            except Exception:
                from textual.coordinate import Coordinate
                key = table.coordinate_to_cell_key(Coordinate(0, 0)).row_key.value
                self._load_widget(key)
        else:
            self._load_widget("")

    @on(DataTable.RowHighlighted, "#vault-aps")
    def _ap_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self._load_widget(event.row_key.value)

    @on(VaultItemView.CapturesChanged)
    def _captures_changed(self) -> None:
        self.query_one("#vault-table", VaultTable).reload_table()

    def action_export_zip(self) -> None:
        try:
            out = self.app.vault.zip_captures(self.app.vault.all_captures())
        except OSError as exc:
            self.notify(f"Export failed: {exc}", severity="error")
            return
        if out is None:
            self.notify("Nothing to export", severity="warning")
            return
        self.notify(f"Exported to {out}")

    def action_show_directory(self) -> None:
        try:
            self.app.vault.open_directory()
        except OSError as exc:
            self.notify(f"Could not open captures dir: {exc}", severity="error")
