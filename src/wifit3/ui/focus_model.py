"""View-model for the Focus screen."""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Optional

from rich.markup import escape

from .encryption_format import format_encryption_markup
from ..campaigns.campaign import Campaign
from ..campaigns.pmkid import PmkidHarvestAttack
from ..campaigns.wep import WepCampaign
from wifit3.crack.wep import CRACK_READY_THRESHOLD
from wifit3.crack.handshake import pmkid_crackable
from wifit3.persist.config import Config
from ..campaigns.pin import WpsCampaign
from ..campaigns.deauth import DeauthCampaign
from ..campaigns.eviltwin import EvilTwinCampaign

# Attack-button campaigns in button-row order.
BUTTON_CAMPAIGNS = [WepCampaign, DeauthCampaign, PmkidHarvestAttack, WpsCampaign, EvilTwinCampaign]
_BUTTON_ORDER = ["btn-gen-ivs", "btn-chop", "btn-deauth", "btn-pmkid", "btn-wps-pin", "btn-eviltwin"]


@dataclass
class Campaigns:
    """The live attack-campaign handles a Focus screen owns."""
    wep: Optional[WepCampaign] = None
    wps: Optional[WpsCampaign] = None
    deauth: Optional[DeauthCampaign] = None
    eviltwin: Optional[EvilTwinCampaign] = None
    pbc_busy: bool = False


def other_long_running_tx(exclude: str = "") -> bool:
    """True if a campaign OTHER than ``exclude`` owns the radio."""
    active = Campaign.active
    return active is not None and active.key != exclude


def is_wep(ap) -> bool:
    return (ap.encryption or "").upper() == "WEP"


@dataclass
class DashboardRow:
    """One row of the packet dashboard."""
    key: str                       # beacon / data / wep_iv / eapol / inject / deauth
    label: str                     # <= 6-char gutter label
    color: str                     # Rich colour name
    peak: int                      # nominal scale (drives the fake generator)
    as_rate: bool = True           # True -> "N/s", False -> a recent count


@dataclass
class ClientRow:
    bssid: str
    power: int
    packets: int


@dataclass
class FocusSnapshot:
    status: list[str]              # up to 3 headline lines (the focal point); markup
    power_dbm: int
    signal: Optional[float]        # windowed beacons/s; None=warming, ~0=dead (signal bar)
    card_chipset: str
    card_bssid: str | None         # the card's own MAC, when the driver exposes it
    card_dynamic: str              # "● replaying" etc; "" when idle
    buttons: list[str]             # encryption-conditional attack-button labels
    ap_essid: str
    ap_bssid: str
    ap_channel: int
    ap_encryption: str             # short markup, e.g. "WPA2"
    dashboard: list[DashboardRow]
    clients: list[ClientRow]
    log_lines: list[str] = field(default_factory=list)


# Dashboard rows by family: WEP shows the wep-iv row, WPA/WPA2/WPA3 the eapol row.
_DASHBOARD_BEACON = DashboardRow("beacon", "beacon", "cyan", 10)
_DASHBOARD_DATA = DashboardRow("data", "data", "blue", 240)
_DASHBOARD_WEP_IV = DashboardRow("wep_iv", "wep iv", "green", 120)
_DASHBOARD_EAPOL = DashboardRow("eapol", "eapol", "green", 4, as_rate=False)
_DASHBOARD_INJECT = DashboardRow("inject", "inject", "orange1", 30)
_DASHBOARD_DEAUTH = DashboardRow("deauth", "deauth", "red", 12)


def dashboard_rows(ap) -> list[DashboardRow]:
    """The 5 dashboard rows for this target's family."""
    enc = (ap.encryption or "").upper()
    rows = [_DASHBOARD_BEACON, _DASHBOARD_DATA]
    if enc == "WEP":
        rows.append(_DASHBOARD_WEP_IV)
    elif enc not in ("OPEN", ""):
        rows.append(_DASHBOARD_EAPOL)
    rows += [_DASHBOARD_INJECT, _DASHBOARD_DEAUTH]
    return rows


