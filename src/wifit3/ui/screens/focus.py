import logging
import re
import time
from pathlib import Path
from typing import Set

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header, Footer, DataTable, Button, Label, RichLog
from textual.containers import Vertical, Horizontal
from textual.binding import Binding
from rich.text import Text
from rich.markup import escape

from wifit3.engine.models import AccessPoint, Handshake
from wifit3.engine.hc22000 import write_hc22000
from wifit3.engine.pcap import write_pcap
from wifit3.engine.attacks.pmkid_harvest import PmkidHarvestAttack

from ..capture_events import CaptureEvent, CaptureEventDetector
from ..encryption_format import (
    format_encryption_markup,
    format_pmf_markup,
    format_wpa3_mode_markup,
)

logger = logging.getLogger(__name__)


class FocusView(Screen):
    """The Attack/Focus mode for a specific AP."""

    BINDINGS = [
        Binding("escape", "go_back", "Back to Scanner", show=True),
        Binding("q", "app.quit", "Quit", show=True),
        Binding("s", "save_capture", "Save Capture", show=True),
        Binding("space", "toggle_client", "Select Client", show=True),
    ]

    def __init__(self):
        super().__init__()
        self.target_ap: AccessPoint = None
        self._refresh_timer = None
        self._known_clients: Set[str] = set()
        self._selected_clients: Set[str] = set()
        # Granular: also surfaces every new EAPOL frame, not just completions.
        self._events = CaptureEventDetector(granular_eapol=True)

    # ----- Compose / mount ---------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Vertical(id="focus-container"):
            # Top: TARGET INFO | SECURITY | CAPTURE  (3 columns)
            with Horizontal(id="ap-info-panel"):
                yield Vertical(
                    Label("TARGET INFO", classes="panel-title"),
                    Label(id="lbl-ssid"),
                    Label(id="lbl-bssid"),
                    Label(id="lbl-channel"),
                    Label(id="lbl-last-beacon"),
                    classes="info-box",
                )
                yield Vertical(
                    Label("SECURITY", classes="panel-title"),
                    Label(id="lbl-enc"),
                    Label(id="lbl-pmf"),
                    Label(id="lbl-wpa3"),
                    classes="info-box",
                )
                yield Vertical(
                    Label("CAPTURE", classes="panel-title"),
                    Label(id="lbl-beacons"),
                    Label(id="lbl-pwr"),
                    Label(id="lbl-handshake"),
                    Label(id="lbl-pmkid"),
                    classes="info-box",
                )

            with Vertical(id="client-panel"):
                yield Label("CLIENTS", classes="panel-title")
                client_table = DataTable(cursor_type="row", id="client-table")
                client_table.add_column("[ ]", key="select")
                client_table.add_column("MAC Address", key="mac")
                client_table.add_column("POWER", key="signal")
                client_table.add_column("PKTS", key="packets")
                client_table.add_column("CAPTURES", key="captures")
                yield client_table

            with Horizontal(id="bottom-row"):
                with Vertical(id="attack-panel"):
                    yield Label("ATTACKS", classes="panel-title")
                    with Horizontal(classes="button-row"):
                        yield Button("Deauth All", variant="error", id="btn-deauth-all")
                        yield Button("Deauth [x]", variant="warning", id="btn-deauth-sel", disabled=True)
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

        # Reset per-target state.
        self._known_clients.clear()
        self._selected_clients.clear()
        self._events.reset()
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

        # TARGET INFO panel.
        self.query_one("#lbl-ssid", Label).update(
            Text.from_markup(
                f"ESSID: [bold cyan]{escape(ap.ssid or '<Hidden>')}[/bold cyan]",
                emoji=False,
            )
        )
        self.query_one("#lbl-bssid", Label).update(Text(f"BSSID: {ap.bssid}"))
        # While in FocusView the hopper is always stopped — channel is locked.
        self.query_one("#lbl-channel", Label).update(
            Text.from_markup(
                f"Channel: {ap.channel} [green](Locked)[/green]",
                emoji=False,
            )
        )
        last_seen_s = max(0, int(time.time() - ap.last_seen))
        self.query_one("#lbl-last-beacon", Label).update(
            f"Last Beacon: {last_seen_s}s ago"
        )

        # SECURITY panel.
        self.query_one("#lbl-enc", Label).update(
            Text.from_markup(
                "Encryption: " + format_encryption_markup(ap, detailed=True),
                emoji=False,
            )
        )
        self.query_one("#lbl-pmf", Label).update(
            Text.from_markup(f"PMF: {format_pmf_markup(ap)}", emoji=False)
        )

        wpa3_label = self.query_one("#lbl-wpa3", Label)
        wpa3_markup = format_wpa3_mode_markup(ap)
        if wpa3_markup is None:
            wpa3_label.display = False
        else:
            wpa3_label.display = True
            wpa3_label.update(
                Text.from_markup(f"WPA3: {wpa3_markup}", emoji=False)
            )

        # Attack-button enable/disable based on AP capability.
        btn_sae = self.query_one("#btn-sae-probe", Button)
        btn_down = self.query_one("#btn-wpa3-down", Button)
        btn_pmkid = self.query_one("#btn-pmkid", Button)
        if ap.wpa3:
            btn_sae.disabled = False
            btn_down.disabled = False
            # PMKID is only useful against WPA2 + WPA3-Transition (the WPA2
            # portion). Pure SAE PMKID isn't crackable with current attacks.
            btn_pmkid.disabled = not ap.transition_mode
        else:
            btn_sae.disabled = True
            btn_down.disabled = True
            btn_pmkid.disabled = False

        # CAPTURE panel (dynamic).
        elapsed = max(1.0, time.time() - ap.first_seen)
        rate = ap.beacons / elapsed
        self.query_one("#lbl-beacons", Label).update(
            f"Beacons: {ap.beacons} ({rate:.1f}/s)"
        )
        self.query_one("#lbl-pwr", Label).update(f"POWER: {ap.signal} dBm")

        n_complete = sum(1 for hs in ap.handshakes.values() if hs.is_complete)
        n_partial = sum(
            1
            for hs in ap.handshakes.values()
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
            if n_pmkid
            else "[dim]Not captured[/dim]"
        )
        self.query_one("#lbl-pmkid", Label).update(
            Text.from_markup(f"PMKID:     {pmkid_text}", emoji=False)
        )

        # Save button.
        self.query_one("#btn-save", Button).disabled = not ap.has_capture

        # Clients.
        iface = getattr(self.app, "active_interface", None)
        if iface:
            self._refresh_clients(iface)

        # Drain new capture events into the log.
        self._drain_capture_events(ap, iface.forged_macs if iface else set())

        # Deauth-selected button.
        self.query_one("#btn-deauth-sel", Button).disabled = not self._selected_clients

    # ----- Client table ------------------------------------------------------

    def _refresh_clients(self, iface) -> None:
        ap = self.target_ap
        client_table = self.query_one("#client-table", DataTable)
        forged = iface.forged_macs
        for mac, client in iface.clients.items():
            if client.bssid != ap.bssid:
                continue
            if mac in forged:
                continue
            checkbox = "[X]" if mac in self._selected_clients else "[ ]"
            hs = ap.handshakes.get(mac)
            captures_text = Text.from_markup(
                self._format_captures_label(hs), emoji=False
            )

            if mac not in self._known_clients:
                self._known_clients.add(mac)
                client_table.add_row(
                    checkbox,
                    Text(mac),
                    Text(f"{client.signal} dBm", justify="right"),
                    Text(str(client.packets), justify="right"),
                    captures_text,
                    key=mac,
                )
            else:
                client_table.update_cell(mac, "select", checkbox)
                client_table.update_cell(
                    mac, "signal", Text(f"{client.signal} dBm", justify="right")
                )
                client_table.update_cell(
                    mac, "packets", Text(str(client.packets), justify="right")
                )
                client_table.update_cell(mac, "captures", captures_text)

    @staticmethod
    def _format_captures_label(hs: Handshake | None) -> str:
        """Build the markup label shown in the per-client CAPTURES column.

        Folds in a `+PMK` suffix when a PMKID was harvested for the same
        client. PMKIDs without any EAPOL frames still display as `PMK`.
        """
        if hs is None or (not hs.total_eapol_frames and not hs.pmkid):
            return "[dim]—[/dim]"

        # PMKID-only (no EAPOL frames captured).
        if not hs.total_eapol_frames and hs.pmkid:
            return "[bold green]PMK[/bold green]"

        if hs.is_complete:
            pair = hs.find_valid_pair()
            if pair:
                base = f"[bold green]M{pair[0].msg_num}+M{pair[1].msg_num} ✓[/bold green]"
            else:
                base = "[bold green]Complete[/bold green]"
            if hs.pmkid:
                base += " [bold green]+PMK[/bold green]"
            return base

        # Partial — show what we have, with retry counts if any.
        from collections import Counter
        counts = Counter(f.msg_num for f in hs.eapol_frames if f.msg_num)
        parts = []
        for n in sorted(counts):
            if counts[n] > 1:
                parts.append(f"M{n}×{counts[n]}")
            else:
                parts.append(f"M{n}")
        unclassified = sum(1 for f in hs.eapol_frames if not f.msg_num)
        if unclassified:
            parts.append(f"?×{unclassified}")
        label = "[yellow]" + ",".join(parts) + "[/yellow]"
        if hs.pmkid:
            label += " [bold green]+PMK[/bold green]"
        return label

    # ----- Capture-event log -------------------------------------------------

    def _drain_capture_events(self, ap: AccessPoint, forged_macs: Set[str]) -> None:
        for ev in self._events.poll(ap, forged_macs=forged_macs):
            self._log_capture_event(ev)

    def _log_capture_event(self, ev: CaptureEvent) -> None:
        client = escape(ev.client_mac)
        if ev.kind == "eapol":
            msg_label = f"M{ev.msg_num}" if ev.msg_num else "EAPOL-?"
            self._log(
                f"[green]→[/green] [bold]{msg_label}[/bold] from "
                f"[bold]{client}[/bold] (replay {ev.replay_hex})"
            )
        elif ev.kind == "handshake_complete":
            self._log(
                f"[bold green]✓ HANDSHAKE COMPLETE[/bold green] "
                f"({ev.pair_label}) for client [bold]{client}[/bold] "
                f"— press [bold]s[/bold] to save"
            )
        elif ev.kind == "pmkid":
            self._log(
                f"[bold yellow]✓ PMKID captured[/bold yellow] "
                f"from [bold]{client}[/bold]"
            )

    def _log(self, markup: str) -> None:
        ts = time.strftime("%H:%M:%S")
        try:
            log = self.query_one("#focus-event-log", RichLog)
        except Exception:
            return
        log.write(Text.from_markup(f"[dim]{ts}[/dim]  {markup}", emoji=False))

    # ----- Actions / handlers ------------------------------------------------

    async def on_data_table_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        """ENTER on a client row toggles selection (Textual emits this for ENTER)."""
        self._toggle_client_selection(event.row_key.value)

    def action_toggle_client(self) -> None:
        """SPACE keybinding — toggles the currently-highlighted client."""
        table = self.query_one("#client-table", DataTable)
        try:
            mac = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return
        if mac:
            self._toggle_client_selection(mac)

    def _toggle_client_selection(self, mac: str | None) -> None:
        if not mac:
            return
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
            self.run_worker(self._run_pmkid_harvest(), exclusive=True)
        elif bid == "btn-sae-probe":
            self._log("[dim]SAE Group Probe — not yet wired in this build.[/dim]")
        elif bid == "btn-wpa3-down":
            self._log("[dim]WPA3 Downgrade — not yet wired in this build.[/dim]")

    def action_save_capture(self) -> None:
        self._save_capture()

    async def _run_pmkid_harvest(self) -> None:
        """Worker: run a PMKID harvest against the focused AP."""
        ap = self.target_ap
        iface = getattr(self.app, "active_interface", None)
        if not ap or not iface:
            self._log("[red]✗ No target / interface — aborting PMKID harvest.[/red]")
            return

        self._log(
            f"[bold cyan]→ PMKID[/bold cyan] harvesting on "
            f"[bold]{escape(ap.ssid or '<hidden>')}[/bold] ({ap.bssid}) "
            f"CH {ap.channel}"
        )
        attack = PmkidHarvestAttack(iface, ap)
        try:
            pmkid = await attack.run()
        except Exception as exc:
            logger.exception("PMKID harvest crashed")
            self._log(f"[bold red]✗ PMKID crashed:[/bold red] {escape(str(exc))}")
            return

        if pmkid:
            self._log(
                f"[bold green]✓ PMKID harvested:[/bold green] "
                f"[bold]{pmkid.hex()}[/bold] — press 's' to save hashline."
            )
        else:
            self._log(
                "[yellow]⚠ PMKID: no result after all attempts.[/yellow] "
                "AP may not advertise a PMKID KDE, or PMF / status rejected us."
            )

    async def action_go_back(self) -> None:
        if getattr(self.app, "active_interface", None):
            await self.app.active_interface.start_hopping(interval=0.25)
        self.app.pop_screen()

    # ----- Save --------------------------------------------------------------

    def _save_capture(self) -> None:
        ap = self.target_ap
        if not ap or not ap.has_capture:
            self._log("[yellow]⚠ Nothing to save yet.[/yellow]")
            return

        frames: list[bytes] = []
        beacon_added = False
        for hs in ap.handshakes.values():
            if hs.beacon_frame and not beacon_added:
                frames.append(hs.beacon_frame)
                beacon_added = True
            for f in hs.eapol_frames:
                frames.append(f.raw)

        n_complete = sum(1 for hs in ap.handshakes.values() if hs.is_complete)
        n_pmkid = sum(1 for hs in ap.handshakes.values() if hs.pmkid)

        captures_dir = Path("captures")
        safe_ssid = re.sub(r"[^A-Za-z0-9_-]", "_", ap.ssid or "hidden")[:24] or "hidden"
        safe_bssid = ap.bssid.replace(":", "-")
        stem = f"{safe_ssid}_{safe_bssid}_{int(time.time())}"
        pcap_path = captures_dir / f"{stem}.pcap"
        hc_path = captures_dir / f"{stem}.hc22000"

        try:
            n_written = write_pcap(pcap_path, frames)
            n_hashlines = write_hc22000(hc_path, ap)
        except Exception as exc:
            logger.exception("Save capture failed")
            self._log(f"[bold red]✗ Save failed:[/bold red] {escape(str(exc))}")
            return

        parts: list[str] = []
        if n_complete:
            parts.append(f"{n_complete} handshake(s)")
        if n_pmkid:
            parts.append(f"{n_pmkid} PMKID(s)")
        summary = " + ".join(parts) if parts else f"{n_written} frame(s)"
        self._log(
            f"[bold green]✓ Saved {summary}[/bold green] "
            f"({n_written} frames) → [bold]{escape(str(pcap_path))}[/bold]"
        )
        if n_hashlines:
            self._log(
                f"[bold green]✓ {n_hashlines} hashline(s)[/bold green] "
                f"→ [bold]{escape(str(hc_path))}[/bold] "
                f"[dim](hashcat -m 22000)[/dim]"
            )
        else:
            self._log(
                "[dim]  (no hc22000 hashline produced — "
                "hidden SSID or truncated capture)[/dim]"
            )
