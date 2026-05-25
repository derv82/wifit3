import logging
import re
import time
from pathlib import Path
from typing import Optional, Set

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
from wifit3.engine.attacks.sae_probe import SAEGroupProbeAttack
from wifit3.engine.attacks.wpa3_downgrade import WPA3DowngradeAttack
from wifit3.engine.attacks.wep.campaign import WepCampaign
from wifit3.engine.attacks.wep.crack import CRACK_READY_THRESHOLD

from ..capture_events import DECLOAK_METHOD_LABELS, CaptureEvent, CaptureEventDetector
from ..encryption_format import (
    format_encryption_markup,
    format_pmf_markup,
    format_wps_markup,
    format_wpa3_mode_markup,
)

logger = logging.getLogger(__name__)


def _format_duration(seconds: int) -> str:
    """Human-readable duration for the Focus 'Last Beacon' line.
    Examples: '5s', '1m 12s', '1h 4m', '2d 3h'. Drops the lower unit
    when it's zero to keep the line tight."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s" if s else f"{m}m"
    if seconds < 86400:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h}h {m}m" if m else f"{h}h"
    d, rem = divmod(seconds, 86400)
    h = rem // 3600
    return f"{d}d {h}h" if h else f"{d}d"


class FocusView(Screen):
    """The Attack/Focus mode for a specific AP."""

    BINDINGS = [
        Binding("escape", "go_back", "Back to Scanner", show=True),
        Binding("q", "app.quit", "Quit", show=True),
        # 's' has two labels for the same key — check_action() reveals exactly
        # one based on the target's encryption (Save Capture for WPA's
        # handshake/PMKID, Save Key for a cracked WEP key) and hides both until
        # there's actually something to save. Likewise 'c' only appears once a
        # WEP key is recovered.
        Binding("s", "save_capture", "Save Capture", show=True),
        Binding("s", "save_key", "Save Key", show=True),
        Binding("c", "copy_key", "Copy WEP Key", show=True),
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
        # WPA3 Downgrade is a long-running probe-response-spoof daemon. Held
        # here so the button can toggle Start/Stop and so target/screen
        # transitions can tear it down deterministically.
        self._wpa3_down_attack: Optional[WPA3DowngradeAttack] = None
        # WEP "Generate IVs" campaign (M3): fake-auth + ARP replay. Held so the
        # button toggles Start/Stop and transitions tear it down deterministically.
        self._wep_campaign: Optional[WepCampaign] = None

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
                    classes="info-box", id="panel-target",
                )
                yield Vertical(
                    Label("SECURITY", classes="panel-title"),
                    Label(id="lbl-enc"),
                    Label(id="lbl-wps"),
                    Label(id="lbl-pmf"),
                    Label(id="lbl-wpa3"),
                    Label(id="lbl-sae-groups"),
                    # WEP-only: fake-auth status, then the Crack progress + a
                    # detail line. Cracking runs in PARALLEL with capture, so it
                    # lives here (under SECURITY, where WEP leaves rows free)
                    # rather than crowding CAPTURE.
                    Label(id="lbl-fakeauth"),
                    Label(id="lbl-crack"),
                    Label(id="lbl-crack-info"),
                    classes="info-box", id="panel-security",
                )
                yield Vertical(
                    Label("CAPTURE", classes="panel-title"),
                    Label(id="lbl-beacons"),
                    Label(id="lbl-pwr"),
                    Label(id="lbl-handshake"),
                    Label(id="lbl-pmkid"),
                    # WEP-only (shown in place of Handshake/PMKID): the IV count
                    # and a dedicated Replay-status row. The Crack line lives
                    # under SECURITY — capture + cracking are parallel processes.
                    Label(id="lbl-ivs"),
                    Label(id="lbl-replay"),
                    classes="info-box", id="panel-capture",
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
                # Far-left: DEAUTH — always available for both WEP and WPA,
                # two buttons stacked in a narrow column.
                with Vertical(id="deauth-panel"):
                    yield Label("DEAUTH", classes="panel-title")
                    yield Button("Selected", variant="warning", id="btn-deauth-sel", disabled=True)
                    yield Button("Broadcast", variant="error", id="btn-deauth-bcast")
                # Center: the crypto-specific attacks (title swaps WEP/WPA). Each
                # target only ever shows its own ≤4 buttons, laid out 2-per-row:
                #   WEP:  Replay  Chop          WPA:  PMKID  SAE
                #         Save    Frag                WPA ↓  Save
                # (Chop/Frag appear only while Replay runs.)
                with Vertical(id="attack-panel"):
                    yield Label("ATTACKS", classes="panel-title", id="lbl-attack-title")
                    with Horizontal(classes="button-row"):
                        # Starts fake-auth + ARP replay + the cracker (WEP).
                        # Green = start; turns red ("Stop Replay") while running.
                        yield Button("Replay", variant="success", id="btn-gen-ivs")
                        # Sibling of Frag: ChopChop manufactures a seed when frag
                        # gets no response. Mutually exclusive (click-to-switch).
                        yield Button("Chop", variant="primary", id="btn-chop")
                        yield Button("PMKID", variant="primary", id="btn-pmkid")
                        yield Button("SAE", variant="primary", id="btn-sae-probe", disabled=True)
                    with Horizontal(classes="button-row"):
                        yield Button("WPA ↓", variant="primary", id="btn-wpa3-down", disabled=True)
                        yield Button("Save", variant="success", id="btn-save", disabled=True)
                        # WEP-only, only while a campaign runs: switch the radio
                        # to fragmentation to manufacture an ARP seed when replay
                        # has none (no client traffic). Click-to-toggle.
                        yield Button("Frag", variant="primary", id="btn-frag")
                # Far-right: the log — grows to fill the (wide) terminal.
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
        # Always tear down any running WPA3 Down daemon — its forged template
        # is bound to the PREVIOUS target's BSSID/SSID/channel, so it'd inject
        # the wrong payload for a new target. Safe no-op if nothing's running.
        self._stop_wpa3_down()
        # Same for the Generate IVs campaign — its forged STA + replay are
        # bound to the previous BSSID; leaving it running would inject to the
        # wrong AP.
        self._stop_generate_ivs()

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
        is_wep = (ap.encryption or "").upper() == "WEP"

        # TARGET INFO panel.
        if ap.ssid:
            ssid_markup = f"[bold cyan]{escape(ap.ssid)}[/bold cyan]"
        else:
            ssid_markup = "[italic bold cyan]<Hidden>[/italic bold cyan]"
        self.query_one("#lbl-ssid", Label).update(
            Text.from_markup(f"ESSID: {ssid_markup}", emoji=False)
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
            f"Last Beacon: {_format_duration(last_seen_s)} ago"
        )

        # SECURITY panel.
        self.query_one("#lbl-enc", Label).update(
            Text.from_markup(
                "Encryption: " + format_encryption_markup(ap, detailed=True),
                emoji=False,
            )
        )
        wps_label = self.query_one("#lbl-wps", Label)
        wps_markup = format_wps_markup(ap)
        if wps_markup is None:
            wps_label.display = False
        else:
            wps_label.display = True
            wps_label.update(Text.from_markup(f"WPS: {wps_markup}", emoji=False))

        # PMF lives in the RSN IE (WPA2/WPA3), so it's only meaningful there —
        # for WEP / OPEN / legacy-WPA1 there's no RSN and it's always Disabled.
        # Hide it rather than show a perpetual "Disabled" that can't change.
        pmf_label = self.query_one("#lbl-pmf", Label)
        if ap.akms or ap.wpa3:
            pmf_label.display = True
            pmf_label.update(
                Text.from_markup(f"PMF: {format_pmf_markup(ap)}", emoji=False)
            )
        else:
            pmf_label.display = False

        wpa3_label = self.query_one("#lbl-wpa3", Label)
        wpa3_markup = format_wpa3_mode_markup(ap)
        if wpa3_markup is None:
            wpa3_label.display = False
        else:
            wpa3_label.display = True
            wpa3_label.update(
                Text.from_markup(f"WPA3: {wpa3_markup}", emoji=False)
            )

        # SAE Groups — populated by the SAE probe attack, cached on the AP.
        # Hidden until we have at least one probe result. Supported groups are
        # coloured by Dragonblood risk (22/23/24 = red attackable, others green).
        sae_label = self.query_one("#lbl-sae-groups", Label)
        if ap.sae_groups:
            sae_label.display = True
            sae_label.update(
                Text.from_markup(
                    f"SAE Groups: {self._format_sae_groups_markup(ap)}",
                    emoji=False,
                )
            )
        else:
            sae_label.display = False

        # Attack buttons. WEP and WPA targets get disjoint button sets — the
        # WPA buttons (PMKID/SAE/WPA3 Down) are meaningless for WEP, so hide
        # (not just disable) them and surface Fake Auth in their place.
        btn_sae = self.query_one("#btn-sae-probe", Button)
        btn_down = self.query_one("#btn-wpa3-down", Button)
        btn_pmkid = self.query_one("#btn-pmkid", Button)
        btn_gen = self.query_one("#btn-gen-ivs", Button)
        btn_frag = self.query_one("#btn-frag", Button)
        btn_chop = self.query_one("#btn-chop", Button)

        self.query_one("#lbl-attack-title", Label).update(
            "WEP ATTACKS" if is_wep else "WPA ATTACKS"
        )
        btn_sae.display = not is_wep
        btn_down.display = not is_wep
        btn_pmkid.display = not is_wep
        # Replay vanishes once the key is cracked — Save takes its place, so it
        # reads as a "Replay → Save" swap.
        btn_gen.display = is_wep and ap.wep_key is None
        # Frag/Chop are shown only inside a running WEP campaign (set below);
        # off otherwise so they never linger on a WPA target.
        btn_frag.display = False
        btn_chop.display = False

        if is_wep:
            # A finished campaign (key recovered) is torn down so the button
            # reverts to "Generate IVs" — the user shouldn't have to click Stop
            # after a successful crack. The key persists on ap.wep_key.
            camp = self._wep_campaign
            if camp is not None and camp.recovered_key is not None:
                self._stop_generate_ivs()
                camp = None
            # Replay is the campaign switch: green to start, red to STOP the
            # whole campaign. Frag/Chop are sub-attacks: blue to start, orange
            # ("Stop X") to stop just that one (the campaign keeps running).
            running = camp is not None
            btn_gen.label = "Stop Replay" if running else "Replay"
            btn_gen.variant = "error" if running else "success"
            # Frag is a sub-mode of a running campaign (it manufactures a seed
            # for replay), so it only appears once IVs are being generated.
            btn_frag.display = running and ap.wep_key is None
            fragging = bool(camp and camp.frag_active)
            btn_frag.label = "Stop Frag" if fragging else "Frag"
            btn_frag.variant = "warning" if fragging else "primary"
            btn_chop.display = running and ap.wep_key is None
            chopping = bool(camp and camp.chop_active)
            btn_chop.label = "Stop Chop" if chopping else "Chop"
            btn_chop.variant = "warning" if chopping else "primary"
            self._update_fakeauth_line()
        else:
            self.query_one("#lbl-fakeauth", Label).display = False
            self.query_one("#lbl-crack", Label).display = False
            self.query_one("#lbl-crack-info", Label).display = False
            if ap.wpa3:
                btn_sae.disabled = False
                # WPA3 Downgrade only works against transition-mode APs (pure
                # WPA3 clients refuse a WPA2-only ad from a known-SAE network).
                btn_down.disabled = not ap.transition_mode
                # PMKID is only useful against WPA2 + WPA3-Transition (the WPA2
                # portion). Pure SAE PMKID isn't crackable with current attacks.
                btn_pmkid.disabled = not ap.transition_mode
            else:
                btn_sae.disabled = True
                btn_down.disabled = True
                btn_pmkid.disabled = False
            # WPA Downgrade button label reflects daemon state.
            btn_down.label = "Stop ↓" if self._wpa3_down_attack else "WPA ↓"

        # CAPTURE panel (dynamic).
        elapsed = max(1.0, time.time() - ap.first_seen)
        rate = ap.beacons / elapsed
        self.query_one("#lbl-beacons", Label).update(
            f"Beacons: {ap.beacons:,} ({rate:.1f}/s)"
        )
        self.query_one("#lbl-pwr", Label).update(f"Power: {ap.signal} dBm")

        # WEP and WPA2/3 capture progress are mutually exclusive: WEP has no
        # handshake/PMKID, WPA has no IVs. Show whichever pair fits the target.
        hs_label = self.query_one("#lbl-handshake", Label)
        pmkid_label = self.query_one("#lbl-pmkid", Label)
        ivs_label = self.query_one("#lbl-ivs", Label)

        hs_label.display = not is_wep
        pmkid_label.display = not is_wep
        ivs_label.display = is_wep
        self.query_one("#lbl-replay", Label).display = is_wep

        if is_wep:
            # _update_wep_capture owns lbl-ivs / lbl-replay (CAPTURE) and
            # lbl-crack / lbl-crack-info (SECURITY).
            self._update_wep_capture(ap)
        else:
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
            hs_label.update(Text.from_markup(f"Handshake: {hs_text}", emoji=False))

            n_pmkid = sum(1 for hs in ap.handshakes.values() if hs.pmkid)
            pmkid_text = (
                f"[bold green]Captured x{n_pmkid}[/bold green]"
                if n_pmkid
                else "[dim]Not captured[/dim]"
            )
            pmkid_label.update(
                Text.from_markup(f"PMKID:     {pmkid_text}", emoji=False)
            )

        # Save button.
        # Save only appears once there's something to save (a WEP key, or a
        # WPA handshake/PMKID) — hidden, not shown-disabled. It's created
        # disabled (compose), so clear that when shown or it greys out.
        btn_save = self.query_one("#btn-save", Button)
        # Keep Save's grid cell reserved (visibility, not display) so its
        # row-mate Frag stays pinned in the right column even before there's
        # anything to save — otherwise a display:none Save collapses the row and
        # Frag slides under Replay.
        btn_save.display = True
        btn_save.visible = ap.has_capture
        btn_save.disabled = not ap.has_capture
        # Button stays plain "Save" to fit the narrow panel; the footer 's'
        # carries the crypto-specific label (Save Capture / Save Key).
        # Re-evaluate the footer's Save/Copy keys (check_action) as the key /
        # handshake state changes.
        self.refresh_bindings()

        # Clients.
        iface = getattr(self.app, "active_interface", None)
        if iface:
            self._refresh_clients(iface)

        # Drain new capture events into the log.
        self._drain_capture_events(ap, iface.forged_macs if iface else set())

        # Deauth-selected button.
        self.query_one("#btn-deauth-sel", Button).disabled = not self._selected_clients

    @staticmethod
    def _replay_status_markup(campaign) -> str:
        """The Replay-status row's value. Surfaces frag/chop too — while they
        run, replay is paused on purpose, so say WHAT'S running (not a bare
        'paused' that reads like the attack stalled)."""
        if campaign is None:
            return "[dim]not started[/dim]"
        # Frag/Chop take over the radio (replay paused by design) — name them.
        if campaign.frag_active:
            return "[dim]forging a seed using[/dim] [cyan]Fragmentation[/cyan][dim]…[/dim]"
        if campaign.chop_active:
            return "[dim]forging a seed using[/dim] [cyan]ChopChop[/cyan][dim]…[/dim]"
        s = campaign.replay.state
        if s == "replaying":
            # target_pps = the smooth P&O rate, not the jittery per-cycle
            # measured effective_pps.
            return (
                f"[green]Replaying ARP[/green] "
                f"[dim]({campaign.replay.target_pps:.0f}pps)[/dim]"
            )
        if s == "testing":
            return "[cyan]Trying candidate ARP…[/cyan]"
        if s == "waiting-arp":
            return "[yellow]waiting for ARP[/yellow]"
        if s == "waiting-auth":
            return "[dim]associating…[/dim]"
        if s == "paused":
            return "[dim]paused[/dim]"
        return "[dim]idle[/dim]"

    def _update_wep_capture(self, ap: AccessPoint) -> None:
        """WEP CAPTURE rows — IVs (with the usable-IV count) and a dedicated
        Replay-status row — plus the Crack section (which lives under SECURITY).
        Usable IVs = crack samples (ARP-sized broadcast, known plaintext); they
        lag raw unique IVs and are what actually gate cracking."""
        iface = getattr(self.app, "active_interface", None)
        n = ap.wep.unique_ivs if ap.wep else 0
        rate = iface.wep_store.rate(ap.bssid) if iface else 0.0
        samples = iface.wep_store.crack_sample_count(ap.bssid) if iface else 0
        campaign = self._wep_campaign

        count = f"[bold green]{n:,}[/bold green]" if n else "[red]0[/red]"
        self.query_one("#lbl-ivs", Label).update(Text.from_markup(
            f"IVs: {count} [dim]({rate:.0f}/s)[/dim] "
            f"[dim]({samples:,} usable)[/dim]", emoji=False
        ))

        replay_markup = (
            "[green]✓ done[/green]" if ap.wep_key is not None
            else self._replay_status_markup(campaign)
        )
        self.query_one("#lbl-replay", Label).update(
            Text.from_markup(f"Replay: {replay_markup}", emoji=False)
        )

        self._update_crack_section(ap, campaign, samples)

    def _update_crack_section(self, ap: AccessPoint, campaign, samples: int) -> None:
        """The two-row Crack section under SECURITY: a status line + a detail
        line. Only shown during a running campaign or once a key is found."""
        crack = self.query_one("#lbl-crack", Label)
        info = self.query_one("#lbl-crack-info", Label)
        if campaign is None and ap.wep_key is None:
            crack.display = False
            info.display = False
            return
        crack.display = True
        info.display = True
        target_k = CRACK_READY_THRESHOLD // 1000

        if ap.wep_key is not None:
            key = ap.wep_key
            ascii_hint = (
                f' = "{key.decode("ascii")}"'
                if all(0x20 <= b < 0x7F for b in key) else ""
            )
            crack.update(Text.from_markup(
                f"Crack: [bold black on cyan] ✓ KEY {key.hex()}{ascii_hint} "
                f"[/bold black on cyan]", emoji=False
            ))
            info.update(Text.from_markup(
                "[white]Press [green]'c'[/green] to copy, "
                "[green]'s'[/green] to save .txt[/white]", emoji=False
            ))
        elif samples < CRACK_READY_THRESHOLD:
            crack.update(Text.from_markup(
                f"Crack: [white]{samples:,}/{target_k}k usable IVs[/white]",
                emoji=False
            ))
            info.update(Text.from_markup(
                f"[dim]Crack begins at {target_k}k[/dim]", emoji=False
            ))
        else:
            sc = campaign.cracker.sample_count if campaign else samples
            crack.update(Text.from_markup(
                f"Crack: [cyan]Cracking…[/cyan] [dim]({sc:,} samples)[/dim]",
                emoji=False
            ))
            info.update(Text.from_markup(
                "[dim]Some keys require >40K samples[/dim]", emoji=False
            ))

    def _update_fakeauth_line(self) -> None:
        """Render the SECURITY-panel Fake-Auth status from the running campaign."""
        fa_label = self.query_one("#lbl-fakeauth", Label)
        fa_label.display = True
        campaign = self._wep_campaign
        if campaign is None:
            fa_label.update(Text.from_markup("Fake-Auth: [dim]Off[/dim]", emoji=False))
            return
        fa = campaign.fake_auth
        if fa.state == "associated":
            countdown = ""
            if fa.next_reauth_at:
                secs = max(0, int(fa.next_reauth_at - time.time()))
                countdown = f" [dim](re-auth in {secs}s)[/dim]"
            fa_markup = f"[green]✓ Associated[/green]{countdown}"
        elif fa.state == "authenticating":
            fa_markup = "[yellow]Associating…[/yellow]"
        elif fa.state == "failed":
            fa_markup = f"[red]Failed: {escape(fa.fail_reason or 'unknown')}[/red]"
        else:
            fa_markup = "[dim]Idle[/dim]"
        fa_label.update(Text.from_markup(f"Fake-Auth: {fa_markup}", emoji=False))

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
            # Our own forged STA (fake-auth) is shown as "YOU", whole cell
            # highlighted so it can't be mistaken for a stranger.
            mac_cell = (
                Text.from_markup(f"[cyan]{escape(mac)} YOU[/cyan]", emoji=False)
                if client.is_self
                else Text(mac)
            )

            if mac not in self._known_clients:
                self._known_clients.add(mac)
                client_table.add_row(
                    checkbox,
                    mac_cell,
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

    _DRAGONBLOOD_GROUPS = {22, 23, 24}

    @classmethod
    def _format_sae_groups_markup(cls, ap: AccessPoint) -> str:
        """Render `ap.sae_groups` as a compact, colour-coded inline list.

        Supported Dragonblood groups → bold red (attackable).
        Other supported groups       → green.
        Rejected groups              → dim.
        """
        parts = []
        for group in sorted(ap.sae_groups.keys()):
            verdict = ap.sae_groups[group]
            if verdict == "supported":
                if group in cls._DRAGONBLOOD_GROUPS:
                    parts.append(f"[bold red]{group}[/bold red]")
                else:
                    parts.append(f"[green]{group}[/green]")
            elif verdict == "rejected":
                parts.append(f"[dim]{group}[/dim]")
        return ", ".join(parts) if parts else "[dim]—[/dim]"

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
        elif ev.kind == "decloak":
            method_label = DECLOAK_METHOD_LABELS.get(ev.method or "", ev.method or "?")
            self._log(
                f"[bold]Decloaked[/bold] [cyan]{escape(ev.bssid)}[/cyan] → "
                f"[green]{escape(ev.ssid or '')}[/green] "
                f"[dim]via {method_label}[/dim]"
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
        elif bid == "btn-deauth-bcast":
            self.run_worker(self._run_deauth_broadcast(), exclusive=True)
        elif bid == "btn-deauth-sel":
            self.run_worker(self._run_deauth_selected(), exclusive=True)
        elif bid == "btn-pmkid":
            self.run_worker(self._run_pmkid_harvest(), exclusive=True)
        elif bid == "btn-sae-probe":
            self.run_worker(self._run_sae_probe(), exclusive=True)
        elif bid == "btn-wpa3-down":
            self._toggle_wpa3_down()
        elif bid == "btn-gen-ivs":
            self._toggle_generate_ivs()
        elif bid == "btn-frag":
            self._toggle_frag()
        elif bid == "btn-chop":
            self._toggle_chop()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Show/hide the Save + Copy footer keys based on the target's
        encryption and whether there's anything to save yet. Returning False
        hides the binding entirely (and blocks the keypress); the rest stay
        shown. ``update_ui`` calls ``refresh_bindings()`` so this re-evaluates
        as the campaign progresses."""
        if action in ("save_capture", "save_key", "copy_key"):
            ap = self.target_ap
            if ap is None:
                return False
            is_wep = (ap.encryption or "").upper() == "WEP"
            if action == "save_capture":      # WPA: a handshake/PMKID to write
                return not is_wep and ap.has_capture
            # save_key / copy_key: a recovered WEP key
            return is_wep and ap.wep_key is not None
        return True

    def action_save_capture(self) -> None:
        self._save_capture()

    def action_save_key(self) -> None:
        # Same writer as Save Capture — it routes to the WEP-key path when a key
        # is present; the separate action just gives the footer a WEP label.
        self._save_capture()

    def action_copy_key(self) -> None:
        """Copy the recovered WEP key (hex) to the clipboard — saves the user
        from hand-selecting terminal text. No-op with a hint if not cracked."""
        # Read from the AP (persists after the finished campaign is torn down).
        key = self.target_ap.wep_key if self.target_ap else None
        if not key:
            self._log("[yellow]No WEP key recovered yet — nothing to copy.[/yellow]")
            return
        kh = key.hex()
        try:
            self.app.copy_to_clipboard(kh)
            self._log(f"[green]✓ Copied WEP key to clipboard:[/green] [bold]{kh}[/bold]")
        except Exception:
            # Clipboard may be unavailable (no OSC-52 support); the key is
            # still right there in the log/panel to select manually.
            self._log(f"[yellow]Clipboard unavailable — key is:[/yellow] [bold]{kh}[/bold]")

    async def _run_deauth_broadcast(self) -> None:
        """Worker: broadcast-deauth every station associated with the focused AP."""
        ap = self.target_ap
        iface = getattr(self.app, "active_interface", None)
        if not ap or not iface:
            self._log("[red]✗ No target / interface — aborting Broadcast.[/red]")
            return

        BROADCAST = "ff:ff:ff:ff:ff:ff"
        self._log(
            f"[bold cyan]→ Broadcast deauth[/bold cyan] on "
            f"[bold]{escape(ap.ssid or '<hidden>')}[/bold] ({ap.bssid}) CH {ap.channel}"
        )
        try:
            await iface.deauth(ap.bssid, BROADCAST)
        except Exception as exc:
            logger.exception("Broadcast deauth crashed")
            self._log(f"[bold red]✗ Broadcast crashed:[/bold red] {escape(str(exc))}")
            return
        self._log("[green]✓ Broadcast deauth burst sent.[/green]")

    # Total deauth pairs per selected client. Round-robin'd across clients so
    # each frame pair is followed by a 10ms RX window — keeps the radio from
    # being TX-saturated when many clients are queued (wifite2 starved RX
    # exactly this way when too many deauths were back-to-back).
    _DEAUTH_SEL_ROUNDS = 10

    async def _run_deauth_selected(self) -> None:
        """Worker: deauth each selected client, interleaved round-robin so RX
        gets a slice between every frame pair."""
        ap = self.target_ap
        iface = getattr(self.app, "active_interface", None)
        if not ap or not iface:
            self._log("[red]✗ No target / interface — aborting Deauth Selected.[/red]")
            return

        # Snapshot — the set can mutate while we're iterating.
        targets = sorted(self._selected_clients)
        if not targets:
            self._log("[yellow]⚠ No clients selected.[/yellow]")
            return

        self._log(
            f"[bold cyan]→ Deauth Selected[/bold cyan] "
            f"({len(targets)} client(s) × {self._DEAUTH_SEL_ROUNDS} rounds, round-robin) on "
            f"[bold]{escape(ap.ssid or '<hidden>')}[/bold] ({ap.bssid}) CH {ap.channel}"
        )
        failed: set[str] = set()
        for _ in range(self._DEAUTH_SEL_ROUNDS):
            for mac in targets:
                if mac in failed:
                    continue
                try:
                    await iface.deauth(ap.bssid, mac, burst_count=1)
                except Exception as exc:
                    logger.exception("Deauth %s crashed", mac)
                    self._log(
                        f"[bold red]✗ Deauth {escape(mac)} crashed:[/bold red] "
                        f"{escape(str(exc))} — dropping target."
                    )
                    failed.add(mac)
        sent = len(targets) - len(failed)
        self._log(
            f"[green]✓ Round-robin deauth complete[/green] — "
            f"{sent}/{len(targets)} client(s) hit "
            f"({self._DEAUTH_SEL_ROUNDS} pairs each)."
        )

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

    async def _run_sae_probe(self) -> None:
        """Worker: enumerate which SAE groups the focused AP accepts."""
        ap = self.target_ap
        iface = getattr(self.app, "active_interface", None)
        if not ap or not iface:
            self._log("[red]✗ No target / interface — aborting SAE probe.[/red]")
            return

        self._log(
            f"[bold cyan]→ SAE Group Probe[/bold cyan] on "
            f"[bold]{escape(ap.ssid or '<hidden>')}[/bold] ({ap.bssid}) CH {ap.channel}"
        )
        attack = SAEGroupProbeAttack(iface, ap)
        try:
            results = await attack.run()
        except Exception as exc:
            logger.exception("SAE probe crashed")
            self._log(f"[bold red]✗ SAE probe crashed:[/bold red] {escape(str(exc))}")
            return

        # Dragonblood-vulnerable groups — auditor view: supporting these is
        # GOOD news (the AP is attackable). Render supported groups in red
        # when they're Dragonblood-relevant, green when they're modern.
        DRAGONBLOOD = {22, 23, 24}
        supported_groups = []
        dragonblood_hits = []
        for group, (label, detail) in results.items():
            if label == "Supported":
                supported_groups.append(group)
                if group in DRAGONBLOOD:
                    color = "bold red"
                    dragonblood_hits.append(group)
                else:
                    color = "green"
                self._log(
                    f"  Group [bold]{group:>2}[/bold]: "
                    f"[{color}]{label}[/{color}] [dim]— {escape(detail)}[/dim]"
                )
            elif label == "Rejected":
                self._log(
                    f"  Group [bold]{group:>2}[/bold]: "
                    f"[dim]{label} — {escape(detail)}[/dim]"
                )
            else:
                self._log(
                    f"  Group [bold]{group:>2}[/bold]: "
                    f"[yellow]{label}[/yellow] [dim]— {escape(detail)}[/dim]"
                )

        # Verdict polarity from the auditor's perspective: Dragonblood-vulnerable
        # is GREEN (attack works), not-vulnerable is RED (no attack here).
        if dragonblood_hits:
            self._log(
                f"[bold green]✓ Vulnerable to Dragonblood[/bold green] "
                f"(supported: {', '.join(str(g) for g in dragonblood_hits)}) "
                f"[dim]— CVE-2019-9494/9495 side-channel.[/dim]"
            )
            self._log(
                "[dim]  Next: capture a handshake, then run "
                "[bold]dragonblood-tools[/bold] "
                "(github.com/vanhoefm/dragonblood) to recover the passphrase.[/dim]"
            )
        elif supported_groups:
            self._log(
                f"[bold red]✗ Not vulnerable to Dragonblood[/bold red] "
                f"(supported: {', '.join(str(g) for g in supported_groups)})"
            )
        else:
            self._log(
                "[bold yellow]⚠ Unable to determine supported SAE groups[/bold yellow] "
                "[dim]— AP may have rate-limited us, PMF is rejecting "
                "unauthenticated Auth, or we're off-channel.[/dim]"
            )

    def _toggle_wpa3_down(self) -> None:
        """Button handler: Start the spoof daemon if idle, Stop it if running."""
        if self._wpa3_down_attack:
            self._stop_wpa3_down()
        else:
            self._start_wpa3_down()

    def _start_wpa3_down(self) -> None:
        ap = self.target_ap
        iface = getattr(self.app, "active_interface", None)
        if not ap or not iface:
            self._log("[red]✗ No target / interface — cannot start WPA3 Down.[/red]")
            return
        if not ap.ssid:
            self._log(
                "[yellow]⚠ Cannot run WPA3 Down on a hidden AP — "
                "SSID unknown, no probe-response payload to forge.[/yellow]"
            )
            return
        if not ap.transition_mode:
            self._log(
                "[yellow]⚠ Target is pure WPA3 (no WPA2 fallback advertised) — "
                "downgrade not possible.[/yellow]"
            )
            return
        try:
            self._wpa3_down_attack = WPA3DowngradeAttack(
                iface, ap, log_callback=self._log
            )
            self._wpa3_down_attack.start()
        except Exception as exc:
            logger.exception("WPA3 Down start failed")
            self._log(f"[bold red]✗ WPA3 Down failed to start:[/bold red] {escape(str(exc))}")
            self._wpa3_down_attack = None
            return
        self._log(
            f"[bold cyan]→ WPA3 Downgrade ACTIVE[/bold cyan] on "
            f"[bold]{escape(ap.ssid)}[/bold] ({ap.bssid}) — watching for probe "
            f"requests. Natural reconnection may take minutes to hours."
        )

    def _toggle_generate_ivs(self) -> None:
        """Button handler. Running campaign → stop. No campaign (incl. one that
        already finished and was torn down) → start fresh."""
        camp = self._wep_campaign
        if camp is not None and camp.recovered_key is None:
            self._stop_generate_ivs()
        else:
            if camp is not None:      # a finished campaign still around — clear it
                self._stop_generate_ivs()
            self._start_generate_ivs()

    def _start_generate_ivs(self) -> None:
        ap = self.target_ap
        iface = getattr(self.app, "active_interface", None)
        if not ap or not iface:
            self._log("[red]✗ No target / interface — cannot Generate IVs.[/red]")
            return
        try:
            self._wep_campaign = WepCampaign(iface, ap, log_callback=self._log)
            self._wep_campaign.start()
        except Exception as exc:
            logger.exception("Generate IVs start failed")
            self._log(f"[bold red]✗ Generate IVs failed to start:[/bold red] {escape(str(exc))}")
            self._wep_campaign = None

    def _stop_generate_ivs(self) -> None:
        if not self._wep_campaign:
            return
        you_mac = ":".join(
            f"{b:02x}" for b in self._wep_campaign.fake_auth.source_mac
        )
        self._wep_campaign.stop()
        self._wep_campaign = None
        # The campaign dropped the YOU client from iface.clients; clear its
        # table row + any selection so it doesn't linger as a stale target.
        self._known_clients.discard(you_mac)
        self._selected_clients.discard(you_mac)
        try:
            self.query_one("#client-table", DataTable).remove_row(you_mac)
        except Exception:
            pass

    def _toggle_frag(self) -> None:
        """Frag button. It's a sub-mode of a running campaign — switches the
        radio from ARP replay to fragmentation (and back). Click-to-toggle."""
        camp = self._wep_campaign
        if camp is None:
            self._log(
                "[yellow]Start Generate IVs first[/yellow] [dim](Frag "
                "manufactures an ARP seed for the replay engine)[/dim]"
            )
            return
        if camp.frag_active:
            camp.stop_frag()
            self._log("[cyan]→ Frag stopped[/cyan] [dim](back to ARP replay)[/dim]")
        else:
            # The daemon logs its own one-liner (waiting → seeded → worked); no
            # need for a separate start line back-to-back with it.
            camp.start_frag()
        self.update_ui()

    def _toggle_chop(self) -> None:
        """Chop button — sibling of Frag (mutually exclusive). Byte-by-byte
        ICV-oracle decryption to forge a seed when frag gets no response."""
        camp = self._wep_campaign
        if camp is None:
            self._log(
                "[yellow]Start Generate IVs first[/yellow] [dim](ChopChop "
                "manufactures an ARP seed for the replay engine)[/dim]"
            )
            return
        if camp.chop_active:
            camp.stop_chop()
            self._log("[cyan]→ Chop stopped[/cyan] [dim](back to ARP replay)[/dim]")
        else:
            # The daemon logs its own tree (waiting → chopping IV → steps →
            # worked/gave-up); no separate start line back-to-back with it.
            camp.start_chop()
        self.update_ui()

    def _stop_wpa3_down(self) -> None:
        if not self._wpa3_down_attack:
            return
        stats = self._wpa3_down_attack.stop()
        self._wpa3_down_attack = None
        duration = max(1, int(time.time() - stats.started_at))
        self._log(
            f"[bold red]✗ WPA3 Downgrade STOPPED[/bold red] after "
            f"{_format_duration(duration)} — "
            f"saw {stats.probes_seen} probes "
            f"({stats.directed_probes} directed, {stats.wildcard_probes} wildcard), "
            f"sent {stats.responses_sent} forged responses"
            + (f" ({stats.responses_failed} failed)" if stats.responses_failed else "")
            + "."
        )

    async def action_go_back(self) -> None:
        # Tear down the WPA3 Down daemon if running — Scanner view doesn't own
        # the AP's channel and the RX callback would keep firing forever.
        self._stop_wpa3_down()
        # Same for the Generate IVs campaign — stop injecting + drop the YOU
        # client on exit.
        self._stop_generate_ivs()
        # Hopper restart happens in ScannerView.on_screen_resume — it owns
        # the channel filter; restarting here would silently widen it.
        self.app.pop_screen()

    # ----- Save --------------------------------------------------------------

    def _save_capture(self) -> None:
        ap = self.target_ap
        if not ap or not ap.has_capture:
            self._log("[yellow]⚠ Nothing to save yet.[/yellow]")
            return

        # WEP: there's no handshake/pcap — just write the recovered key.
        if ap.wep_key is not None:
            self._save_wep_key(ap)
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

    def _save_wep_key(self, ap: AccessPoint) -> None:
        """Write the recovered WEP key to captures/<ssid>_<bssid>_<ts>_wepkey.txt."""
        key = ap.wep_key
        captures_dir = Path("captures")
        safe_ssid = re.sub(r"[^A-Za-z0-9_-]", "_", ap.ssid or "hidden")[:24] or "hidden"
        safe_bssid = ap.bssid.replace(":", "-")
        path = captures_dir / f"{safe_ssid}_{safe_bssid}_{int(time.time())}_wepkey.txt"
        ascii_form = (
            key.decode("ascii") if all(0x20 <= b < 0x7F for b in key) else ""
        )
        try:
            captures_dir.mkdir(parents=True, exist_ok=True)
            lines = [
                f"SSID:  {ap.ssid or '<hidden>'}",
                f"BSSID: {ap.bssid}",
                f"WEP key (hex):   {key.hex()}",
            ]
            if ascii_form:
                lines.append(f'WEP key (ASCII): "{ascii_form}"')
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as exc:
            logger.exception("Save WEP key failed")
            self._log(f"[bold red]✗ Save failed:[/bold red] {escape(str(exc))}")
            return
        self._log(
            f"[bold green]✓ Saved WEP key[/bold green] → [bold]{escape(str(path))}[/bold]"
        )
