from __future__ import annotations

from dataclasses import dataclass, field
import re

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select


_BSSID_RE = re.compile(r"^[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


@dataclass(frozen=True)
class ManualCredential:
    kind: str
    ssid: str
    bssid: str
    value: str = ""
    username: str = ""
    password: str = ""
    eap_settings: dict[str, str] = field(default_factory=dict)


class AddCredentialModal(ModalScreen[ManualCredential | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    AddCredentialModal { align: center middle; }
    AddCredentialModal #dialog {
        width: 58; height: 90%;
        border: thick $primary; background: $surface; padding: 1 2;
        overflow-y: auto;
    }
    AddCredentialModal #title { width: 100%; text-align: center; text-style: bold; }
    AddCredentialModal .field-label { margin-top: 1; color: $text-muted; }
    AddCredentialModal #eap-fields { display: none; height: auto; }
    AddCredentialModal #status { height: auto; min-height: 1; color: $error; margin-top: 1; }
    AddCredentialModal #buttons { height: auto; align: right middle; }
    AddCredentialModal #buttons Button { margin-left: 1; }
    """

    def __init__(self, ssid: str = "", bssid: str = "") -> None:
        super().__init__()
        self._ssid = ssid
        self._bssid = bssid

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label("Add Credential", id="title")
            yield Label("Network", classes="field-label")
            yield Input(self._ssid, placeholder="SSID", id="ssid")
            yield Label("BSSID", classes="field-label")
            yield Input(self._bssid, placeholder="aa:bb:cc:dd:ee:ff", id="bssid")
            yield Label("Credential type", classes="field-label")
            yield Select(
                [("WPA PSK", "wpa_psk"), ("WPS PIN", "wps_pin"), ("WEP key", "wep"),
                 ("EAP credentials", "eap")],
                value="wpa_psk",
                allow_blank=False,
                id="kind",
            )
            yield Label("Credential", classes="field-label", id="value-label")
            yield Input(placeholder="8–63 character passphrase or 64 hex digits", id="value")
            with Vertical(id="eap-fields"):
                yield Label("Username", classes="field-label")
                yield Input(placeholder="identity", id="username")
                yield Label("Password", classes="field-label")
                yield Input(password=True, placeholder="password", id="password")
                yield Label("EAP method", classes="field-label")
                yield Input(placeholder="PEAP, TTLS, TLS…", id="eap-method")
                yield Label("Phase 2", classes="field-label")
                yield Input(placeholder="MSCHAPv2, PAP…", id="eap-phase2")
                yield Label("Anonymous identity", classes="field-label")
                yield Input(placeholder="optional", id="eap-anonymous-identity")
            yield Label("", id="status")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Add", variant="primary", id="add")

    @on(Select.Changed, "#kind")
    def _kind_changed(self, event: Select.Changed) -> None:
        kind = str(event.value)
        value = self.query_one("#value", Input)
        self.query_one("#value-label", Label).display = kind != "eap"
        value.display = kind != "eap"
        self.query_one("#eap-fields", Vertical).display = kind == "eap"
        if kind == "wps_pin":
            value.placeholder = "4 or 8 digit PIN"
        elif kind == "wep":
            value.placeholder = "5/13 ASCII characters or 10/26 hex digits"
        else:
            value.placeholder = "8–63 character passphrase or 64 hex digits"

    @on(Button.Pressed, "#cancel")
    def _cancel_pressed(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#value")
    @on(Input.Submitted, "#password")
    def _value_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._add_pressed()

    @on(Button.Pressed, "#add")
    def _add_pressed(self) -> None:
        ssid = self.query_one("#ssid", Input).value.strip()
        bssid = self.query_one("#bssid", Input).value.strip().lower()
        kind = self.query_one("#kind", Select).value
        value = self.query_one("#value", Input).value
        username = self.query_one("#username", Input).value
        password = self.query_one("#password", Input).value
        eap_settings = {
            "method": self.query_one("#eap-method", Input).value.strip(),
            "phase2": self.query_one("#eap-phase2", Input).value.strip(),
            "anonymous_identity": self.query_one("#eap-anonymous-identity", Input).value,
        }
        error = self._validate(ssid, bssid, kind, value, username, password)
        if error:
            self.query_one("#status", Label).update(error)
            return
        self.dismiss(ManualCredential(
            str(kind), ssid, bssid, value, username, password,
            {name: setting for name, setting in eap_settings.items() if setting},
        ))

    @staticmethod
    def _validate(
        ssid: str, bssid: str, kind, value: str, username: str = "", password: str = ""
    ) -> str | None:
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
        elif kind == "eap":
            if not username or not password:
                return "EAP username and password are required"
            if any(character in username for character in ":\r\n") or "\r" in password or "\n" in password:
                return "EAP credentials must be one line; username cannot contain a colon"
        return None

    def action_cancel(self) -> None:
        self.dismiss(None)
