"""Live preview + offset-tuning harness for the WiFFy assistant. No hardware, no UAC, no install.

    uv run python scripts/wiffy_preview.py
    # or hot-reload while editing wiffy.py / the .ans frames:
    uv run textual run --dev scripts/wiffy_preview.py

It pushes the real BringupProgressModal over a fake dimmed splash and slides WiFFy in, so what you
see is exactly what the Windows install shows. Keys: [i] re-show  [o] slide out ok  [e] slide out
error  [q] quit. To tune the text-hole overlay, set _HOLE_* / the #wiffy-text background in
src/wifit3/ui/wiffy.py (a loud `background: magenta` makes misalignment obvious), then flip back.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Label, Static

from wifit3.ui.screens.bringup_progress import BringupProgressModal
from wifit3.ui.wiffy import INSTALL_LINES


class _FakeSplash(Screen):
    """Stand-in for the real splash so the modal's 60% dim (and WiFFy's transparent cells) have
    something to sit over."""

    def compose(self) -> ComposeResult:
        with Vertical():
            with Center():
                yield Static("[bold green]wifit3[/]  [dim green]// wireless auditor[/]")
            for ssid in ("HackThePlanet", "linksys", "NETGEAR-5G", "xfinitywifi", "Pretty Fly 4 WiFi"):
                yield Label(f"  [green]▮▮▮[/] {ssid}   [dim]WPA2[/]")


class WiffyPreview(App):
    CSS = "Screen { background: $background; }"
    # priority=True so quit fires even while the modal is the focused screen (else it swallows them).
    BINDINGS = [("i", "show", "Re-show"), ("o", "out_ok", "Slide out ok"),
                ("e", "out_err", "Slide out error"),
                Binding("q", "quit", "Quit", priority=True),
                Binding("ctrl+c", "quit", "Quit", priority=True)]

    def on_mount(self) -> None:
        self.push_screen(_FakeSplash())
        self.action_show()

    def action_show(self) -> None:
        self._modal = BringupProgressModal("Installing WinUSB driver for RTL8814AU (Alfa AWUS1900)…")
        self.push_screen(self._modal)
        self._modal.set_status("wdi-simple: waiting for Windows to install the driver…")
        self.call_after_refresh(
            lambda: self._modal.run_worker(
                self._modal.show_assistant(*INSTALL_LINES, intro_delay=0.4), name="wiffy-show"))

    def action_out_ok(self) -> None:
        self._modal.run_worker(self._end(True), name="wiffy-end")

    def action_out_err(self) -> None:
        self._modal.run_worker(self._end(False), name="wiffy-end")

    async def _end(self, ok: bool) -> None:
        await self._modal.hide_assistant(ok)


if __name__ == "__main__":
    WiffyPreview().run()
