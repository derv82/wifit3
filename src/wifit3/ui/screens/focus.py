import logging
import re
import time
from pathlib import Path
from typing import Dict, Set

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header, Footer, DataTable, Button, Label, RichLog
from textual.containers import Vertical, Horizontal
from textual.binding import Binding
from rich.text import Text
from rich.markup import escape

from wifit3.engine.models import AccessPoint
from wifit3.engine.pcap import write_pcap

logger = logging.getLogger(__name__)


class FocusView(Screen):
    """The Attack/Focus mode for a specific AP."""

    BINDINGS = [
        Binding("escape", "go_back", "Back to Scanner", show=True),
        Binding("q", "app.quit", "Quit", show=True),
        Binding("s", "save_capture", "Save Capture", show=True),
        Binding("enter", "toggle_client", "Select Client", show=False)
    ]

    def __init__(self):
        super().__init__()
        self.target_ap: AccessPoint = None
        self._refresh_timer = None
        self._known_clients: Set[str] = set()
        self._selected_clients: Set[str] = set()
        # Event-log de-dup state — see _poll_capture_events.
        self._seen_replay_counts: Dict[str, Dict[str, int]] = {}
        self._completed_clients: Set[str] = set()
        self._pmkid_clients: Set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Vertical(id="focus-container"):
            # Top: TARGET INFO | SECURITY | CAPTURE  (3 columns)
            with Horizontal(id="ap-info-panel"):
                yield Vertical(
                    Label("TARGET INFO", classes="panel-title"),
                    Label(id="lbl-ssid", classes="bold-title"),
                    Label(id="lbl-bssid"),
                    Label(id="lbl-channel"),
                    Label(id="lbl-first-seen"),
                    classes="info-box"
                )
                yield Vertical(
                    Label("SECURITY", classes="panel-title"),
                    Label(id="lbl-enc"),
                    Label(id="lbl-pmf"),
                    Label(id="lbl-wpa3"),
                    classes="info-box"
                )
                yield Vertical(
                    Label("CAPTURE", classes="panel-title"),
                    Label(id="lbl-beacons"),
                    Label(id="lbl-pwr"),
                    Label(id="lbl-handshake"),
                    Label(id="lbl-pmkid"),
                    classes="info-box"
                )

            # Middle: Client list (with handshake-status column)
            with Vertical(id="client-panel"):
                yield Label("CLIENTS", classes="panel-title")
                client_table = DataTable(cursor_type="row", id="client-table")
                client_table.add_column("[ ]", key="select")
                client_table.add_column("MAC Address", key="mac")
                client_table.add_column("PWR", key="signal")
                client_table.add_column("Frames", key="packets")
                client_table.add_column("Handshake", key="handshake")
                yield client_table

            # Bottom: ATTACKS (left) | EVENT LOG (right)
            with Horizontal(id="bottom-row"):
                with Vertical(id="attack-panel"):
                    yield Label("ATTACKS", classes="panel-title")
                    with Horizontal(classes="button-row"):
                        yield Button("Deauth All", variant="error", id="btn-deauth-all")
                        yield Button("Deauth Sel", variant="warning", id="btn-deauth-sel", disabled=True)
                        yield Button("PMKID", variant="primary", id="btn-pmkid")
                    with Horizontal(classes="button-row"):
                        yield Button("SAE Probe", variant="primary", id="btn-sae-probe", disabled=True)
                        yield Button("WPA3 Down", variant="primary", id="btn-wpa3-down", disabled=True)
                        yield Button("Save", variant="success", id="btn-save", disabled=True)
                with Vertical(id="event-log-panel"):
                    yield Label("EVENT LOG", classes="panel-title")
                    yield RichLog(id="focus-event-log", markup=True, highlight=False, wrap=True)

        yield Footer()

    async def on_mount(self) -> None:
        self._refresh_timer = self.set_interval(1 / 10, self.update_ui)
        await self._init_target()

    async def on_screen_resume(self) -> None:
        await self._init_target()

    async def _init_target(self) -> None:
        self.target_ap = getattr(self.app, "target_ap", None)
        if not self.target_ap:
            return

        # Reset UI state for the new target
        self._known_clients.clear()
        self._selected_clients.clear()
        self._seen_replay_counts.clear()
        self._completed_clients.clear()
        self._pmkid_clients.clear()
        self.query_one("#client-table", DataTable).clear()
        self.query_one("#focus-event-log", RichLog).clear()

        self._log(
            f"[bold]Target acquired:[/bold] "
            f"{escape(self.target_ap.ssid or '<hidden>')} ({self.target_ap.bssid})"
        )

        if getattr(self.app, "active_interface", None):
            ok = await self.app.active_interface.set_channel(self.target_ap.channel)
            verb = "Tuned" if ok else "Tried to tune"
            self._log(f"[cyan]{verb} to channel {self.target_ap.channel}[/cyan]")

        self.update_ui()

    # ----- Per-tick UI refresh -----------------------------------------------

    def update_ui(self) -> None:
        if not self.target_ap or not self.is_current:
            return

        ap = self.target_ap

        # Identity panel (rarely changes)
        msg_ssid = Text.from_markup(
            f"[bold white]{escape(ap.ssid or '<Hidden>')}[/bold white]",
            emoji=False,
        )
        self.query_one("#lbl-ssid", Label).update(msg_ssid)
        self.query_one("#lbl-bssid", Label).update(Text(f"BSSID: {ap.bssid}"))
        self.query_one("#lbl-channel", Label).update(f"Channel: {ap.channel}")
        age_s = max(0, int(time.time() - ap.first_seen))
        self.query_one("#lbl-first-seen", Label).update(
            f"Age: {age_s // 60:02d}:{age_s % 60:02d}"
        )

        # Security panel
        self.query_one("#lbl-enc", Label).update(f"Encryption: {ap.encryption}")
        pmf_status = "Disabled"
        if ap.pmf_required:
            pmf_status = "Required"
        elif ap.pmf_capable:
            pmf_status = "Capable (Optional)"
        self.query_one("#lbl-pmf", Label).update(f"PMF: {pmf_status}")

        wpa3_status = "N/A"
        btn_sae = self.query_one("#btn-sae-probe", Button)
        btn_down = self.query_one("#btn-wpa3-down", Button)
        btn_pmkid = self.query_one("#btn-pmkid", Button)
        if ap.wpa3:
            wpa3_status = "Transition Mode" if ap.transition_mode else "Pure WPA3-SAE"
            btn_sae.disabled = False
            btn_down.disabled = False
            # PMKID is only useful against WPA2 and WPA3-Transition (the WPA2
            # portion); pure SAE PMKID is not crackable with current attacks.
            btn_pmkid.disabled = not ap.transition_mode
        else:
            btn_sae.disabled = True
            btn_down.disabled = True
            btn_pmkid.disabled = False
        self.query_one("#lbl-wpa3", Label).update(f"WPA3: {wpa3_status}")

        # Capture panel (dynamic)
        elapsed = max(1.0, time.time() - ap.first_seen)
        rate = ap.beacons / elapsed
        self.query_one("#lbl-beacons", Label).update(
            f"Beacons: {ap.beacons} ({rate:.1f}/s)"
        )
        self.query_one("#lbl-pwr", Label).update(f"PWR: {ap.signal} dBm")

        n_complete = sum(1 for hs in ap.handshakes.values() if hs.is_complete)
        n_partial = sum(
            1 for hs in ap.handshakes.values()
            if not hs.is_complete and hs.total_eapol_frames > 0
        )
        if n_complete:
            hs_text = f"[bold green]Captured x{n_complete}[/bold green]"
            if n_partial:
                hs_text += f" [dim](+{n_partial} partial)[/dim]"
        elif n_partial:
            hs_text = f"[yellow]Partial x{n_partial}[/yellow]"
        else:
            hs_text = "[dim]Not captured[/dim]"
        self.query_one("#lbl-handshake", Label).update(
            Text.from_markup(f"Handshake: {hs_text}", emoji=False)
        )

        n_pmkid = sum(1 for hs in ap.handshakes.values() if hs.pmkid)
        pmkid_text = (
            f"[bold green]Captured x{n_pmkid}[/bold green]"
            if n_pmkid else "[dim]Not captured[/dim]"
        )
        self.query_one("#lbl-pmkid", Label).update(
            Text.from_markup(f"PMKID:     {pmkid_text}", emoji=False)
        )

        # Save button enable/disable
        self.query_one("#btn-save", Button).disabled = not ap.has_capture

        # Clients
        iface = getattr(self.app, "active_interface", None)
        if iface:
            self._refresh_clients(iface)

        # Detect & log capture events
        self._poll_capture_events()

        # Deauth-selected button
        self.query_one("#btn-deauth-sel", Button).disabled = not self._selected_clients

    def _refresh_clients(self, iface) -> None:
        ap = self.target_ap
        client_table = self.query_one("#client-table", DataTable)
        for mac, client in iface.clients.items():
            if client.bssid != ap.bssid:
                continue
            checkbox = "[X]" if mac in self._selected_clients else "[ ]"
            hs = ap.handshakes.get(mac)
            if hs and hs.is_complete:
                hs_label = "[green]Complete[/green]"
            elif hs and hs.total_eapol_frames:
                hs_label = f"[yellow]{hs.total_eapol_frames}/4[/yellow]"
            else:
                hs_label = "[dim]—[/dim]"
            hs_text = Text.from_markup(hs_label, emoji=False)

            if mac not in self._known_clients:
                self._known_clients.add(mac)
                client_table.add_row(
                    checkbox,
                    Text(mac),
                    f"{client.signal}dBm",
                    str(client.packets),
                    hs_text,
                    key=mac,
                )
            else:
                client_table.update_cell(mac, "select", checkbox)
                client_table.update_cell(mac, "signal", f"{client.signal}dBm")
                client_table.update_cell(mac, "packets", str(client.packets))
                client_table.update_cell(mac, "handshake", hs_text)

    def _poll_capture_events(self) -> None:
        """Diff current handshake/PMKID state against last tick; log changes."""
        ap = self.target_ap
        if ap is None:
            return
        for client_mac, hs in ap.handshakes.items():
            prev_counts = self._seen_replay_counts.setdefault(client_mac, {})
            for replay_hex, frames in hs.eapol_frames_by_replay.items():
                n = len(frames)
                prev = prev_counts.get(replay_hex, 0)
                if n > prev:
                    prev_counts[replay_hex] = n
                    if prev == 0:
                        self._log(
                            f"[green][+][/green] EAPOL from "
                            f"[bold]{client_mac}[/bold] "
                            f"(replay {replay_hex}, {n} frame)"
                        )
                    else:
                        self._log(
                            f"[bold green][++][/bold green] EAPOL pair grew: "
                            f"[bold]{client_mac}[/bold] replay {replay_hex} → "
                            f"{n} frames"
                        )
            if hs.is_complete and client_mac not in self._completed_clients:
                self._completed_clients.add(client_mac)
                self._log(
                    f"[bold green][✓] 4-WAY HANDSHAKE COMPLETE[/bold green] "
                    f"for client [bold]{client_mac}[/bold] "
                    f"— press [bold]s[/bold] to save"
                )
            if hs.pmkid and client_mac not in self._pmkid_clients:
                self._pmkid_clients.add(client_mac)
                self._log(
                    f"[bold yellow][✓] PMKID captured[/bold yellow] "
                    f"from [bold]{client_mac}[/bold]"
                )

    # ----- Event log helper --------------------------------------------------

    def _log(self, markup: str) -> None:
        ts = time.strftime("%H:%M:%S")
        try:
            log = self.query_one("#focus-event-log", RichLog)
        except Exception:
            return
        log.write(Text.from_markup(f"[dim]{ts}[/dim]  {markup}", emoji=False))

    # ----- Actions / handlers ------------------------------------------------

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """ENTER on a client row toggles its selection checkbox."""
        mac = event.row_key.value
        if mac in self._selected_clients:
            self._selected_clients.remove(mac)
        else:
            self._selected_clients.add(mac)
        self.update_ui()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-save":
            self._save_capture()
        elif bid == "btn-deauth-all":
            self._log("[dim]Deauth All — not yet wired in this build.[/dim]")
        elif bid == "btn-deauth-sel":
            self._log("[dim]Deauth Selected — not yet wired in this build.[/dim]")
        elif bid == "btn-pmkid":
            self._log("[dim]PMKID Harvest attack — not yet wired in this build.[/dim]")
        elif bid == "btn-sae-probe":
            self._log("[dim]SAE Group Probe — not yet wired in this build.[/dim]")
        elif bid == "btn-wpa3-down":
            self._log("[dim]WPA3 Downgrade — not yet wired in this build.[/dim]")

    def action_save_capture(self) -> None:
        self._save_capture()

    async def action_go_back(self) -> None:
        """Return to the scanner and resume channel hopping."""
        if getattr(self.app, "active_interface", None):
            await self.app.active_interface.start_hopping(interval=0.25)
        self.app.pop_screen()

    # ----- Save ----------------------------------------------------------------

    def _save_capture(self) -> None:
        ap = self.target_ap
        if not ap or not ap.has_capture:
            self._log("[red]Nothing to save yet.[/red]")
            return

        # Collect frames: one beacon (dedup) + all EAPOL frames across clients.
        frames: list[bytes] = []
        beacon_added = False
        for hs in ap.handshakes.values():
            if hs.beacon_frame and not beacon_added:
                frames.append(hs.beacon_frame)
                beacon_added = True
            for replay_frames in hs.eapol_frames_by_replay.values():
                frames.extend(replay_frames)

        n_complete = sum(1 for hs in ap.handshakes.values() if hs.is_complete)
        n_pmkid = sum(1 for hs in ap.handshakes.values() if hs.pmkid)

        captures_dir = Path("captures")
        safe_ssid = re.sub(r"[^A-Za-z0-9_-]", "_", ap.ssid or "hidden")[:24] or "hidden"
        safe_bssid = ap.bssid.replace(":", "-")
        path = captures_dir / f"{safe_ssid}_{safe_bssid}_{int(time.time())}.pcap"

        try:
            n_written = write_pcap(path, frames)
        except Exception as exc:
            logger.exception("Save capture failed")
            self._log(f"[bold red]Save failed:[/bold red] {escape(str(exc))}")
            return

        parts: list[str] = []
        if n_complete:
            parts.append(f"{n_complete} handshake(s)")
        if n_pmkid:
            parts.append(f"{n_pmkid} PMKID(s)")
        summary = " + ".join(parts) if parts else f"{n_written} frame(s)"
        self._log(
            f"[bold green]Saved {summary}[/bold green] "
            f"({n_written} frames) → [bold]{escape(str(path))}[/bold]"
        )
