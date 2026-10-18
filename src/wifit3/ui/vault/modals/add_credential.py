from __future__ import annotations

from dataclasses import dataclass
import re

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select


_BSSID_RE = re.compile(r"^[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


@dataclass(frozen=True)
class ManualCredential:
    kind: str
    ssid: str
    bssid: str
    value: str


class AddCredentialModal(ModalScreen[ManualCredential | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    AddCredentialModal { align: center middle; }
    AddCredentialModal #dialog {
        width: 58; height: auto; max-height: 90%;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    AddCredentialModal #title { width: 100%; text-align: center; text-style: bold; }
    AddCredentialModal .field-label { margin-top: 1; color: $text-muted; }
    AddCredentialModal #status { height: auto; min-height: 1; color: $error; margin-top: 1; }
    AddCredentialModal #buttons { height: auto; align: right middle; }
    AddCredentialModal #buttons Button { margin-left: 1; }
    """

    def __init__(self, ssid: str = "", bssid: str = "") -> None:
        super().__init__()
        self._ssid = ssid
        self._bssid = bssid

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Add Credential", id="title")
            yield Label("Network", classes="field-label")
            yield Input(self._ssid, placeholder="SSID", id="ssid")
            yield Label("BSSID", classes="field-label")
            yield Input(self._bssid, placeholder="aa:bb:cc:dd:ee:ff", id="bssid")
            yield Label("Credential type", classes="field-label")
            yield Select(
                [("WPA PSK", "wpa_psk"), ("WPS PIN", "wps_pin"), ("WEP key", "wep")],
                value="wpa_psk",
                allow_blank=False,
                id="kind",
            )
            yield Label("Credential", classes="field-label", id="value-label")
            yield Input(placeholder="8–63 character passphrase or 64 hex digits", id="value")
            yield Label("", id="status")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Add", variant="primary", id="add")

    @on(Select.Changed, "#kind")
    def _kind_changed(self, event: Select.Changed) -> None:
        value = self.query_one("#value", Input)
        if event.value == "wps_pin":
            value.placeholder = "4 or 8 digit PIN"
        elif event.value == "wep":
            value.placeholder = "5/13 ASCII characters or 10/26 hex digits"
        else:
            value.placeholder = "8–63 character passphrase or 64 hex digits"

    @on(Button.Pressed, "#cancel")
    def _cancel_pressed(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#add")
    def _add_pressed(self) -> None:
        ssid = self.query_one("#ssid", Input).value.strip()
        bssid = self.query_one("#bssid", Input).value.strip().lower()
        kind = self.query_one("#kind", Select).value
        value = self.query_one("#value", Input).value
        error = self._validate(ssid, bssid, kind, value)
        if error:
            self.query_one("#status", Label).update(error)
            return
        self.dismiss(ManualCredential(str(kind), ssid, bssid, value))

    @staticmethod
    def _validate(ssid: str, bssid: str, kind, value: str) -> str | None:
        if not ssid or len(ssid.encode("utf-8")) > 32:
            return "SSID must be 1–32 bytes"
        if not _BSSID_RE.fullmatch(bssid):
            return "Enter a colon-separated BSSID"
        if kind == "wpa_psk":
            encoded = value.encode("utf-8")
            if not (8 <= len(encoded) <= 63 or len(value) == 64 and _HEX_RE.fullmatch(value)):
                return "WPA PSK must be 8–63 bytes or 64 hex digits"
        elif kind == "wps_pin":
            if not (value.isdigit() and len(value) in (4, 8)):
                return "WPS PIN must contain 4 or 8 digits"
        elif kind == "wep":
            ascii_key = len(value) in (5, 13) and value.isascii()
            hex_key = len(value) in (10, 26) and _HEX_RE.fullmatch(value)
            if not (ascii_key or hex_key):
                return "WEP key must be 5/13 ASCII characters or 10/26 hex digits"
        return None

    def action_cancel(self) -> None:
        self.dismiss(None)