def format_duration(seconds: int) -> str:
    """Human-readable duration for the 'Last Beacon' line (5s, 1m 12s, etc)."""
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


def truncate_ssid(ssid: str, maxlen: int = 24) -> str:
    """Ellipsize an SSID that overflows the endpoint width '  …'."""
    if len(ssid) <= maxlen:
        return ssid
    return ssid[:maxlen - 1].rstrip() + "…"


def beacon_rate(ap, samples: deque, now: float, window_s: float = 5.0):
    """Windowed beacons/s + cumulative count."""
    samples.append((now, ap.beacons))
    while len(samples) > 1 and now - samples[0][0] > window_s:
        samples.popleft()
    oldest_t, oldest_n = samples[0]
    span = now - oldest_t
    rate = (ap.beacons - oldest_n) / span if span >= 1.0 else None
    return rate, ap.beacons


def _fmt_eta(secs: Optional[float]) -> str:
    if secs is None:
        return "?"
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        return f"{int(secs / 60)}m"
    return f"{secs / 3600:.1f}h"


def _compact_count(n: int) -> str:
    """Width-bounded counter: 0..999 verbatim, then 1.5k / 15k."""
    if n < 1000:
        return str(n)
    if n < 10000:
        return f"{n / 1000:.1f}k"            # 1500 -> "1.5k"
    return f"{n // 1000}k"                    # 15000 -> "15k"


def wps_status_markup(camp) -> str:
    """Compact WPS-PIN campaign status: PIN progress + soft/hard lock state."""
    st = camp.state
    if st.found_pin:
        return (f"[black bold on cyan] PIN CRACKED: ✓ "
                f"{escape(st.found_pin)} [/black bold on cyan]")
    tested = _compact_count(st.tested)
    if camp.status == "locked":
        # Countdown updates each tick
        remaining = int(camp.lock_remaining_seconds)
        m, s = divmod(remaining, 60)
        countdown = f"{m}:{s:02d}"
        kind = camp.lock_kind or "soft"
        color = "red" if kind == "hard" else "dark_orange"
        return (f"WPS PIN: [cyan]{tested}[/cyan]/11k · "
                f"[{color}]{kind} {countdown}[/{color}]")
    if camp.status in ("failed", "error"):
        return f"WPS PIN: [red]{camp.status}[/red] [dim]({tested}/11k)[/dim]"
    eta = _fmt_eta(camp.eta_seconds)
    if st.phase == "second_half" and st.first_half:
        # First half locked in: the meaningful keyspace is the second half
        return (f"WPS PIN: [cyan]{st.p2_index}[/cyan]/1k · "
                f"[green]p1={escape(st.first_half)}[/green] [dim]{eta}[/dim]")
    return f"WPS PIN: [cyan]{tested}[/cyan]/11k · [dim]ETA {eta}[/dim]"


def count_handshakes(ap):
    """``(complete, partial, msg_counts)`` across this AP's handshakes."""
    n_complete = sum(hs.complete_instances for hs in ap.handshakes.values())
    n_partial = sum(
        1 for hs in ap.handshakes.values()
        if not hs.is_complete and hs.total_messages > 0
    )
    msg_counts: Counter = Counter()
    for hs in ap.handshakes.values():
        for f in hs.messages:
            if f.msg_num:
                msg_counts[f.msg_num] += 1
    return n_complete, n_partial, msg_counts


