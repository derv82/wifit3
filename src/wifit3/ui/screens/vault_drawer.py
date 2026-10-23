from typing import Optional

from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Footer, DataTable, Input
from textual.screen import ModalScreen
from textual import events, on, work
from textual.binding import Binding

from .vault_table import VaultTable
from .vault_item import VaultItemView
from wifit3.models import AccessPoint, CaptureType
from wifit3.persist.meta import meta_path, read_meta, update_meta
from wifit3.ui.vault.job_pane import JobTrackerPane
from wifit3.ui.vault.modals.add_credential import AddCredentialModal, ManualCredential
from wifit3.ui.vault.modals.nickname import NicknameModal

class VaultDrawer(ModalScreen):
    """The Vault drawer overlay."""

    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (90, "-compact"), (105, "-wide")]
    VERTICAL_BREAKPOINTS = [(0, "-short"), (36, "-tall")]

    BINDINGS = [
        Binding("v", "dismiss_drawer", "Close", show=False),
        Binding("escape", "dismiss_drawer", "Close", show=True),
        Binding("f", "focus_search", "Search", show=True),
        Binding("/", "focus_search", "Search", show=False),
        Binding("a", "add_credential", "Add", show=True),
        Binding("n", "set_nickname", "Nickname", show=True),
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

    VaultDrawer.-narrow #vault-main, VaultDrawer.-compact #vault-main {
        layout: vertical;
    }
    VaultDrawer.-narrow #vault-table, VaultDrawer.-compact #vault-table,
    VaultDrawer.-narrow #vault-item, VaultDrawer.-compact #vault-item {
        width: 100%;
        height: 1fr;
    }
    VaultDrawer.-short #vault-content {
        height: 100%;
        max-height: 100%;
    }
    VaultDrawer.open #vault-content {
        offset-y: 0%;
    }
    """

    def __init__(self, preselected_bssid: Optional[str] = None) -> None:
        super().__init__()
        self._preselected_bssid = preselected_bssid

    def compose(self) -> ComposeResult:
        with Container(id="vault-content"):
            with Horizontal(id="vault-main"):
                yield VaultTable(id="vault-table")
                yield VaultItemView(id="vault-item")
            yield JobTrackerPane()
            yield Footer()

    def on_mount(self) -> None:
        narrow = self.size.width < 90
        compact = 90 <= self.size.width < 105
        self.set_class(narrow, "-narrow")
        self.set_class(compact, "-compact")
        self.set_class(not narrow and not compact, "-wide")
        short = self.size.height < 36
        self.set_class(short, "-short")
        self.set_class(not short, "-tall")
        self.call_after_refresh(lambda: self.add_class("open"))
        self.call_after_refresh(self._focus_list)

    def _focus_list(self) -> None:
        """Land focus on the AP list so up/down works without a Tab press."""
        try:
            self.query_one("#vault-aps", DataTable).focus()
        except Exception:
            pass

    def action_focus_search(self) -> None:
        self.query_one("#vault-search", Input).focus()

    def action_dismiss_drawer(self) -> None:
        try:
            self.query_one("#vault-item", VaultItemView).cancel_verification()
        except Exception:
            pass
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
            widget.load(bssid, table.display_name(bssid, entry[0]), entry[1])
        else:
            widget.load("", None, [])

    @on(VaultTable.TableReloaded)
    def _table_reloaded(self, event: VaultTable.TableReloaded) -> None:
        table = self.query_one("#vault-aps", DataTable)
        if table.row_count > 0:
            if self._preselected_bssid in table.rows:
                table.move_cursor(row=table.get_row_index(self._preselected_bssid))
            self._preselected_bssid = None   # the opening screen's AP, honored once
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

    def _selected_network(self) -> tuple[Optional[str], str]:
        """(SSID, BSSID) of the row under the cursor; BSSID is "" when there is none."""
        aps = self.query_one("#vault-aps", DataTable)
        if aps.row_count == 0:
            return None, ""
        bssid = aps.coordinate_to_cell_key(aps.cursor_coordinate).row_key.value or ""
        entry = self.query_one("#vault-table", VaultTable)._aps.get(bssid)
        return (entry[0] if entry is not None else None), bssid

    def action_set_nickname(self) -> None:
        ssid, bssid = self._selected_network()
        if not bssid:
            self.notify("Select an AP first", severity="warning")
            return
        nickname = read_meta(meta_path(ssid, bssid)).get("nickname")
        current = nickname if isinstance(nickname, str) else ""

        def _save(value: Optional[str]) -> None:
            if value is None:
                return
            try:
                update_meta(ssid, bssid, nickname=value or None)
            except OSError as exc:
                self.notify(f"Could not save nickname: {exc}", severity="error")
                return
            self.query_one("#vault-table", VaultTable).reload_nicknames()
            self.notify("Nickname saved" if value else "Nickname cleared")

        self.app.push_screen(NicknameModal(ssid, current), _save)

    def action_add_credential(self) -> None:
        ssid, bssid = self._selected_network()
        self.app.push_screen(AddCredentialModal(ssid or "", bssid), self._credential_added)

    def _credential_added(self, credential: Optional[ManualCredential]) -> None:
        if credential is None:
            return
        vault = self.app.vault
        ap = AccessPoint(bssid=credential.bssid, ssid=credential.ssid)
        verified = False
        try:
            if credential.kind == CaptureType.WPA_PSK:
                verification = vault.verify_wpa_psk(ap.bssid, credential.value)
                if verification is False:
                    self.notify("PSK does not match the saved handshake", severity="error")
                    return
                verified = verification is True
                result = vault.save_wpa_psk(ap.ssid, ap.bssid, credential.value)
                if result is not None:
                    self._propagate_psk(ap.bssid, ap.ssid, credential.value)
            elif credential.kind == CaptureType.WPS_PIN:
                result = vault.save_wps_pin(ap, credential.value, None)
            else:
                value = credential.value
                key = bytes.fromhex(value) if len(value) in (10, 26) else value.encode("ascii")
                result = vault.save_wep_key(ap, key)
        except OSError as exc:
            self.notify(f"Could not save credential: {exc}", severity="error")
            return
        if result is None:
            self.notify("Credential could not be saved", severity="error")
        elif not result.was_new:
            self.notify("Credential is already in Vault", severity="warning")
        elif verified:
            self.notify("Credential verified and added to Vault")
        else:
            self.notify("Credential added to Vault")

    @work(thread=True)
    def _propagate_psk(self, bssid: str, ssid: str, psk: str) -> None:
        try:
            matched = self.app.vault.propagate_verified_psk(bssid, ssid, psk)
        except OSError as exc:
            self.app.call_from_thread(
                self.notify, f"Could not save a related AP's PSK: {exc}", severity="warning")
            return
        if matched:
            self.app.call_from_thread(
                self.notify, f"PSK also verified and saved for {len(matched)} related APs")

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
        except FileNotFoundError:
            self.notify("No captures yet", severity="warning")
        except OSError as exc:
            self.notify(f"Could not open captures dir: {exc}", severity="error")
