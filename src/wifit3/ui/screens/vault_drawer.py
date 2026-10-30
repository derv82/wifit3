import asyncio
from typing import Optional

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Footer, DataTable, Input
from textual.screen import ModalScreen
from textual import events, on, work
from textual.binding import Binding

from .vault_breakpoints import COMPACT, NARROW, SHORT, TALL, TINY, WIDE
from .vault_table import VaultTable
from .vault_item import AddKeyPressed, DeleteKeyPressed, VaultItemView, key_description
from wifit3.models import AccessPoint, CaptureType, PersistedCapture
from wifit3.persist.ap_state import ApState, is_confirmed
from wifit3.persist.meta import meta_path, read_meta, update_meta
from wifit3.ui.vault.job_pane import JobTrackerPane
from wifit3.ui.vault.modals.add_credential import AddCredentialModal, ManualCredential
from wifit3.ui.vault.modals.confirm import ConfirmModal
from wifit3.ui.vault.modals.nickname import NicknameModal


def _ap_contents_text(captures: list[PersistedCapture], state: ApState) -> str:
    capture_count = len({c.path for c in captures if c.type in (CaptureType.HS, CaptureType.PMKID)})
    pin_count = sum(1 for k in state.keys if k.type == CaptureType.WPS_PIN)
    key_count = sum(1 for k in state.keys if k.is_password)
    counts = [(capture_count, "capture"), (pin_count, "PIN"), (key_count, "key")]
    return ", ".join(f"{n} {noun}{'' if n == 1 else 's'}" for n, noun in counts if n)


