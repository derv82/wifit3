from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalGroup
from textual.content import Content
from textual.events import Resize
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Label, Rule, SelectionList, Static
from textual.widgets.selection_list import Selection

from wifit3.campaigns.campaign import Campaign
from wifit3.crack.hc22000_format import Hc22000Line
from wifit3.models import CaptureType, PersistedCapture
from wifit3.persist.ap_state import ApState, DatedHashline, Key, PasswordStatus
from wifit3.persist.pcap import FourWayHandshake
from wifit3.ui.colors import KEY_CHIP_STYLE, PASSWORD_MISMATCH_COLOR
from wifit3.ui.screens.vault.breakpoints import NARROW, SHORT, TALL, TINY, WIDE
from wifit3.ui.screens.vault.handshakes import UncrackedHandshakeList, captures_count_markup, handshake_prompt
from wifit3.ui.screens.vault.hashlines import (CrackerButton, UncrackedHashlineList, exhausted_count_text,
                                               hashline_groups, hashline_prompt, hashlines_count_markup,
                                               selected_count_text)
from wifit3.ui.screens.vault.key_row import KEY_KINDS, KeyRow, key_text
from wifit3.ui.screens.vault.rows import date_text
from wifit3.ui.vault.tools_ui import UI_TOOLS
from wifit3.vault.tools.base import VaultTool


_STATUS_COLOR = {
    PasswordStatus.CURRENT: "$text-success",
    PasswordStatus.NOT_VERIFIED: "$text-warning",
    PasswordStatus.STALE: PASSWORD_MISMATCH_COLOR,
    PasswordStatus.NOT_CRACKED: "$text-error",
}
_SHOW_AP_TOOLTIP = "Focus this AP"
_LIST_INDENT = 2                                        # the UNCRACKED rows under their header


def _no_crackable_text(files: tuple[PersistedCapture, ...]) -> str:
    count = len(files)
    return f"{count} capture{'' if count == 1 else 's'}, none crackable ({date_text(files[0].timestamp)})"


def _client_and_ssid_columns(state: ApState) -> tuple[bool, bool]:
    return len({h.line.mac_sta for h in state.hashlines}) > 1, len(state.ssids) > 1


def _status_markup(state: ApState) -> str:
    status = f"[bold {_STATUS_COLOR[state.status]}]{state.status.value}[/]"
    key = state.status_key
    if key is None:
        return status
    chip = f"[{KEY_CHIP_STYLE[state.status]}] {KEY_KINDS[key.type].badge}: {escape(key_text(key))} [/]"
    return f"{chip} {status}"


def _ssid_texts(state: ApState) -> list[tuple[str, str, Optional[str]]]:
    ssids = state.ssids
    if len(ssids) <= 1:
        return [("SSID", ssids[0].displayed_ssid if ssids else "‹hidden›", None)]
    texts: list[tuple[str, str, Optional[str]]] = []
    for i, history in enumerate(ssids):
        first, last = date_text(history.first_seen), date_text(history.last_seen)
        texts.append(("" if i else "SSID", history.displayed_ssid, first if first == last else f"{first} → {last}"))
    return texts


class AddKeyPressed(Message):
    """The user asked to add a key to the AP in view."""


class ShowApPressed(Message):
    """The user asked for Focus on the AP in view."""

    def __init__(self, bssid: str) -> None:
        super().__init__()
        self.bssid = bssid


class _SsidLine(Horizontal):
    """One SSID the AP went by, with its dates when it went by several."""

    def __init__(self) -> None:
        super().__init__(classes="section-line")
        self._section_label = Label("", classes="section")
        self._ssid_label = Label("", classes="ssid", markup=False)
        self._dates_label = Label("", classes="dates")
        self.show_ap_button = Button("Show AP", compact=True, classes="show-ap", tooltip=_SHOW_AP_TOOLTIP)

    def compose(self) -> ComposeResult:
        yield self._section_label
        yield self._ssid_label
        yield self._dates_label
        yield self.show_ap_button

    def show(self, section: str, ssid: str, dates: Optional[str]) -> None:
        self._section_label.update(section)
        self._ssid_label.update(ssid)
        self._dates_label.update(dates or "")
        self._dates_label.display = dates is not None