def fakeauth_value_markup(campaign, now: float, compact: bool = False) -> str:
    """Just the fake-auth status value (no 'Fake-Auth:' label)."""
    if campaign is None:
        return "[dim]Off[/dim]"
    fa = campaign.fake_auth
    if fa.state == "associated":
        countdown = ""
        if fa.next_reauth_at and not compact:
            secs = max(0, int(fa.next_reauth_at - now))
            countdown = f" [dim](re-auth in {secs}s)[/dim]"
        return f"[green]✓ Associated[/green]{countdown}"
    if fa.state == "authenticating":
        return "[yellow]Associating…[/yellow]"
    if fa.state == "failed":
        return f"[red]Failed: {escape(fa.fail_reason or 'unknown')}[/red]"
    return "[dim]Idle[/dim]"


def wep_status_lines(ap, array, campaign, now: float) -> list[str]:
    """The WEP status footer (v2)."""
    samples = array.wep_store.crack_sample_count(ap.bssid) if array else 0
    n = f"[cyan]{samples:,}[/cyan]" if samples else "[red]0[/red]"
    # /10k tags the crack threshold (distinct from the gross "wep iv" rate above)
    ivs = (n if samples >= CRACK_READY_THRESHOLD
           else f"{n}[dim]/{CRACK_READY_THRESHOLD // 1000}k[/dim]")
    lines = []
    if campaign is not None:
        lines.append(
            f"[dim]Fake-Auth:[/dim] {fakeauth_value_markup(campaign, now, compact=True)}")
    lines.append(f"[dim]Usable IVs:[/dim] {ivs}")
    return lines


def encryption_chip(ap) -> str:
    """The encryption family for the 'Target acquired' log."""
    return format_encryption_markup(ap, detailed=False)


def pmf_status_markup(ap) -> str:
    """PMF status for the Focus footer.
    Disabled (dim) → Optional (orange) → Required (red)."""
    if ap.pmf_required:
        return "[red]Required[/red]"
    if ap.pmf_capable:
        return "[dark_orange]Optional[/dark_orange]"
    return "[dim]Disabled[/dim]"


def status_footer_lines(ap, array, campaign, now: float) -> list[str]:
    """The dashboard footer lines for this target."""
    if is_wep(ap):
        return wep_status_lines(ap, array, campaign, now)
    lines = [f"[dim]Encryption:[/dim] {format_encryption_markup(ap, detailed=True)}"]
    parts = []
    if ap.akms or ap.wpa3:              # RSN (WPA2/3): PMF is meaningful
        parts.append(f"[dim]PMF:[/dim] {pmf_status_markup(ap)}")
    if getattr(ap, "wps", None):
        lock = "[red]🔒[/red]" if ap.wps_locked else "[green]🔓[/green]"
        ver = f"{ap.wps_version} " if ap.wps_version else ""
        parts.append(f"[dim]WPS:[/dim] {ver}{lock}")
    if parts:
        lines.append("  ·  ".join(parts))
    return lines


@dataclass
class ButtonState:
    """One attack button's derived state."""
    visible: bool = False
    disabled: bool = False
    label: str = ""
    variant: str = "primary"
    reason: str = ""      # why it's disabled (shown as the button tooltip); "" when enabled


def derive_buttons(ap) -> dict[str, ButtonState]:
    """Per-button state, keyed by button id, registry-driven."""
    active = Campaign.active
    silenced = Config.is_silenced(ap.bssid)
    states: dict[str, ButtonState] = {}
    for cls in BUTTON_CAMPAIGNS:
        vis = cls.visible(ap)
        if active is not None and active.key == cls.key and cls.stoppable:
            states[cls.button_id] = ButtonState(
                visible=vis, disabled=False,
                label=cls.run_label, variant=cls.run_variant)
        else:
            other = active is not None and active.key != cls.key
            reason = "AP silenced" if silenced else cls.ineligible_reason(ap)
            states[cls.button_id] = ButtonState(
                visible=vis,
                disabled=reason is not None or other,
                label=cls.idle_label, variant=cls.idle_variant,
                reason=reason or ("radio busy (another attack running)" if other else ""))
    # ChopChop: a WEP sub-action, enabled only while the WEP campaign runs.
    wep_running = active is not None and active.key == "wep"
    chopping = wep_running and getattr(active, "chop_active", False)
    states["btn-chop"] = ButtonState(
        visible=WepCampaign.visible(ap),
        disabled=not wep_running or silenced,
        label="Stop Chop" if chopping else "ChopChop",
        variant="warning" if chopping else "primary",
    )
    return states


