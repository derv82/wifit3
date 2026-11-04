"""Pick the SSIDs to guess at a hidden AP, and which methods send them."""
from pathlib import Path
from typing import Iterable, Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, TextArea

from wifit3.campaigns.decloak import (
    ASSOC_TIMEOUT_S,
    PROBE_RESPONSE_WAIT_S,
    DecloakInput,
    candidates_from_sibling,
)
from wifit3.dot11 import SSID_MAX_OCTETS
from wifit3.persist.config import Config
from wifit3.ui.path_picker import PathInput

TRUNCATE_SECONDS = (300, 1200, 3600)


def sendable_ssids(names: Iterable[str]) -> list[str]:
    """``names`` minus blanks, duplicates and anything over SSID_MAX_OCTETS, order kept."""
    sendable: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name.strip() and name not in seen and len(name.encode("utf-8")) <= SSID_MAX_OCTETS:
            seen.add(name)
            sendable.append(name)
    return sendable


def candidates_from_base(base_ssid: str, remembered: str) -> list[str]:
    return sendable_ssids([remembered, *candidates_from_sibling(base_ssid)])


def line_count(path: Path) -> int:
    """Counted a megabyte at a time: a wordlist can be gigabytes."""
    count, last = 0, b"\n"
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            count += chunk.count(b"\n")
            last = chunk[-1:]
    return count + (last != b"\n")


def wordlist_head(path: Path, limit: int) -> list[str]:
    """The first ``limit`` non-blank lines."""
    names: list[str] = []
    if limit <= 0:
        return names
    with path.open("rb") as handle:
        for raw in handle:
            try:
                name = raw.rstrip(b"\r\n").decode("utf-8")
            except UnicodeDecodeError:
                continue
            if name.strip():
                names.append(name)
                if len(names) >= limit:
                    break
    return names


def seconds_per_ssid(methods: DecloakInput) -> float:
    """Airtime one SSID costs across the enabled methods; 0 when none are enabled."""
    return (PROBE_RESPONSE_WAIT_S * methods.use_directed_probe
            + PROBE_RESPONSE_WAIT_S * methods.use_broadcast_probe
            + ASSOC_TIMEOUT_S * methods.use_authassoc_probe)


def _eta_text(seconds: float) -> str:
    if seconds >= 172800:
        return f"{seconds / 86400:.1f} d"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    if seconds >= 60:
        return f"{seconds / 60:.0f} min"
    return f"{seconds:.0f}s"


class _TruncateButton(Button):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"{seconds // 60} min", compact=True)
        self.seconds = seconds


