"""The Scanner's filter bar and the ScanFilter predicate."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import HorizontalScroll
from textual.message import Message
from textual.widgets import Button, Checkbox, Input, Label, Select

from wifit3.models import AccessPoint
from wifit3.persist.config import Config
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
    hide_silenced: bool = False

    def matches(self, ap: AccessPoint, *, ssid: Optional[str] = None) -> bool:
        """``ssid`` overrides ap.ssid so a hidden AP is searchable by its guessed name."""
        if self.hide_silenced and Config.is_silenced(ap.bssid):
            return False
        if not self.encryption.matches(ap):
            return False
        # The scanner runs this over every AP every tick, and the box is usually empty.
        return not self.text or text_matches(self.text, ap.bssid, ssid or ap.ssid)


class FilterBar(HorizontalScroll):
    """One row above the AP table: text query, encryption select, channels button, hide-silenced."""

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    FilterBar {
        height: auto;
        padding: 0 1;
        overflow-x: auto;
        overflow-y: hidden;
        border: round $primary;
        border-title-color: $primary;
        border-title-style: bold;
    }
    FilterBar > Label { margin-right: 1; color: $text-muted; }
    FilterBar > #filter-encryption-label,
    FilterBar > #filter-channels-label { text-wrap: nowrap; text-overflow: clip; }
    FilterBar > #filter-encryption { width: 9; margin-right: 2; }
    FilterBar > #filter-channels { margin-right: 2; }
    FilterBar > Input { width: 1fr; min-width: 16; max-width: 32; }
    FilterBar > #filter-hide-silenced { margin-left: 2; border: none; }
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
        enc = Label("[u]E[/u]ncryption", id="filter-encryption-label")
        enc.ALLOW_SELECT = False
        yield enc
        yield Select(
            [(f.value, f) for f in EncryptionFilter], value=EncryptionFilter.ALL,
            allow_blank=False, id="filter-encryption", compact=True,
        )
        channels_label = Label("[u]C[/u]hannel", id="filter-channels-label")
        channels_label.ALLOW_SELECT = False
        yield channels_label
        yield Button(self._channels_text(None), id="filter-channels", compact=True)
        yield Input(placeholder="filter by ssid…", id="filter-text", compact=True)
        yield Checkbox("Hide silenced", value=Config.hide_silenced,
                       id="filter-hide-silenced", compact=True)

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
        return " + ".join(parts) or "none"

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

    def on_checkbox_changed(self) -> None:
        self._emit_scan_filter()

    def _emit_scan_filter(self) -> None:
        text = self.query_one("#filter-text", Input).value
        encryption = self.query_one("#filter-encryption", Select).value
        hide_silenced = self.query_one("#filter-hide-silenced", Checkbox).value
        self.post_message(self.ScanFilterChanged(
            ScanFilter(text=text, encryption=encryption, hide_silenced=hide_silenced)))

    def _focus_table(self) -> None:
        tables = self.screen.query("#ap-table")
        if tables:
            tables.first().focus()