class VaultDrawer(ModalScreen):
    """The Vault drawer overlay."""

    _SELECTION_DELAY = 0.1

    HORIZONTAL_BREAKPOINTS = [(0, TINY), (60, NARROW), (90, COMPACT), (105, WIDE)]
    VERTICAL_BREAKPOINTS = [(0, SHORT), (36, TALL)]

    BINDINGS = [
        Binding("v", "dismiss_drawer", "Close", show=False),
        Binding("escape", "dismiss_drawer", "Close", show=True),
        Binding("f", "focus_filter", "Filter", show=False),
        Binding("/", "focus_filter", "Filter", show=False),
        Binding("n", "set_nickname", "Nickname", show=True),
        Binding("a", "add_credential", "Add Key", show=True),
        Binding("o", "show_directory", "Open Dir", show=True),
        Binding("z", "export_zip", "Export .zip", show=True),
        Binding("d", "delete_ap", "Delete AP", show=True),
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

    VaultDrawer.-tiny #vault-main, VaultDrawer.-narrow #vault-main, VaultDrawer.-compact #vault-main {
        layout: vertical;
    }
    VaultDrawer.-tiny #vault-table, VaultDrawer.-narrow #vault-table, VaultDrawer.-compact #vault-table,
    VaultDrawer.-tiny #vault-item, VaultDrawer.-narrow #vault-item, VaultDrawer.-compact #vault-item {
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
    VaultDrawer #vault-item .key-line Button, VaultDrawer #vault-item Button.add-key {
        min-width: 0;
        margin-right: 0;
    }
    VaultDrawer #vault-item .key-line Button.key-btn { margin-left: 1; }
    VaultDrawer.-tiny #vault-item Button.tool-btn { min-width: 0; }
    """

    def __init__(self, preselected_bssid: Optional[str] = None) -> None:
        super().__init__()
        self._preselected_bssid = preselected_bssid
        self._reveal_scheduled = False
        self._selection_timer = None
        self._pending_bssid: str | None = None
        self._psk_to_propagate_after_reload: tuple[str, str, str] | None = None

    def compose(self) -> ComposeResult:
        with Container(id="vault-content"):
            with Horizontal(id="vault-main"):
                yield VaultTable(id="vault-table")
                yield VaultItemView(id="vault-item")
            yield JobTrackerPane()
            yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self._focus_list)

    def _schedule_reveal(self) -> None:
        if self.has_class("open") or self._reveal_scheduled:
            return
        self._reveal_scheduled = True
        self.call_after_refresh(self._reveal_after_content_layout)

    def _reveal_after_content_layout(self) -> None:
        self.call_after_refresh(self._reveal)

    def _reveal(self) -> None:
        self._reveal_scheduled = False
        if self.is_attached:
            self.add_class("open")
            self.app.preload_vault_states()

    def _focus_list(self) -> None:
        """Land focus on the AP list so up/down works without a Tab press."""
        try:
            self.query_one("#vault-aps", DataTable).focus()
        except Exception:
            pass

    def action_focus_filter(self) -> None:
        self.query_one("#vault-filter", Input).focus()

    def action_dismiss_drawer(self) -> None:
        self._cancel_pending_selection()
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

    def _cancel_pending_selection(self) -> None:
        if self._selection_timer is not None:
            self._selection_timer.stop()
            self._selection_timer = None
        self._pending_bssid = None

    def _schedule_widget_load(self, bssid: str) -> None:
        """Load at once, unless the previous highlight came within the delay (a held key): then
        load once the highlights stop."""
        keys_held = self._selection_timer is not None
        self._cancel_pending_selection()
        self._pending_bssid = bssid
        if not keys_held:
            self._load_pending_widget()
        self._selection_timer = self.set_timer(self._SELECTION_DELAY, self._load_pending_widget)

    def _load_pending_widget(self) -> None:
        bssid = self._pending_bssid
        self._selection_timer = None
        self._pending_bssid = None
        if bssid is None:
            return
        try:
            table = self.query_one("#vault-aps", DataTable)
            selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return
        if selected == bssid:
            self._load_widget(bssid)

    def _load_widget(self, bssid: str) -> None:
        widget = self.query_one("#vault-item", VaultItemView)
        if bssid and self.app.vault.persisted(bssid):
            ssids = self.app.vault.ap_state(bssid).ssids
            ssid = ssids[0].ssid if ssids else self.app.vault.ssid(bssid)
            widget.load(bssid, self.query_one("#vault-table", VaultTable).display_name(bssid, ssid))
        else:
            widget.load("", None)

    def vault_changed(self, full_refresh: bool) -> None:
        """Reload the table, re-reading nicknames after a full refresh; its TableReloaded reloads
        the item view, which repaints only when its AP's state changed."""
        table = self.query_one("#vault-table", VaultTable)
        if full_refresh:
            table.reload_nicknames()
        else:
            table.reload_table()

    @on(VaultTable.TableReloaded)
    def _table_reloaded(self, event: VaultTable.TableReloaded) -> None:
        self._cancel_pending_selection()
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
        if self._psk_to_propagate_after_reload is not None:
            self.call_after_refresh(self._propagate_psk, *self._psk_to_propagate_after_reload)
            self._psk_to_propagate_after_reload = None
        self._schedule_reveal()

    @on(DataTable.RowHighlighted, "#vault-aps")
    def _ap_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self._schedule_widget_load(event.row_key.value)

    def _selected_network(self) -> tuple[Optional[str], str]:
        """(SSID, BSSID) of the row under the cursor; BSSID is "" when there is none."""
        aps = self.query_one("#vault-aps", DataTable)
        if aps.row_count == 0:
            return None, ""
        bssid = aps.coordinate_to_cell_key(aps.cursor_coordinate).row_key.value or ""
        return self.app.vault.ssid(bssid), bssid

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

    def action_delete_ap(self) -> None:
        ssid, bssid = self._selected_network()
        if not bssid:
            self.notify("Select an AP first", severity="warning")
            return
        name = self.query_one("#vault-table", VaultTable).display_name(bssid, ssid)
        contents = _ap_contents_text(self.app.vault.persisted(bssid), self.app.vault.ap_state(bssid))
        prompt = f"Delete [bold]{escape(name)}[/]?" + (f" {contents}" if contents else "")

        def _delete(confirmed: Optional[bool]) -> None:
            if not confirmed:
                return
            try:
                self.app.vault.delete_ap(bssid)
            except OSError as exc:
                self.notify(f"Could not delete {name}: {exc}", severity="error")
                return
            self.notify(f"Deleted {name}")

        self.app.push_screen(ConfirmModal(prompt), _delete)

    @on(DeleteKeyPressed)
    def _delete_key_pressed(self, event: DeleteKeyPressed) -> None:
        key = event.key
        bssid = key.newest_capture.bssid
        paths = {c.path for c in key.captures}
        state = self.app.vault.ap_state(bssid)
        others = [k for k in state.keys if k.id != key.id and {c.path for c in k.captures} <= paths]
        description = escape(key_description(key))
        prompt = f"Delete [bold]{description}[/]?"
        if others:
            prompt += f" Also removes {escape(', '.join(key_description(k) for k in others))}."

        def _delete(confirmed: Optional[bool]) -> None:
            if not confirmed:
                return
            try:
                self.app.vault.delete_key(bssid, key.id)
            except OSError as exc:
                self.notify(f"Could not delete {description}: {escape(str(exc))}", severity="error")
                return
            self.notify(f"Deleted {description}")

        self.app.push_screen(ConfirmModal(prompt), _delete)

    @on(AddKeyPressed)
    def _add_key_pressed(self, event: AddKeyPressed) -> None:
        self.action_add_credential()

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
                result = vault.save_wpa_psk(ap.ssid, ap.bssid, credential.value, manual=True)
                if result is not None:
                    state = vault.ap_state(ap.bssid)
                    key = next(k for k in state.keys if k.id == (CaptureType.WPA_PSK, credential.value))
                    verified = is_confirmed(key, state.hashlines)
                    if result.was_new:
                        self._psk_to_propagate_after_reload = (ap.bssid, ap.ssid, credential.value)
                    else:
                        self._propagate_psk(ap.bssid, ap.ssid, credential.value)
            elif credential.kind == CaptureType.WPS_PIN:
                result = vault.save_wps_pin(ap, credential.value, None, manual=True)
            else:
                value = credential.value
                key = bytes.fromhex(value) if len(value) in (10, 26) else value.encode("ascii")
                result = vault.save_wep_key(ap, key, manual=True)
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
            self.notify("Credential added, not verified")

    @work
    async def _propagate_psk(self, bssid: str, ssid: str, psk: str) -> None:
        propagation = asyncio.wrap_future(self.app.vault.propagate_verified_psk(bssid, ssid, psk))
        try:
            matched = await asyncio.shield(propagation)
        except OSError as exc:
            self.notify(f"Could not save a related AP's PSK: {exc}", severity="warning")
            return
        if matched:
            self.notify(f"PSK also verified and saved for {len(matched)} related APs")

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