def deauth_blocked(ap) -> bool:
    """Deauth bursts are dead when a campaign owns the radio OR the AP requires PMF."""
    return other_long_running_tx() or ap.pmf_required


def client_rows(ap, array) -> list[ClientRow]:
    """The target's real clients."""
    rows: list[ClientRow] = []
    forged = array.forged_macs
    for mac, client in array.clients.items():
        if client.bssid != ap.bssid:
            continue
        if mac in forged:
            continue
        rows.append(ClientRow(bssid=mac, power=client.signal, packets=client.packets))
    return rows


def card_dynamic(campaigns: Campaigns) -> str:
    """What the card is doing right now, shown under the card art."""
    if campaigns.wep is not None:
        if getattr(campaigns.wep, "chop_active", False):
            return "● chopping"
        return "● replaying"
    if campaigns.wps is not None:
        return "● WPS PIN"
    if campaigns.deauth is not None:
        return "● Deauth"
    if campaigns.eviltwin is not None:
        return "● EvilTwin"
    if campaigns.pbc_busy:
        return "● WPS PBC"
    return ""


def wep_action_phrase(campaign) -> str:
    """What the WEP campaign's TX side is doing right now."""
    if getattr(campaign, "chop_active", False):
        return "Chopping a packet"
    state = getattr(getattr(campaign, "replay", None), "state", None)
    return {
        "replaying": "Replaying ARP",
        "testing": "Testing a packet",
        "waiting-arp": "Waiting for a packet",
        "waiting-auth": "Associating",
        "paused": "Paused",
    }.get(state, "Listening for a packet")