class _ApStatePanel(VerticalGroup):
    """One AP's status, SSIDs, keys and uncracked hashlines."""

    SCOPED_CSS = False
    DEFAULT_CSS = f"""
    _ApStatePanel {{ height: auto; max-width: 81; }}
    _ApStatePanel Rule.-horizontal {{ margin: 0; color: $panel-lighten-2; }}
    _ApStatePanel Rule.uncracked-rule {{ margin-top: 1; }}
    _ApStatePanel .status {{ width: 100%; text-align: center; margin-top: 1; }}
    _ApStatePanel .ssids {{ height: auto; }}
    _ApStatePanel .section-line {{ height: 1; }}
    _ApStatePanel .section {{ text-style: bold; color: $text-muted; width: auto; margin-right: 2; }}
    _ApStatePanel .ssids .section {{ width: 4; }}
    _ApStatePanel .ssid {{ width: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }}
    _ApStatePanel .dates {{ width: auto; color: $text-muted; margin-left: 1; }}
    _ApStatePanel Button.show-ap {{ margin-left: 2; }}
    _ApStatePanel .none-yet {{ color: $text-muted; padding-left: 2; }}
    _ApStatePanel .tools {{ height: auto; align-horizontal: center; }}
    _ApStatePanel .selected-count {{ color: $text-muted; padding-left: {2 * _LIST_INDENT}; }}
    _ApStatePanel .uncracked {{
        height: auto; max-height: 8; margin-top: 1; padding: 0 0 0 {_LIST_INDENT}; border: none;
        background: transparent; scrollbar-size-vertical: 1; text-wrap: nowrap; text-overflow: ellipsis;
    }}

    Screen.{TINY} _ApStatePanel Button.tool-btn {{ height: 1; border: none; }}
    Screen.{SHORT} _ApStatePanel Button.key-btn, Screen.{TINY} _ApStatePanel Button.key-btn,
    Screen.{NARROW} _ApStatePanel Button.key-btn {{
        height: 1; border: none;
    }}
    Screen.{SHORT} _ApStatePanel .key-line, Screen.{TINY} _ApStatePanel .key-line,
    Screen.{NARROW} _ApStatePanel .key-line {{
        height: 1;
    }}
    Screen.{TINY} _ApStatePanel .ssids _SsidLine.-older, Screen.{NARROW} _ApStatePanel .ssids _SsidLine.-older,
    Screen.{TINY} _ApStatePanel .ssids .dates, Screen.{NARROW} _ApStatePanel .ssids .dates,
    Screen.{TINY} _ApStatePanel .ssids Rule, Screen.{NARROW} _ApStatePanel .ssids Rule {{
        display: none;
    }}
    Screen.{TINY} _ApStatePanel .status, Screen.{NARROW} _ApStatePanel .status,
    Screen.{TINY} _ApStatePanel Rule.uncracked-rule, Screen.{NARROW} _ApStatePanel Rule.uncracked-rule {{
        margin-top: 0;
    }}
    Screen.{TALL}.{WIDE} _ApStatePanel .uncracked {{ max-height: 12; }}
    """

    def __init__(self) -> None:
        super().__init__()
        self._state: Optional[ApState] = None
        self._status_label = Static(classes="status")
        self._ssid_lines_container = Vertical(classes="ssids")
        self._ssid_lines: list[_SsidLine] = []
        self._ssid_rule = Rule()
        self.add_key_button = Button("Add key", compact=True, classes="add-key",
                                     tooltip="Add a PSK, WPS PIN or WEP key")
        self.key_rows: list[KeyRow] = []
        self._no_keys_label = Label("none yet", classes="none-yet")
        self._no_crackable_label = Label("", classes="none-yet")
        self._uncracked_section = VerticalGroup(classes="uncracked-section")
        self._uncracked_count_label = Label("")
        self._uncracked_list = UncrackedHashlineList(classes="uncracked")
        self._selected_count_label = Label("", classes="selected-count")
        self._cracker_buttons = [CrackerButton(tool) for tool in self.app.vault.manager.tools.values()]
        self._handshake_section = VerticalGroup(classes="uncracked-section")
        self._handshake_count_label = Label("")
        self._handshake_list = UncrackedHandshakeList(classes="uncracked")
        self._dwpa_button = Button("DWPA", "primary", classes="tool-btn", disabled=True,
                                   tooltip="Not implemented yet; work in progress")

    async def show(self, state: ApState, handshakes: tuple[FourWayHandshake, ...],
                   expanded_key_ids: frozenset[tuple[CaptureType, str]],
                   unticked_hashline_ids: frozenset[str]) -> None:
        """Update the widgets in place for ``state``; for the same AP, keep the focus, the highlighted
        hashline and each list's scroll."""
        same_ap = self._state is not None and state.bssid == self._state.bssid
        focus_key = self._key_row_focus(self.screen.focused) if same_ap else None
        history_scroll = {row.key.id: row.history_rows.scroll_y for row in self.key_rows} if same_ap else {}
        uncracked = self._uncracked_list
        uncracked_scroll = uncracked.scroll_y if same_ap else 0.0
        highlighted = uncracked.highlighted_group() if same_ap else None
        handshake_list = self._handshake_list
        handshake_scroll = handshake_list.scroll_y if same_ap else 0.0
        highlighted_handshake = handshake_list.highlighted_handshake() if same_ap else None
        show_client, show_ssid = _client_and_ssid_columns(state)
        async with self.batch():
            self._state = state
            await self._show_ssid_lines(state)
            await self._show_key_rows(state, show_client, show_ssid, expanded_key_ids)
            self._status_label.update(Content.from_markup(_status_markup(state)))
            self._no_keys_label.display = not state.keys
            files = state.handshake_and_pmkid_files
            self._no_crackable_label.display = bool(files and not state.hashlines and not handshakes)
            if files:
                self._no_crackable_label.update(_no_crackable_text(files))
            self._show_uncracked(state, show_client, show_ssid, unticked_hashline_ids)
            self._show_handshakes(state, handshakes, show_ssid)
        focused = self.screen.focused
        if focused is not None and self in focused.ancestors and focused not in self.screen.focus_chain:
            focused.blur()
        self.call_after_refresh(self._restore, focus_key, history_scroll, uncracked_scroll,
                                highlighted[0].id if highlighted else None,
                                handshake_scroll, highlighted_handshake)

    async def _show_ssid_lines(self, state: ApState) -> None:
        texts = _ssid_texts(state)
        lines = self._ssid_lines[:len(texts)] + [_SsidLine() for _ in texts[len(self._ssid_lines):]]
        await self._ssid_lines_container.remove_children(self._ssid_lines[len(texts):])
        await self._ssid_lines_container.mount_all(lines[len(self._ssid_lines):], before=self._ssid_rule)
        self._ssid_lines = lines
        for line, (section, ssid, dates) in zip(lines, texts):
            line.show(section, ssid, dates)
            line.set_class(line is not lines[0], "-older")
        self._refresh_show_ap_button()

    def _refresh_show_ap_button(self) -> None:
        """On the first SSID line, while the AP is in the device array; disabled while a campaign is active."""
        array = self.app.array
        in_array = self._state is not None and array is not None and self._state.bssid in array.access_points
        active = Campaign.active
        for line in self._ssid_lines:
            button = line.show_ap_button
            button.display = in_array and line is self._ssid_lines[0]
            button.disabled = active is not None
            if active is None:
                button.tooltip = _SHOW_AP_TOOLTIP
            else:
                button.tooltip = f"Disabled; {active.key} campaign is active"

    async def _show_key_rows(self, state: ApState, show_client: bool, show_ssid: bool,
                             expanded_key_ids: frozenset[tuple[CaptureType, str]]) -> None:
        rows = self.key_rows[:len(state.keys)] + [KeyRow() for _ in state.keys[len(self.key_rows):]]
        await self.remove_children(self.key_rows[len(state.keys):])
        await self.mount_all(rows[len(self.key_rows):], before=self._no_keys_label)
        for row, key in zip(rows, state.keys):
            await row.show(key, state, show_client=show_client, show_ssid=show_ssid,
                           expanded=key.id in expanded_key_ids)
        self.key_rows = rows

    def _restore(self, focus_key: Optional[tuple[tuple[CaptureType, str], int]],
                 history_scroll: dict[tuple[CaptureType, str], float], uncracked_scroll: float,
                 highlighted_hashline_id: Optional[str], handshake_scroll: float,
                 highlighted_handshake: Optional[FourWayHandshake]) -> None:
        for row in self.key_rows:
            row.history_rows.scroll_to(y=history_scroll.get(row.key.id, 0.0), animate=False)
        uncracked = self._uncracked_list
        index = next((i for i, group in enumerate(uncracked.groups()) if group[0].id == highlighted_hashline_id),
                     None)
        if index is not None:
            uncracked.highlighted = index
        uncracked.scroll_to(y=uncracked_scroll, animate=False)
        handshake_list = self._handshake_list
        if highlighted_handshake in handshake_list.handshakes:
            handshake_list.highlighted = handshake_list.handshakes.index(highlighted_handshake)
        handshake_list.scroll_to(y=handshake_scroll, animate=False)
        if focus_key is not None:
            key_id, button_index = focus_key
            button = next((row.buttons[button_index] for row in self.key_rows if row.key.id == key_id), None)
            if button is not None and button in self.screen.focus_chain:
                button.focus(scroll_visible=False)

    def _key_row_focus(self, widget: Optional[Widget]) -> Optional[tuple[tuple[CaptureType, str], int]]:
        for row in self.key_rows:
            if widget in row.buttons:
                return row.key.id, row.buttons.index(widget)
        return None

    def on_mount(self) -> None:
        self.set_interval(1.0, self._refresh_show_ap_button)

    def compose(self) -> ComposeResult:
        yield self._status_label
        yield Rule()
        with self._ssid_lines_container:
            yield self._ssid_rule
        with Horizontal(classes="section-line"):
            yield Label("KEYS", classes="section")
            yield self.add_key_button
        yield self._no_keys_label
        yield self._no_crackable_label
        with self._uncracked_section:
            yield Rule(classes="uncracked-rule")
            with Horizontal(classes="section-line"):
                yield Label("UNCRACKED HASHES", classes="section")
                yield self._uncracked_count_label
            yield self._uncracked_list
            yield self._selected_count_label
            with Horizontal(classes="tools"):
                yield from self._cracker_buttons
        with self._handshake_section:
            yield Rule(classes="uncracked-rule")
            with Horizontal(classes="section-line"):
                yield Label("UNCRACKED HANDSHAKES", classes="section")
                yield self._handshake_count_label
            yield self._handshake_list
            with Horizontal(classes="tools"):
                yield self._dwpa_button

    def _show_uncracked(self, state: ApState, show_client: bool, show_ssid: bool,
                        unticked_hashline_ids: frozenset[str]) -> None:
        uncracked = state.uncracked
        self._uncracked_section.display = bool(uncracked)
        exhausted = state.exhausted_wordlists
        self._uncracked_count_label.update(
            Content.from_markup(hashlines_count_markup(len(uncracked), len(state.hashlines))))
        groups = hashline_groups(uncracked)
        ticked = [not all(h.id in unticked_hashline_ids for h in group) for group in groups]
        exhausted_counts = [exhausted_count_text(group, exhausted) for group in groups]
        exhausted_width = max(map(len, exhausted_counts), default=0)
        self._uncracked_list.show(
            [Selection(hashline_prompt(group, show_client, show_ssid, count, exhausted_width), group, is_ticked)
             for group, count, is_ticked in zip(groups, exhausted_counts, ticked)], exhausted)
        selected = [h.line for group, is_ticked in zip(groups, ticked) if is_ticked for h in group]
        self._show_selected_count(selected)
        uncracked_lines = [h.line for h in uncracked]
        for button in self._cracker_buttons:
            button.display = state.hashline_capture is not None and button.tool.can_crack(uncracked_lines)

    def _show_handshakes(self, state: ApState, handshakes: tuple[FourWayHandshake, ...],
                         show_ssid: bool) -> None:
        psks = [key.value for key in state.keys if key.type == CaptureType.WPA_PSK]
        uncracked = tuple(h for h in handshakes if not any(h.matches_psk(psk) for psk in psks))
        self._handshake_section.display = bool(uncracked)
        self._handshake_count_label.update(
            Content.from_markup(captures_count_markup(len(uncracked), len(handshakes))))
        show_client = len({h.client_mac for h in handshakes}) > 1
        label_width = max((len(h.messages_label) for h in uncracked), default=0)
        self._handshake_list.show(uncracked, [handshake_prompt(h, show_client, show_ssid, label_width)
                                              for h in uncracked])

    def _show_selected_count(self, selected: list[Hc22000Line]) -> None:
        self._selected_count_label.update(selected_count_text(len(selected)))
        for button in self._cracker_buttons:
            button.disabled = not button.tool.can_crack(selected)

    def _selected_hashlines(self) -> list[DatedHashline]:
        return [h for group in self._uncracked_list.selected for h in group]

    @on(SelectionList.SelectedChanged)
    def _selection_changed(self, event: SelectionList.SelectedChanged) -> None:
        if event.selection_list is self._uncracked_list:
            self._show_selected_count([h.line for h in self._selected_hashlines()])

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        if isinstance(event.button, CrackerButton):
            self._launch_tool(event.button.tool)
        elif event.button is self._dwpa_button:
            self._upload_to_dwpa()
        elif event.button is self.add_key_button:
            self.post_message(AddKeyPressed())
        elif (self._ssid_lines and event.button is self._ssid_lines[0].show_ap_button
              and self._state is not None):
            self.post_message(ShowApPressed(self._state.bssid))
        else:
            return
        event.stop()

    def _upload_to_dwpa(self) -> None:
        """The highlighted 4-way and its beacon, alone in a temporary pcap, for DWPA."""
        handshake = self._handshake_list.highlighted_handshake()
        if handshake is None:
            return
        fd, name = tempfile.mkstemp(suffix=".pcap")
        os.close(fd)
        handshake.write(Path(name))
        # Upload Path(name) to DWPA here, then delete it.

    def _launch_tool(self, tool: VaultTool) -> None:
        state = self._state
        if state is None or state.hashline_capture is None:
            return
        capture = state.hashline_capture
        selected = self._selected_hashlines()

        def _on_config(config: dict | None) -> None:
            if not config:
                return
            job_id = self.app.vault.manager.submit_job(
                tool.name, capture, self.app.vault.add_probed_ssids(config), hashlines=[h.raw for h in selected])
            self.notify(f"Queued job: {job_id}")

        self.app.push_screen(UI_TOOLS[tool.name](bssid=state.bssid, probed_ssids=self.app.vault.probed_ssids(),
                                                 hashlines=selected), _on_config)