class DecloakInputModal(ModalScreen[Optional[DecloakInput]]):
    """Select candidate SSIDs to probe for (guess) and the probing method."""

    BINDINGS = [Binding("escape", "cancel", "Anuluj", show=True)]

    DEFAULT_CSS = """
    DecloakInputModal { align: center middle; }
    DecloakInputModal #dialog {
        width: 64; height: auto; max-height: 90%; overflow-y: auto;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    DecloakInputModal #dialog > Label { width: 100%; }
    DecloakInputModal #title {
        content-align: center middle; text-style: bold; color: $accent; padding-bottom: 1;
    }
    DecloakInputModal #hint { color: $text-muted; }
    DecloakInputModal #sources { height: auto; }
    DecloakInputModal #sources > * { width: 1fr; }
    DecloakInputModal TextArea { height: 8; margin-bottom: 1; }
    DecloakInputModal #methods { height: auto; }
    DecloakInputModal #methods Checkbox {
        border: none; height: 1; padding: 0 1; background: transparent;
    }
    DecloakInputModal #truncate { height: 1; }
    DecloakInputModal #truncate Label { padding: 0 1 0 0; color: $text-muted; }
    DecloakInputModal #truncate Button { background: $panel; }
    DecloakInputModal #truncate Button.-primary { background: $primary; }
    DecloakInputModal #status {
        content-align: center middle; color: $text-muted; padding-top: 1;
    }
    DecloakInputModal #status.-error { color: $error; }
    DecloakInputModal #button-row { height: auto; align: center middle; padding-top: 1; }
    """

    wordlist_lines: reactive[int] = reactive(0)
    truncate_seconds: reactive[Optional[int]] = reactive(None)

    def __init__(self, bssid: str, base_ssid: str) -> None:
        super().__init__()
        self._bssid = bssid
        self._base_ssid = base_ssid

    def compose(self) -> ComposeResult:
        defaults = DecloakInput()
        wordlist = PathInput("", title="Wybierz słownik", placeholder="plik słownika",
                             id="wordlist")
        wordlist.tooltip = "Wczytaj listę SSID z pliku tekstowego (1 SSID na linię)"
        with Vertical(id="dialog"):
            yield Label(f"Odsłoń ukrytą sieć ({self._bssid})", id="title")
            yield Label("Wpisz bazowy SSID [i]lub[/i] wczytaj listę SSID z pliku:", id="hint")
            with Horizontal(id="sources"):
                yield Input(self._base_ssid, placeholder="bazowy SSID", id="base-ssid",
                            tooltip="Wpisz wspólny [i]prefiks[/i] – sufiksy "
                                    "uzupełnią się automatycznie")
                yield wordlist
            yield TextArea("\n".join(self._guesses(self._base_ssid)), id="candidates")
            with Vertical(id="methods"):
                yield Checkbox(
                    "Sondy kierowane", defaults.use_directed_probe, id="use-directed",
                    tooltip="Wysyła sondy bezpośrednio do AP (odsłania tylko AP docelowy). "
                            "Z retransmisjami/ACK, szybkie.")
                yield Checkbox(
                    "Sondy rozgłoszeniowe", defaults.use_broadcast_probe,
                    id="use-broadcast",
                    tooltip="Wysyła sondę rozgłoszeniową (do wszystkich AP), może odsłonić "
                            "też inne AP. Bez retransmisji/ACK (część ginie), szybkie.")
                yield Checkbox(
                    "Uwierzytelnianie + asocjacja", defaults.use_authassoc_probe,
                    id="use-authassoc",
                    tooltip="Uwierzytelnia się i asocjuje z docelowym AP dla każdego "
                            "zgadywanego SSID. Retransmisje/ACK, ale wolniejsze niż sondy.")
            with Horizontal(id="truncate"):
                yield Label("Skróć do:")
                for seconds in TRUNCATE_SECONDS:
                    yield _TruncateButton(seconds)
            yield Label("", id="status")
            with Horizontal(id="button-row"):
                yield Button("Odsłoń", variant="primary", id="accept")
                yield Button("Anuluj", id="cancel")

    def watch_wordlist_lines(self) -> None:
        self._update_estimate()

    def watch_truncate_seconds(self) -> None:
        self._update_estimate()

    @on(Checkbox.Changed)
    @on(TextArea.Changed)
    def _method_or_list_edited(self, event: Message) -> None:
        self._update_estimate()

    @on(Input.Changed, "#base-ssid")
    def _base_ssid_edited(self, event: Input.Changed) -> None:
        if event.value:
            self.query_one("#wordlist", Input).value = ""
        self.query_one("#candidates", TextArea).load_text(
            "\n".join(self._guesses(event.value)))

    @on(Input.Changed, "#wordlist")
    def _wordlist_picked(self, event: Input.Changed) -> None:
        path = Path(event.value)
        if event.value and not path.is_file():
            return                                  # still being typed
        self.query_one("#candidates", TextArea).display = not event.value
        if not event.value:
            self.wordlist_lines = 0
            return
        try:
            lines = line_count(path)
        except OSError as exc:
            self._set_status(f"{path.name}: {exc.strerror or exc}", error=True)
            return
        if lines * seconds_per_ssid(self._picked_methods()) > TRUNCATE_SECONDS[-1]:
            self.truncate_seconds = TRUNCATE_SECONDS[0]
        self.wordlist_lines = lines

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if isinstance(event.button, _TruncateButton):
            seconds = event.button.seconds
            self.truncate_seconds = None if self.truncate_seconds == seconds else seconds
        elif event.button.id == "accept":
            self._accept()
        elif event.button.id == "cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _guesses(self, base_ssid: str) -> list[str]:
        return candidates_from_base(base_ssid, Config.decloaked_ssid(self._bssid) or "")

    def _picked_methods(self) -> DecloakInput:
        return DecloakInput([], self.query_one("#use-directed", Checkbox).value,
                            self.query_one("#use-broadcast", Checkbox).value,
                            self.query_one("#use-authassoc", Checkbox).value)

    def _set_status(self, message: str, *, error: bool = False) -> None:
        status = self.query_one("#status", Label)
        status.update(message)
        status.set_class(error, "-error")

    def _update_estimate(self) -> None:
        methods = self._picked_methods()
        per_ssid = seconds_per_ssid(methods)
        written = [line for line in self.query_one("#candidates", TextArea).text.splitlines()
                   if line.strip()]
        sendable = sendable_ssids(written)
        ssid_count = self.wordlist_lines or len(sendable)
        untruncated_eta = ssid_count * per_ssid

        would_shorten = [seconds for seconds in TRUNCATE_SECONDS if untruncated_eta > seconds]
        truncate = self.truncate_seconds if self.truncate_seconds in would_shorten else None
        for button in self.query(_TruncateButton):
            button.display = button.seconds in would_shorten
            button.variant = "primary" if button.seconds == truncate else "default"
        self.query_one("#truncate").display = bool(would_shorten)

        if not per_ssid:
            self._set_status("Wybierz minimum jedną metodę", error=True)
            return
        if truncate:
            ssid_count = min(ssid_count, int(truncate / per_ssid))
        if not ssid_count:
            self._set_status("Brak kandydatów SSID do wysłania", error=True)
            return
        requests = ssid_count * sum((methods.use_directed_probe, methods.use_broadcast_probe,
                                     methods.use_authassoc_probe))
        status = (f"{ssid_count:,} SSID, {requests:,} żądań, "
                  f"ETA {_eta_text(ssid_count * per_ssid)}")
        if truncate:
            status += f" (skrócone do {truncate // 60} min)"
        if not self.wordlist_lines and len(written) > len(sendable):
            status += f" (odrzucono {len(written) - len(sendable):,})"
        self._set_status(status)

    def _accept(self) -> None:
        methods = self._picked_methods()
        per_ssid = seconds_per_ssid(methods)
        if not per_ssid:
            self._set_status("Wybierz minimum jedną metodę", error=True)
            return
        limit = int(self.truncate_seconds / per_ssid) if self.truncate_seconds else None
        if self.wordlist_lines:
            path = Path(self.query_one("#wordlist", Input).value)
            try:
                methods.candidate_ssids = sendable_ssids(
                    wordlist_head(path, limit or self.wordlist_lines))
            except OSError as exc:
                self._set_status(f"{path.name}: {exc.strerror or exc}", error=True)
                return
        else:
            methods.candidate_ssids = sendable_ssids(
                self.query_one("#candidates", TextArea).text.splitlines())[:limit]
        if not methods.candidate_ssids:
            self._set_status("Brak kandydatów SSID do wysłania", error=True)
            return
        self.dismiss(methods)
