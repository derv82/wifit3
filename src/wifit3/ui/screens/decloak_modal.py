"""Candidate-SSID editor shared by Scanner and Focus decloak actions."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, TextArea

from wifit3.campaigns.decloak import (
    SSID_MAX_OCTETS, candidates_from_sibling, named_sibling_ssid,
)
from wifit3.models import AccessPoint
from wifit3.persist.config import Config
from wifit3.ui.path_picker import PathPickerModal
from wifit3.wlan.array import WlanArray

SSID_TOKEN = "$ssid"


def expand_candidates(text: str, base: str) -> list[str]:
    """Expand templates into the deduplicated, valid SSIDs the sweep will send."""
    candidates: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if SSID_TOKEN in line and not base:
            raise ValueError("$ssid requires a base SSID")
        candidate = line.replace(SSID_TOKEN, base)
        if (candidate and candidate not in seen
                and len(candidate.encode("utf-8")) <= SSID_MAX_OCTETS):
            seen.add(candidate)
            candidates.append(candidate)
    return candidates


def wordlist_ssids(contents: str) -> list[str]:
    """SSIDs from a loaded wordlist, excluding blank and comment lines."""
    return [line for line in contents.splitlines()
            if line and not line.lstrip().startswith("#")]


def decloak_candidate_defaults(array: WlanArray, ap: AccessPoint) -> tuple[str, list[str]]:
    """The template base and ordered initial candidates for an AP."""
    base = named_sibling_ssid(array, ap)
    remembered = Config.decloaked_ssid(ap.bssid)
    candidates: list[str] = []
    for candidate in ([remembered] if remembered else []) + candidates_from_sibling(base):
        if candidate not in candidates:
            candidates.append(candidate)
    return base, candidates


class DecloakCandidateModal(ModalScreen[Optional[list[str]]]):
    """Edit and confirm the exact candidate SSIDs a decloak sweep will send."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=True)]

    DEFAULT_CSS = """
    DecloakCandidateModal { align: center middle; }
    DecloakCandidateModal #dialog {
        width: 64; height: auto; min-height: 20; max-height: 90%;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    DecloakCandidateModal #title { width: 1fr; content-align: center middle; margin-bottom: 1; text-style: bold; }
    DecloakCandidateModal #hint { color: $text-muted; margin-bottom: 1; height: auto; }
    DecloakCandidateModal .row { height: auto; margin-bottom: 1; }
    DecloakCandidateModal .row-label { width: 10; height: 3; content-align: left middle; color: $text-muted; }
    DecloakCandidateModal #base { width: 1fr; }
    DecloakCandidateModal #candidates { height: 1fr; min-height: 3; overflow-y: auto; border: round $primary; margin-bottom: 1; }
    DecloakCandidateModal #warn { color: $text-warning; content-align: center middle; height: auto; display: none; }
    DecloakCandidateModal #button-row { dock: bottom; height: auto; align: center middle; }
    DecloakCandidateModal #button-row Button { margin: 0 1; }
    """

    def __init__(self, bssid: str, base: str, prefill: list[str]) -> None:
        super().__init__()
        self._bssid = bssid
        self._base = base
        self._prefill = prefill

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Decloak {self._bssid}", id="title")
            yield Label(f"One SSID per line. [b]{SSID_TOKEN}[/b] expands to the base below "
                        "(e.g. [b]$ssid Guest[/b]).", id="hint")
            with Horizontal(classes="row"):
                yield Label("$ssid =", classes="row-label")
                yield Input(value=self._base, placeholder="base SSID (optional)", id="base")
            yield TextArea("\n".join(self._prefill), id="candidates")
            yield Label("", id="warn")
            with Horizontal(id="button-row"):
                yield Button("Load wordlist…", variant="default", id="btn-load")
                yield Button("Decloak", variant="primary", id="btn-decloak")
                yield Button("Cancel", variant="default", id="btn-cancel")

    @on(Button.Pressed, "#btn-load")
    def _load(self, event: Button.Pressed) -> None:
        event.stop()
        self.app.push_screen(PathPickerModal(str(Path.home()), title="Load SSID wordlist"),
                             self._append_wordlist)

    async def _append_wordlist(self, path: Optional[Path]) -> None:
        if path is None:
            return
        try:
            contents = await asyncio.to_thread(
                Path(path).read_text, encoding="utf-8", errors="replace"
            )
        except OSError as exc:
            self._set_warn(f"[red]Could not read {escape(str(path))}: "
                           f"{escape(exc.strerror or str(exc))}[/red]")
            return
        area = self.query_one("#candidates", TextArea)
        loaded = wordlist_ssids(contents)
        area.text = "\n".join(filter(None, [area.text.rstrip("\n"), *loaded]))
        self.query_one("#btn-decloak", Button).label = "Decloak"
        self._set_warn(f"Loaded {len(loaded)} SSIDs from {escape(Path(path).name)}")

    @on(Button.Pressed, "#btn-decloak")
    def _accept(self, event: Button.Pressed) -> None:
        area = self.query_one("#candidates", TextArea)
        try:
            candidates = expand_candidates(
                area.text, self.query_one("#base", Input).value
            )
        except ValueError as exc:
            self._set_warn(f"[red]{escape(str(exc))}[/red]")
            return
        if not candidates:
            self._set_warn("[red]No candidate SSIDs to send[/red]")
            return
        exact_text = "\n".join(candidates)
        if area.text != exact_text:
            area.text = exact_text
            self.query_one("#btn-decloak", Button).label = "Confirm Decloak"
            self._set_warn(f"Review the exact {len(candidates)} SSIDs, then confirm")
            return
        self.dismiss(candidates)

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self, event: Button.Pressed) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _set_warn(self, text: str) -> None:
        warn = self.query_one("#warn", Label)
        warn.update(text)
        warn.display = bool(text)
