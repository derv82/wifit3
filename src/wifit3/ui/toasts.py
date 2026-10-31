"""Toast notifications for AP events."""
from __future__ import annotations

from enum import IntEnum
from typing import Dict

from rich.markup import escape
from textual.app import App

from wifit3.campaigns.pin import EMPTY_PIN_LABEL
from wifit3.ui.encryption_format import wep_key_hex_and_ascii
from wifit3.watchers.ap_watcher import (
    ApEvent, HandshakeCaptured, PmkidCaptured, WepKeyRecovered, WpsPbcPskRecovered, WpsPinRecovered,
    WpsPskRecovered,
)

TOAST_TIMEOUT_S = 6


class ShownCaptureRank(IntEnum):
    CHALLENGE = 1    # M1+M2, no confirmation from AP
    AUTHORIZED = 2   # PMKID/M3/M4, AP authorized the capture


class Toasts:
    """Shows a toast for each AP event worth one. Captures toast only when they outrank the
    capture already shown for that AP."""

    def __init__(self, app: App) -> None:
        self._app = app
        self._shown_capture_ranks: Dict[str, ShownCaptureRank] = {}

    def ap_event(self, event: ApEvent, guessid: str) -> None:
        if event.ssid and event.ssid != "<hidden>":
            name = f"[bold]{escape(event.ssid)}[/bold]"
            location = (f"on channel [bold]{event.channel}[/bold] "
                        f"[dim bold](BSSID: {event.bssid})[/dim bold]")
        else:
            hidden_name = f"{escape(guessid)}?" if guessid else "<Hidden>"
            name = f"[bold]{event.bssid}[/bold] ([italic]{hidden_name}[/italic])"
            location = f"on channel [bold]{event.channel}[/bold]"

        if isinstance(event, HandshakeCaptured):
            if event.authorized:
                rank = ShownCaptureRank.AUTHORIZED
            else:
                rank = ShownCaptureRank.CHALLENGE
            self._show_capture(event.bssid, rank, f"Handshake captured ({event.pair_label})",
                               f"{name} {location}")
        elif isinstance(event, PmkidCaptured):
            self._show_capture(event.bssid, ShownCaptureRank.AUTHORIZED, "PMKID captured (M1)",
                               f"{name} {location}")
        elif isinstance(event, WepKeyRecovered):
            self._app.notify(f"{name}: {escape(wep_key_hex_and_ascii(event.key_hex))}",
                             title="WEP key recovered", timeout=TOAST_TIMEOUT_S)
        elif isinstance(event, WpsPinRecovered):
            self._app.notify(f"{name}: {escape(event.pin or EMPTY_PIN_LABEL)}",
                             title="WPS PIN recovered", timeout=TOAST_TIMEOUT_S)
        elif isinstance(event, WpsPskRecovered):
            self._app.notify(f'{name}: "{escape(event.psk)}"',
                             title="WPS PSK recovered", timeout=TOAST_TIMEOUT_S)
        elif isinstance(event, WpsPbcPskRecovered):
            self._app.notify(f'{name}: "{escape(event.psk)}"',
                             title="WPS PSK recovered (PushButton)", timeout=TOAST_TIMEOUT_S)

    def _show_capture(self, bssid: str, rank: ShownCaptureRank, title: str, message: str) -> None:
        shown_rank = self._shown_capture_ranks.get(bssid)
        if shown_rank is not None and rank <= shown_rank:
            return
        self._shown_capture_ranks[bssid] = rank
        self._app.notify(message, title=title, timeout=TOAST_TIMEOUT_S)
