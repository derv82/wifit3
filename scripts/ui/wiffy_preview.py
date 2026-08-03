"""Live preview + offset-tuning harness for the WiFFy assistant. No hardware, no UAC, no install.

    uv run python scripts/ui/wiffy_preview.py
    # or hot-reload while editing wiffy.py / the .ans frames:
    uv run textual run --dev scripts/ui/wiffy_preview.py

It pushes the real BringupProgressModal over a fake dimmed splash and slides WiFFy in, so what you
see is exactly what the Windows install shows. Keys: [space] skip to the next message (great for
eyeballing every message fast)  [i] install pack  [u] uninstall pack  [o] slide out ok  [e] slide
out error  [q] quit. To tune the text-hole overlay, set _HOLE_* / the #wiffy-text background in
src/wifit3/ui/wiffy.py (a loud `background: magenta` makes misalignment obvious), then flip back.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Label, Static

from wifit3.ui.screens.bringup_progress import BringupProgressModal
from wifit3.ui.wiffy import INSTALL_LINES, UNINSTALL_LINES, WiffyAssistant


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
    # All priority=True: over the modal the app has no focused widget, so non-priority app bindings
    # never fire. Priority routes the key to the app first, regardless of focus.
    BINDINGS = [Binding("space", "skip", "Next message", priority=True),
                Binding("i", "show", "Install msgs", priority=True),
                Binding("u", "show_uninstall", "Uninstall msgs", priority=True),
                Binding("o", "out_ok", "Slide out ok", priority=True),
                Binding("e", "out_err", "Slide out error", priority=True),
                Binding("q", "quit", "Quit", priority=True),
                Binding("ctrl+c", "quit", "Quit", priority=True)]

    def on_mount(self) -> None:
        self.push_screen(_FakeSplash())
        self.action_show()

    def _open(self, title: str, status: str, lines) -> None:
        if getattr(self, "_modal", None) is not None and self._modal.is_mounted:
            self._modal.dismiss()          # reopen replaces, so install/uninstall don't stack
        self._modal = BringupProgressModal(title)
        self.push_screen(self._modal)
        self._modal.set_status(status)
        self.call_after_refresh(
            lambda: self._modal.run_worker(
                self._modal.show_assistant(*lines, intro_delay=0.4), name="wiffy-show"))

    def action_show(self) -> None:
        self._open("Installing WinUSB driver for RTL8814AU (Alfa AWUS1900)…",
                   "wdi-simple: waiting for Windows to install the driver…", INSTALL_LINES)

    def action_show_uninstall(self) -> None:
        self._open("Removing wifit3 driver for RTL8814AU (Alfa AWUS1900)…",
                   "pnputil: removing the WinUSB driver…", UNINSTALL_LINES)

    def action_skip(self) -> None:
        for w in self._modal.query(WiffyAssistant):
            w.skip()

    def action_out_ok(self) -> None:
        self._modal.run_worker(self._end(True), name="wiffy-end")

    def action_out_err(self) -> None:
        self._modal.run_worker(self._end(False), name="wiffy-end")

    async def _end(self, ok: bool) -> None:
        await self._modal.hide_assistant(ok)


if __name__ == "__main__":
    WiffyPreview().run()