def derive_headline(ap, array, campaigns: Campaigns) -> list[str]:
    """The Campaign headline: up to 3 markup lines holding current activity."""
    enc = (ap.encryption or "").upper()
    wep = enc == "WEP"

    # 1. WEP active attack: cracking / replaying / chopping.
    camp = campaigns.wep
    if camp is not None:
        n_ivs = ap.wep.unique_ivs if ap.wep else 0
        cracker_samples = getattr(getattr(camp, "cracker", None), "sample_count", 0)
        action = wep_action_phrase(camp)
        if cracker_samples >= CRACK_READY_THRESHOLD:
            # Replay/chop and cracking run concurrently.
            return [f"[bold cyan]● {action}[/bold cyan] & "
                    f"[bold cyan]Cracking[/bold cyan] WEP key",
                    f"[dim]{cracker_samples:,} usable IVs[/dim]"]
        if camp.chop_active:
            return ["[bold cyan]● ChopChop[/bold cyan] forging an ARP seed",
                    f"[dim]{n_ivs:,} IVs captured[/dim]"]
        suffix = " [dim]for IVs[/dim]" if action == "Replaying ARP" else ""
        return [f"[bold green]● {action}[/bold green]{suffix}",
                f"[dim]{n_ivs:,} IVs · cracks at "
                f"{CRACK_READY_THRESHOLD // 1000}k usable[/dim]"]

    # 2. Live WPS attack.
    wps = campaigns.wps
    if campaigns.pbc_busy:
        return ["[bold green]● WPS PushButton[/bold green] window: capturing PSK"]
    if wps is not None:
        if wps.state.found_pin:
            return ["[black bold on green] ✓ WPS PIN cracked [/black bold on green]",
                    f"[dim]PIN {escape(wps.state.found_pin)}[/dim]"]
        return ["[bold cyan]● WPS PIN brute-force[/bold cyan]",
                f"[dim]{wps_status_markup(wps)}[/dim]"]

    # 3. EvilTwin campaign running.
    if campaigns.eviltwin is not None:
        camp = campaigns.eviltwin
        if camp.captured:
            return ["[black bold on green] ✓ Captured [/black bold on green] crackable M2",
                    "[dim]saved to captures/[/dim]"]
        stats = getattr(camp.fakeap, "stats", None)
        if stats is None:
            return [f"[bold cyan]EvilTwin arming…[/bold cyan] on CH {camp.twin_channel}"]
        return [f"[bold cyan]EvilTwin active[/bold cyan] on CH {camp.twin_channel}",
                f"[dim]auth:{stats.auth} · assoc:{stats.assoc} · M2:{stats.m2}[/dim]",
                f"[dim]probes: {stats.probes_direct} direct · "
                f"{stats.probes_wildcard} wildcard[/dim]"]

    # 3b. Deauth campaign running: provoking a re-handshake for the passive capture.
    deauth = campaigns.deauth
    if deauth is not None:
        return ["[bold cyan]● Deauth[/bold cyan] forcing a re-handshake",
                f"[dim]client acks:{deauth.client_acks}/{deauth.client_sent} · "
                f"bcast:{deauth.bcast_sent}[/dim]"]

    # 4. Recovered credentials, when idle: WEP key / WPS PSK.
    if ap.wep_key is not None or any(p.type == "WEP" for p in ap.persisted):
        return ["[black bold on green] ✓ WEP key recovered [/black bold on green]",
                "[dim]see the event log for the key[/dim]"]
    if ap.known_psk:
        return ["[black bold on green] ✓ WPS PSK recovered [/black bold on green]",
                "[dim]see the event log for the passphrase[/dim]"]

    if Config.is_silenced(ap.bssid):
        return ["[dim]● Silenced[/dim]",
                "[dim]campaigns off, handshakes ignored · press s to resume[/dim]"]

    # 4-5. Passive capture state: captured / partial / listening.
    if wep:
        n_ivs = ap.wep.unique_ivs if ap.wep else 0
        if n_ivs:
            return ["[green]● Listening for WEP IVs[/green]",
                    f"[dim]{n_ivs:,} captured · press Replay to generate more[/dim]"]
        return ["[green]● Listening for WEP IVs[/green]"]

    n_complete, n_partial, msg_counts = count_handshakes(ap)
    n_pmkid = sum(1 for hs in ap.handshakes.values() if hs.pmkid and pmkid_crackable(hs))
    if n_complete or n_pmkid:
        bits = []
        if n_complete:
            bits.append(f"handshake ×{n_complete}")
        if n_pmkid:
            bits.append(f"PMKID ×{n_pmkid}")
        return ["[black bold on green] ✓ Captured [/black bold on green] " + " · ".join(bits),
                "[dim]saved to captures/[/dim]"]
    if n_partial:
        breakdown = " · ".join(f"M{m}×{msg_counts[m]}" for m in sorted(msg_counts))
        return ["[yellow]◌ Capturing handshake[/yellow]",
                f"[dim]{breakdown}: deauth a client to force a re-handshake[/dim]"]
    if enc in ("OPEN", ""):
        return ["[dim]● Open network: no handshake to capture[/dim]"]
    return ["[green]● Listening for handshake + PMKID[/green]",
            "[dim]passive: deauth a client to force a handshake[/dim]"]


def card_identity(source) -> tuple[str, str | None]:
    """``(chipset/label, own_bssid_or_None)`` for the card endpoint. ``source`` is the WlanArray
    (or a bare interface): a one-card pool shows that card's chipset + MAC, a multi-card pool the
    count."""
    if source is None:
        return "no card", None
    members = getattr(source, "members", None)
    if members is not None:                 # a WlanArray
        if not members:
            return "no card", None
        if len(members) > 1:
            return f"{len(members)} cards", None
        source = members[0]                 # a pool of one: describe that single card
    driver = getattr(source, "driver", None)
    label = getattr(source, "chipset", None)
    if not label:
        # legacy fallback: strip the "(Make Model)" suffix off a description/name
        label = str(getattr(source, "description", None)
                    or getattr(source, "name", None) or "card").split("(")[0].strip()
    label = label or "card"
    mac = getattr(driver, "mac_address", None)
    if isinstance(mac, (bytes, bytearray)) and len(mac) == 6:
        mac = ":".join(f"{b:02x}" for b in mac)
    return str(label), (str(mac) if mac else None)


