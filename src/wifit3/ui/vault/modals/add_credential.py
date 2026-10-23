import re
from dataclasses import dataclass
from typing import Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select

from wifit3.crack.passwords import is_valid_psk
from wifit3.models import CaptureType

_BSSID_RE = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}")
_PSK_HEX_RE = re.compile(r"[0-9a-fA-F]{64}")
_WPS_PIN_RE = re.compile(r"[0-9]{4}|[0-9]{8}")
_WEP_HEX_RE = re.compile(r"[0-9a-fA-F]{10}|[0-9a-fA-F]{26}")

_PLACEHOLDERS = {
    CaptureType.WPA_PSK: "8-63 character passphrase or 64 hex digits",
    CaptureType.WPS_PIN: "4 or 8 digit PIN",
    CaptureType.WEP: "5/13 ASCII characters or 10/26 hex digits",
}


@dataclass(frozen=True)
class ManualCredential:
    """A credential typed in by the user; ``kind`` is WPA_PSK, WPS_PIN or WEP."""
    kind: CaptureType
    ssid: str
    bssid: str
    value: str


def credential_error(ssid: str, bssid: str, kind: CaptureType, value: str) -> Optional[str]:
    """Why the typed credential can't be saved, or None when it is valid."""
    if not ssid or len(ssid.encode("utf-8")) > 32:
        return "SSID must be 1-32 bytes"
    if not _BSSID_RE.fullmatch(bssid):
        return "Enter a colon-separated BSSID"
    if kind == CaptureType.WPA_PSK:
        if not (is_valid_psk(value) or _PSK_HEX_RE.fullmatch(value)):
            return "WPA PSK must be 8-63 bytes or 64 hex digits"
    elif kind == CaptureType.WPS_PIN:
        if not _WPS_PIN_RE.fullmatch(value):
            return "WPS PIN must contain 4 or 8 digits"
    elif kind == CaptureType.WEP:
        if not ((len(value) in (5, 13) and value.isascii()) or _WEP_HEX_RE.fullmatch(value)):
            return "WEP key must be 5/13 ASCII characters or 10/26 hex digits"
    return None


class AddCredentialModal(ModalScreen[Optional[ManualCredential]]):
    """Type in a WPA PSK, WPS PIN or WEP key for an AP; dismisses with it, or None."""

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
                [("WPA PSK", CaptureType.WPA_PSK), ("WPS PIN", CaptureType.WPS_PIN),
                 ("WEP key", CaptureType.WEP)],
                value=CaptureType.WPA_PSK, allow_blank=False, id="kind",
            )
            yield Label("Credential", classes="field-label")
            yield Input(placeholder=_PLACEHOLDERS[CaptureType.WPA_PSK], id="value")
            yield Label("", id="status")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Add", variant="primary", id="add")

    @on(Select.Changed, "#kind")
    def _kind_changed(self, event: Select.Changed) -> None:
        self.query_one("#value", Input).placeholder = _PLACEHOLDERS[CaptureType(event.value)]

    @on(Input.Submitted, "#value")
    @on(Button.Pressed, "#add")
    def _add(self) -> None:
        ssid = self.query_one("#ssid", Input).value.strip()
        bssid = self.query_one("#bssid", Input).value.strip().lower()
        kind = CaptureType(self.query_one("#kind", Select).value)
        value = self.query_one("#value", Input).value
        error = credential_error(ssid, bssid, kind, value)
        if error:
            self.query_one("#status", Label).update(error)
            return
        self.dismiss(ManualCredential(kind, ssid, bssid, value))

    @on(Button.Pressed, "#cancel")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)
