from typing import Optional

from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import DataTable, Footer
from textual.screen import ModalScreen
from textual import events, on, work
from textual.binding import Binding

from .vault_table import VaultTable
from .vault_item import ConfirmModal, VaultItemView
from wifit3.models import AccessPoint
from wifit3.persist.metadata import load_metadata, metadata_path, save_ap_nickname
from wifit3.persist.repair import RepairResult, repair_vault
from wifit3.ui.vault.job_pane import JobTrackerPane
from wifit3.ui.vault.modals.add_credential import AddCredentialModal, ManualCredential
from wifit3.ui.vault.modals.nickname import NicknameModal

class VaultDrawer(ModalScreen):
    """The Vault drawer overlay."""

    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (90, "-wide")]

    BINDINGS = [
        Binding("v", "dismiss_drawer", "Close", show=False),
        Binding("escape", "dismiss_drawer", "Close", show=True),
        Binding("a", "add_credential", "Add", show=True),
        Binding("n", "set_nickname", "Nickname", show=True),
        Binding("c", "crack_all", "Crack All", show=True),
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

    #vault-panes {
        height: 1fr;
    }

    VaultDrawer.-narrow _CapturePanel .tools, VaultDrawer.-narrow _CapturePanel .actions {
        layout: vertical;
    }
    VaultDrawer.-narrow _CapturePanel .spacer { display: none; }
    VaultDrawer.open #vault-content {
        offset-y: 0%;
    }
    """

    def __init__(self, preselected_bssid: Optional[str] = None) -> None:
        super().__init__()
        self._preselected_bssid = preselected_bssid
        self._metadata_generation_targets: set[str] = set()

    def compose(self) -> ComposeResult:
        with Container(id="vault-content"):
            with Horizontal(id="vault-panes"):
                yield VaultTable(id="vault-table")
                yield VaultItemView(id="vault-item")
            yield JobTrackerPane()
            yield Footer()

    def on_mount(self) -> None:
        self.app.vault.refresh()
        self.query_one("#vault-table", VaultTable).reload_table()
        self.call_after_refresh(lambda: self.add_class("open"))
        self.call_after_refresh(self._focus_list)
        self.call_after_refresh(self._offer_metadata_generation)

    def _offer_metadata_generation(self) -> None:
        missing = self.app.vault.claim_new_missing_metadata()
        if not missing:
            return
        self._metadata_generation_targets = missing
        noun = "AP" if len(missing) == 1 else "APs"
        prompt = (
            f"Found [bold]{len(missing)}[/] new saved {noun} without Vault metadata. "
            "Generate metadata and repair derived HC22000 and safe SSID filenames now?\n\n"
            "[bold]Original HC22000 and pcap sources will be preserved in a v1 backup "
            "directory. Saved passwords and credentials will not be changed.[/]"
        )
        self.app.push_screen(ConfirmModal(prompt), self._metadata_generation_confirmed)

    def _metadata_generation_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        self.notify("Repairing Vault data…")
        self._generate_missing_metadata()

    @work(thread=True, exclusive=True)
    def _generate_missing_metadata(self) -> None:
        result = repair_vault()
        self.app.call_from_thread(self._metadata_generation_finished, result)

    def _metadata_generation_finished(self, result: RepairResult) -> None:
        self.app.vault.refresh()
        self.query_one("#vault-table", VaultTable).reload_table()
        generated = len(self._metadata_generation_targets)
        noun = "AP" if generated == 1 else "APs"
        message = f"Generated metadata for {generated} new {noun}"
        if result.aps != generated:
            message += f"; scanned {result.aps} saved APs"
        if result.backup_dir is not None:
            message += f". Originals: {result.backup_dir.name}"
        if result.errors:
            message += f". First error: {result.errors[0]}"
            self.notify(message, severity="warning", title=f"{len(result.errors)} repair errors")
        else:
            self.notify(message, title="Vault repair complete")

    def _focus_list(self) -> None:
        """Land focus on the AP list so navigation and shortcuts work immediately."""
        try:
            self.query_one("#vault-aps", DataTable).focus()
        except Exception:
            pass

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

    def action_add_credential(self) -> None:
        ssid, bssid = self._selected_network()
        self.app.push_screen(AddCredentialModal(ssid, bssid), self._credential_added)

    def action_set_nickname(self) -> None:
        ssid, bssid = self._selected_network()
        if not bssid:
            self.notify("Select a router first", severity="warning")
            return
        nickname = load_metadata(metadata_path(bssid)).get("nickname")
        current = nickname if isinstance(nickname, str) else ""
        self.app.push_screen(NicknameModal(ssid, current), self._nickname_saved)

    def _nickname_saved(self, nickname: str | None) -> None:
        if nickname is None:
            return
        ssid, bssid = self._selected_network()
        if not bssid:
            return
        try:
            save_ap_nickname(AccessPoint(bssid=bssid, ssid=ssid), nickname)
        except OSError as exc:
            self.notify(f"Could not save nickname: {exc}", severity="error")
            return
        table = self.query_one("#vault-table", VaultTable)
        table.set_nickname(bssid, nickname)
        table.reload_table()
        self.notify("Nickname saved" if nickname else "Nickname cleared")

    def action_crack_all(self) -> None:
        self.notify("Crack All is coming later")

    def check_action(self, action: str, parameters: tuple) -> Optional[bool]:
        if action == "crack_all":
            return False
        return True

    def _selected_network(self) -> tuple[str, str]:
        table = self.query_one("#vault-aps", DataTable)
        if table.row_count == 0:
            return "", ""
        try:
            bssid = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return "", ""
        entry = self.query_one("#vault-table", VaultTable)._aps.get(bssid)
        return ((entry[0] or "") if entry else "", bssid)

    def _credential_added(self, credential: ManualCredential | None) -> None:
        if credential is None:
            return
        ap = AccessPoint(bssid=credential.bssid, ssid=credential.ssid)
        verified = False
        if credential.kind == "wpa_psk":
            verification = self.app.vault.verify_wpa_psk(ap.bssid, credential.value)
            if verification is False:
                self.notify("PSK does not match the saved handshake", severity="error")
                return
            verified = verification is True
            result = self.app.vault.save_wpa_psk(ap.ssid, ap.bssid, credential.value)
            if result is not None:
                self._verify_related_aps(ap.bssid, ap.ssid, credential.value)
        elif credential.kind == "wps_pin":
            result = self.app.vault.save_wps_pin(ap, credential.value, None)
        elif credential.kind == "eap":
            result = self.app.vault.save_eap_credential(
                ap, credential.username, credential.password, credential.eap_settings
            )
        else:
            value = credential.value
            key = bytes.fromhex(value) if len(value) in (10, 26) else value.encode("ascii")
            result = self.app.vault.save_wep_key(ap, key)
        if result is None:
            self.notify("Credential could not be saved", severity="error")
        elif result.was_new:
            message = "Credential verified and added to Vault" if verified else "Credential added to Vault"
            self.notify(message)
        else:
            self.notify("Credential is already in Vault", severity="warning")

    @work(thread=True)
    def _verify_related_aps(self, bssid: str, ssid: str, psk: str) -> None:
        try:
            matched = self.app.vault.propagate_verified_psk(bssid, ssid, psk)
        except OSError as exc:
            self.app.call_from_thread(self._related_psk_failed, str(exc))
            return
        self.app.call_from_thread(self._related_psk_finished, matched)

    def _related_psk_finished(self, matched: list[str]) -> None:
        if matched:
            noun = "AP" if len(matched) == 1 else "APs"
            self.notify(f"PSK also verified and saved for {len(matched)} related {noun}")

    def _related_psk_failed(self, error: str) -> None:
        self.notify(f"Could not save a related AP credential: {error}", severity="warning")

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
