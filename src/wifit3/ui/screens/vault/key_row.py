from __future__ import annotations

from dataclasses import dataclass

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalGroup, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Button, Label, Static

from wifit3.campaigns.pin import EMPTY_PIN_LABEL
from wifit3.models import CREDENTIAL_NAMES, CaptureType
from wifit3.persist.ap_state import ApState, Key, LiveVerification, PasswordStatus, epoch_sort_key
from wifit3.ui.colors import KEY_CHIP_STYLE
from wifit3.ui.encryption_format import wep_key_ascii
from wifit3.ui.screens.vault.hashlines import hashline_groups, hashline_prompt
from wifit3.ui.screens.vault.rows import row_content, verdict_tooltip


_EXPAND_LABEL, _COLLAPSE_LABEL = "▶", "▼"
_EXPAND_TOOLTIP, _COLLAPSE_TOOLTIP = "Show captures and verifications", "Hide captures and verifications"


@dataclass(frozen=True)
class _KeyKind:
    badge: str          # before the value in its row, and on the status chip
    badge_color: str


KEY_KINDS = {
    CaptureType.WPA_PSK: _KeyKind("PSK", "$text-success"),
    CaptureType.WPS_PIN: _KeyKind("WPS", "$text-accent"),
    CaptureType.WEP: _KeyKind("WEP", "$text-secondary"),
}
_TOGGLE_WIDTH = 3
_BADGE_WIDTH = max(len(kind.badge) for kind in KEY_KINDS.values())
_VALUE_COLUMN = _TOGGLE_WIDTH + _BADGE_WIDTH + 2       # a space after the badge, then the chip's own space


def key_description(key: Key) -> str:
    """``PSK hunter22``, ``WPS PIN 12345670``, ``WEP key abcde``."""
    return f"{CREDENTIAL_NAMES[key.type]} {key_text(key)}"


def _status_reason(key: Key, status: PasswordStatus) -> str:
    if status is PasswordStatus.CURRENT:
        return "verified"
    if status is PasswordStatus.STALE:
        return "stale"
    if key.manual_capture_names:
        return "added, unverified"
    return "not verified yet"


def key_text(key: Key) -> str:
    """The key as the user types it: a WEP key in ASCII when printable, else hex."""
    if key.type == CaptureType.WEP:
        return wep_key_ascii(key.value) or key.value
    if key.type == CaptureType.WPS_PIN:
        return key.value or EMPTY_PIN_LABEL
    return key.value


def _live_verification_content(verification: LiveVerification, show_client: bool) -> Content:
    verdict = (("verified live", "$text-success") if verification.correct
               else ("rejected live", "$text-error"))
    return row_content(verification.at, "", "", verdict, show_client, [])


def _status_class(status: PasswordStatus) -> str:
    return "-" + status.name.lower().replace("_", "-")


class DeleteKeyPressed(Message):
    """The user asked to delete ``key`` and every file holding it."""

    def __init__(self, key: Key) -> None:
        super().__init__()
        self.key = key


