"""The Scanner's filter bar and the ScanFilter predicate."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Button, Input, Label, Select

from wifit3.models import AccessPoint
from wifit3.ui.encryption_format import EncryptionType
from wifit3.wlan.channels import band_ranges


def _token_matches(token: str, bssid: str, ssid: str) -> bool:
    if len(token) == 1:
        return token in ssid
    return token in bssid or token in ssid


def text_matches(query: str, bssid: str, ssid: Optional[str]) -> bool:
    """Whitespace tokens AND-matched case-insensitively; a single-char token hits the SSID only."""
    bssid_l = bssid.lower()
    ssid_l = (ssid or "").lower()
    return all(_token_matches(t, bssid_l, ssid_l) for t in query.lower().split())


class EncryptionFilter(Enum):
    """ENCRYPT dropdown choices; ``value`` doubles as the label. WPA/2 merges WPA1+WPA2."""
    ALL = "All"
    OPEN = "Open"
    WEP = "WEP"
    WPA = "WPA/2"
    WPA3_TRANSITION = "WPA3→2"
    WPA3 = "WPA3"

    def matches(self, ap: AccessPoint) -> bool:
        if self is EncryptionFilter.ALL:
            return True
        return EncryptionType.from_ap(ap) in _FILTER_TYPES[self]


_FILTER_TYPES = {
    EncryptionFilter.OPEN: {EncryptionType.OPEN},
    EncryptionFilter.WEP: {EncryptionType.WEP},
    EncryptionFilter.WPA: {EncryptionType.WPA1, EncryptionType.WPA2},
    EncryptionFilter.WPA3_TRANSITION: {EncryptionType.WPA3_TRANSITION},
    EncryptionFilter.WPA3: {EncryptionType.WPA3},
}


@dataclass(frozen=True)
class ScanFilter:
    text: str = ""
    encryption: EncryptionFilter = EncryptionFilter.ALL

    def matches(self, ap: AccessPoint, *, ssid: Optional[str] = None) -> bool:
        """``ssid`` overrides ap.ssid so a hidden AP is searchable by its guessed name."""
        return self.encryption.matches(ap) and text_matches(self.text, ap.bssid, ssid or ap.ssid)


class FilterBar(Horizontal):
    """One row above the AP table: text query, encryption select, channels button."""

    DEFAULT_CSS = """
    FilterBar {
        height: auto;
        padding: 0 1;
        border: round $primary;
        border-title-color: $primary;
        border-title-style: bold;
    }
    FilterBar > Label { margin-right: 1; color: $text-muted; }
    FilterBar > #filter-encryption { width: 12; margin-right: 2; }
    FilterBar > #filter-channels { margin-right: 2; }
    FilterBar > Input { width: 32; }
    FilterBar Select.-expanded SelectOverlay { border: round $primary !important; background: $surface; }
    """

    BINDINGS = [Binding("escape", "leave", "", show=False)]

    class ScanFilterChanged(Message):
        def __init__(self, scan_filter: ScanFilter) -> None:
            super().__init__()
            self.scan_filter = scan_filter

    class EditChannels(Message):
        pass

    def __init__(self, supported_channels: List[int]) -> None:
        super().__init__()
        self._supported = sorted(set(supported_channels))
        self.border_title = "FILTER"

    def compose(self) -> ComposeResult:
        yield Label("[u]E[/u]ncryption")
        yield Select(
            [(f.value, f) for f in EncryptionFilter], value=EncryptionFilter.ALL,
            allow_blank=False, id="filter-encryption", compact=True,
        )
        yield Button(self._channels_text(None), id="filter-channels", compact=True)
        yield Input(placeholder="filter by ssid…", id="filter-text", compact=True)

    def focus_text(self) -> None:
        self.query_one("#filter-text", Input).focus()

    def focus_encryption(self) -> None:
        select = self.query_one("#filter-encryption", Select)
        select.focus()
        select.expanded = True

    def set_channels(self, active: Optional[List[int]]) -> None:
        button = self.query_one("#filter-channels", Button)
        button.label = self._channels_text(active)
        button.refresh(layout=True)   # label reactive alone does not repaint the compact button

    def _channels_text(self, active: Optional[List[int]]) -> str:
        chans = set(active if active is not None else self._supported)
        parts = []
        for short, band in (("2.4G", [c for c in self._supported if c <= 14]),
                            ("5G", [c for c in self._supported if c > 14])):
            picked = [c for c in band if c in chans]
            if picked:
                parts.append(short if set(picked) == set(band) else band_ranges(picked)[0][1])
        return "[u]C[/u]hannels: " + (" + ".join(parts) or "none")

    def action_leave(self) -> None:
        self._focus_table()

    def on_input_submitted(self) -> None:
        self._focus_table()

    def on_input_changed(self) -> None:
        self._emit_scan_filter()

    def on_select_changed(self) -> None:
        self._emit_scan_filter()
        self._focus_table()

    def on_button_pressed(self) -> None:
        self.post_message(self.EditChannels())

    def _emit_scan_filter(self) -> None:
        text = self.query_one("#filter-text", Input).value
        encryption = self.query_one("#filter-encryption", Select).value
        self.post_message(self.ScanFilterChanged(ScanFilter(text=text, encryption=encryption)))

    def _focus_table(self) -> None:
        tables = self.screen.query("#ap-table")
        if tables:
            tables.first().focus()