# ---------------------------------------------------------------------------
# Snapshot factory (v2) + the demo snapshot (no-target fallback / screenshots).
# ---------------------------------------------------------------------------


def build_snapshot(ap, array, campaigns: Campaigns, samples: deque,
                   now: float) -> FocusSnapshot:
    """Compose a :class:`FocusSnapshot` from the derivations for the v2 layout."""
    rate, _count = beacon_rate(ap, samples, now)
    chipset, card_bssid = card_identity(array)
    btns = derive_buttons(ap)
    button_labels = [btns[bid].label for bid in _BUTTON_ORDER if btns[bid].visible]
    clients = client_rows(ap, array) if array else []
    essid = truncate_ssid(ap.ssid) if ap.ssid else "‹hidden›"
    return FocusSnapshot(
        status=derive_headline(ap, array, campaigns),
        power_dbm=ap.signal,
        signal=rate,
        card_chipset=chipset,
        card_bssid=card_bssid,
        card_dynamic=card_dynamic(campaigns),
        buttons=button_labels,
        ap_essid=essid,
        ap_bssid=ap.bssid,
        ap_channel=ap.channel,
        ap_encryption=format_encryption_markup(ap, detailed=True),
        dashboard=dashboard_rows(ap),
        clients=clients,
        log_lines=[],
    )


def fake_snapshot() -> FocusSnapshot:
    """The EvilTwin scenario from the redesign mockup."""
    return FocusSnapshot(
        status=[
            "● EvilTwin active",
            "WPA2 twin up · waiting for M1·M2",
            "handshake:  M1 ✓   M2 -",
        ],
        power_dbm=-71,
        signal=6.0,
        card_chipset="rtl8187l",
        card_bssid="00:c0:ca:11:22:33",
        card_dynamic="● EvilTwin",
        buttons=["Extract PMKID", "EvilTwin", "WPS Brute Force"],
        ap_essid="HomeNetwork",
        ap_bssid="a2:b3:c4:d5:e6:f0",
        ap_channel=6,
        ap_encryption="WPA2/CCMP",
        dashboard=[
            DashboardRow("beacon", "beacon", "cyan", 10),
            DashboardRow("data", "data", "blue", 240),
            DashboardRow("eapol", "eapol", "green", 4, as_rate=False),
            DashboardRow("inject", "inject", "orange1", 30),
            DashboardRow("deauth", "deauth", "red", 12),
        ],
        clients=[
            ClientRow("fa:11:22:33:44:aa", -79, 10),
            ClientRow("b2:c3:d4:e5:f6:07", -80, 134),
            ClientRow("9c:b6:d0:1a:2b:3c", -67, 512),
            ClientRow("3a:f1:08:77:aa:01", -83, 22),
            ClientRow("de:ad:be:ef:00:42", -75, 88),
        ],
        log_lines=[
            "19:41:58  Listening on ch 6",
            "19:42:00  Beacon ◂ target AP",
            "19:42:01  Target locked.",
            "19:42:02  2 clients seen",
            "19:42:03  Deauth ▸ ff:ff:ff…",
            "19:42:03  Deauth ▸ fa:11:…:aa",
            "19:42:04  M1 captured (ANonce)",
            "19:42:05  Waiting for M2…",
            "19:42:06  Deauth ▸ b2:c3:…:07",
            "19:42:07  Client reassoc",
            "19:42:08  M1 captured (ANonce)",
            "19:42:09  Waiting for M2…",
        ],
    )