class VaultItemView(Vertical):
    DEFAULT_CSS = """
    VaultItemView {
        width: 1fr;
        height: 1fr;
        align-horizontal: center;
        background: $background;
        border: heavy $primary;
        border-title-align: center;
        border-title-color: $primary;
        border-title-style: bold;
        padding: 0 1;
        overflow-y: auto;
        scrollbar-size-vertical: 1;
    }
    VaultItemView #vault-item-empty { color: $text-muted; }
    """

    def __init__(self, id: Optional[str] = None) -> None:
        super().__init__(id=id)
        self.history_expanded_by_user: dict[tuple[CaptureType, str], bool] = {}    # by Key.id, this AP's
        self.unticked_hashline_ids: set[str] = set()
        self._bssid = ""
        self._ssid: Optional[str] = None
        self._state: Optional[ApState] = None
        self._handshakes: tuple[FourWayHandshake, ...] = ()

    def load(self, bssid: str, ssid: Optional[str]) -> None:
        """Show this AP. Reloading the AP in view keeps the pane's scroll, the focus, each list's
        scroll, and what the user expanded and ticked."""
        state = self.app.vault.ap_state(bssid) if bssid else None
        handshakes = self.app.vault.pcap_handshakes(bssid) if bssid else ()
        if (bssid, ssid, state) == (self._bssid, self._ssid, self._state) and handshakes is self._handshakes:
            return
        same_ap = bssid == self._bssid
        same_state = state == self._state and handshakes is self._handshakes
        if not same_ap:
            self.history_expanded_by_user.clear()
        self._bssid, self._ssid, self._state, self._handshakes = bssid, ssid, state, handshakes
        if state is not None:
            self._set_title()
        if same_ap and same_state:
            return
        self.call_later(self._show_state)
        if not same_ap:
            self.scroll_home(animate=False)

    async def _show_state(self) -> None:
        state = self._state
        async with self.batch():
            if (state is None) != (self._panel() is None):
                await self.recompose()
            panel = self._panel()
            if state is not None and panel is not None:
                await panel.show(state, self._handshakes, *self._panel_choices(state))

    def _panel(self) -> Optional[_ApStatePanel]:
        return next(iter(self.query_children(_ApStatePanel).results(_ApStatePanel)), None)

    def _key_rows(self) -> list[KeyRow]:
        panel = self._panel()
        return [] if panel is None else panel.key_rows

    def history_expanded(self, key: Key) -> bool:
        """The user's choice for this key's history, else expanded only in a tall, wide drawer."""
        chosen = self.history_expanded_by_user.get(key.id)
        return chosen if chosen is not None else self.screen.has_class(TALL) and self.screen.has_class(WIDE)

    def _panel_choices(self, state: ApState) -> tuple[frozenset[tuple[CaptureType, str]], frozenset[str]]:
        expanded = frozenset(key.id for key in state.keys if self.history_expanded(key))
        return expanded, frozenset(self.unticked_hashline_ids)

    def _set_title(self) -> None:
        self.border_title = Content.from_text(f" {self._ssid or '‹hidden›'} ({self._bssid}) ")

    def compose(self) -> ComposeResult:
        if self._state is None:
            self.border_title = "Vault Detail"
            yield Label("Select an AP to view its captures", id="vault-item-empty")
            return
        yield _ApStatePanel()

    def on_resize(self, event: Resize) -> None:
        self.call_after_refresh(self._expand_by_drawer_size)

    def _expand_by_drawer_size(self) -> None:
        for row in self._key_rows():
            row.expanded = self.history_expanded(row.key)

    @on(KeyRow.Toggled)
    def _key_toggled(self, event: KeyRow.Toggled) -> None:
        self.history_expanded_by_user[event.key.id] = event.expanded

    @on(SelectionList.SelectionToggled)
    def _hashline_toggled(self, event: SelectionList.SelectionToggled) -> None:
        if not isinstance(event.selection_list, UncrackedHashlineList):
            return
        ids = {h.id for h in event.selection.value}
        if ids <= self.unticked_hashline_ids:
            self.unticked_hashline_ids -= ids
        else:
            self.unticked_hashline_ids |= ids