class KeyRow(VerticalGroup):
    DEFAULT_CSS = f"""
    KeyRow {{ height: auto; }}
    KeyRow .key-line {{ height: 3; }}
    KeyRow .no-toggle {{ width: {_TOGGLE_WIDTH}; }}
    KeyRow Button.toggle {{
        width: {_TOGGLE_WIDTH}; height: 100%; content-align: center middle; background: transparent;
    }}
    KeyRow .key-text {{
        width: 1fr; height: 100%; content-align: left middle; text-wrap: nowrap; text-overflow: ellipsis;
    }}
    KeyRow .history {{ display: none; height: auto; padding-left: {_VALUE_COLUMN}; }}
    KeyRow.-expanded .history {{ display: block; }}
    KeyRow .history-rows {{ height: auto; max-height: 8; scrollbar-size-vertical: 1; }}
    KeyRow .history-row {{ height: 1; text-wrap: nowrap; text-overflow: ellipsis; }}
    """

    expanded: reactive[bool] = reactive(False)

    class Toggled(Message):
        def __init__(self, key: Key, expanded: bool) -> None:
            super().__init__()
            self.key = key
            self.expanded = expanded

    key: Key
    status: PasswordStatus
    _state: ApState
    _show_client: bool
    _show_ssid: bool
    _has_history: bool

    def __init__(self) -> None:
        super().__init__()
        self._history_built = False
        self.toggle_button = Button("", compact=True, classes="toggle")
        self._no_toggle_label = Label("", classes="no-toggle")
        self._summary_label = Label("", classes="key-text")
        self.copy_button = Button("Copy", classes="key-btn")
        self.delete_button = Button("Delete", variant="error", classes="key-btn",
                                    tooltip="Delete this key and every file holding it")
        self.history_rows = VerticalScroll(classes="history-rows")

    @property
    def buttons(self) -> tuple[Button, Button, Button]:
        return self.toggle_button, self.copy_button, self.delete_button

    async def show(self, key: Key, state: ApState, *, show_client: bool, show_ssid: bool, expanded: bool) -> None:
        """Show ``key``; its history rows are rebuilt only when they would read differently."""
        if self._history_built and (key, state.hashlines, show_client, show_ssid) != (
                self.key, self._state.hashlines, self._show_client, self._show_ssid):
            self._history_built = False
            await self.history_rows.remove_children()
        self.status = state.key_statuses[key.id]
        for status in PasswordStatus:
            self.set_class(status is self.status, _status_class(status))
        self.key, self._state = key, state
        self._show_client, self._show_ssid = show_client, show_ssid
        self._has_history = bool(key.live_verifications or key.hashline_ids)
        self.toggle_button.display = self._has_history
        self._no_toggle_label.display = not self._has_history
        self._summary_label.update(Content.from_markup(self._summary_markup()))
        self._summary_label.tooltip = self._details()
        self.set_reactive(KeyRow.expanded, expanded)
        self._show_expansion(expanded)
        if expanded:
            await self._build_history()

    def _history(self) -> list[Static]:
        rows = [(v.at, Static(_live_verification_content(v, self._show_client), classes="history-row"))
                for v in self.key.live_verifications]
        matched = tuple(h for h in self._state.hashlines if h.id in self.key.hashline_ids)
        for group in hashline_groups(matched):
            row = Static(hashline_prompt(group, self._show_client, self._show_ssid, "", 0), classes="history-row")
            row.tooltip = verdict_tooltip(group[0].line.authorized)
            rows.append((group[0].first_captured, row))
        return [row for _, row in sorted(rows, key=lambda row: epoch_sort_key(row[0]), reverse=True)]

    def compose(self) -> ComposeResult:
        with Horizontal(classes="key-line"):
            yield self.toggle_button
            yield self._no_toggle_label
            yield self._summary_label
            yield self.copy_button
            yield self.delete_button
        with Vertical(classes="history"):
            yield self.history_rows

    async def _build_history(self) -> None:
        """The history rows, built the first time the key is expanded."""
        if self._history_built or not self._has_history:
            return
        self._history_built = True
        await self.history_rows.mount_all(self._history())

    def _summary_markup(self) -> str:
        key = self.key
        kind = KEY_KINDS[key.type]
        markup = (f"[bold {kind.badge_color}]{kind.badge.ljust(_BADGE_WIDTH)}[/] "
                  f"[{KEY_CHIP_STYLE[self.status]}] {escape(key_text(key))} [/]")
        return f"{markup} [dim]({self._details()})[/dim]"

    def _details(self) -> str:
        details = [_status_reason(self.key, self.status)]
        total = len(self._state.hashlines)
        if self.key.type == CaptureType.WPA_PSK and total:
            details.insert(0, f"{len(self.key.hashline_ids)}/{total} hashlines")
        return ", ".join(details)

    def _show_expansion(self, expanded: bool) -> None:
        self.set_class(expanded, "-expanded")
        self.toggle_button.label = _COLLAPSE_LABEL if expanded else _EXPAND_LABEL
        self.toggle_button.tooltip = _COLLAPSE_TOOLTIP if expanded else _EXPAND_TOOLTIP

    async def watch_expanded(self, expanded: bool) -> None:
        self._show_expansion(expanded)
        if expanded and self.is_mounted:
            await self._build_history()

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button is self.toggle_button:
            self.expanded = not self.expanded
            self.post_message(self.Toggled(self.key, self.expanded))
        elif event.button is self.copy_button:
            self._copy()
        elif event.button is self.delete_button:
            self.post_message(DeleteKeyPressed(self.key))
        else:
            return
        event.stop()

    def _copy(self) -> None:
        if self.key.type == CaptureType.WPS_PIN and not self.key.value:
            self.app.copy_to_clipboard("")
            self.notify("PIN is empty")
            return
        self.app.copy_to_clipboard(key_text(self.key))
        self.notify("Copied to clipboard")
