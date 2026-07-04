"""Focus view-model — the campaign-value picture, decoupled from layout.

``FocusViewV2`` paints these campaign derivations; this module holds them as pure
functions so the screen never re-derives them. A function takes
``(ap, iface, campaigns)`` (plus, where a sliding window is involved, the
caller-owned state) and returns a render-ready string / number / small struct.
The screen calls :func:`build_snapshot`, which composes the helpers into a
:class:`FocusSnapshot` — a per-tick, render-ready description its widgets paint.

Side effects (auto-save, campaign teardown, capture-event logging, PBC spawning)
deliberately stay in the screen — only pure derivations live here.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Optional

from rich.markup import escape

from .encryption_format import format_encryption_markup
from ..engine.attacks.campaign import Campaign
from ..engine.attacks.pmkid_harvest import PmkidHarvestAttack
from ..engine.attacks.wep.campaign import WepCampaign
from ..engine.attacks.wep.crack import CRACK_READY_THRESHOLD
from ..engine.attacks.wps.campaign import WpsCampaign
from ..engine.attacks.wpa3_downgrade import WPA3DowngradeAttack

# Attack-button campaigns in button-row order. Each declares its own
# visible()/ineligible_reason() + labels; running-state comes from the
# class-level Campaign.active mutex. WEP's ChopChop is a sub-action of the WEP
# campaign (not its own entry), painted specially in derive_buttons.
BUTTON_CAMPAIGNS = [WepCampaign, PmkidHarvestAttack, WpsCampaign, WPA3DowngradeAttack]
_BUTTON_ORDER = ["btn-gen-ivs", "btn-chop", "btn-pmkid", "btn-wps-pin", "btn-wpa3-down"]

# ---------------------------------------------------------------------------
# Inputs the screens hand to the derivations.
# ---------------------------------------------------------------------------


@dataclass
class Campaigns:
    """The live attack-campaign handles a Focus screen owns, bundled so the
    derivations stay decoupled from the screen instance. All optional — an idle
    target has every field at its default. ``pbc_busy`` is a bool (not a task)
    because the WPS-PBC capture is a fire-and-forget asyncio task, not a campaign
    object the derivations introspect."""
    wep: Any = None                # WepCampaign | None
    wps: Any = None                # WpsCampaign | None
    wpa3_down: Any = None          # WPA3DowngradeAttack | None
    pbc_busy: bool = False


def other_long_running_tx(exclude: str = "") -> bool:
    """True if a campaign OTHER than ``exclude`` owns the radio — the cross-attack
    mutex (the half-duplex radio is never shared). Reads the single
    ``Campaign.active`` slot; pass the running attack's own key (``"wep"`` /
    ``"wps"`` / ``"wpa3down"`` / ``"pmkid"`` / ``"pbc"``) to keep its Stop live."""
    active = Campaign.active
    return active is not None and active.key != exclude


def is_wep(ap) -> bool:
    return (ap.encryption or "").upper() == "WEP"


# ---------------------------------------------------------------------------
# Snapshot shape — what the v2 layout paints.
# ---------------------------------------------------------------------------


@dataclass
class FlowRow:
    """One row of the packet-flow channel."""
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
    flow: list[FlowRow]
    clients: list[ClientRow]
    log_lines: list[str] = field(default_factory=list)


# Flow rows by family — WEP shows the wep-iv row, WPA/WPA2/WPA3 the eapol row;
# neither shows on OPEN. The keys match wlan.packet_stats.PACKET_CLASSES so the
# v2 channel can sample by key.
_FLOW_BEACON = FlowRow("beacon", "beacon", "cyan", 10)
_FLOW_DATA = FlowRow("data", "data", "blue", 240)
_FLOW_WEP_IV = FlowRow("wep_iv", "wep iv", "green", 120)
_FLOW_EAPOL = FlowRow("eapol", "eapol", "green", 4, as_rate=False)
_FLOW_INJECT = FlowRow("inject", "inject", "orange1", 30)
_FLOW_DEAUTH = FlowRow("deauth", "deauth", "red", 12)


def flow_rows(ap) -> list[FlowRow]:
    """The 5 flow-channel rows for this target's family. The key-material row
    (wep_iv vs eapol) is chosen by encryption; OPEN shows neither (4 rows)."""
    enc = (ap.encryption or "").upper()
    rows = [_FLOW_BEACON, _FLOW_DATA]
    if enc == "WEP":
        rows.append(_FLOW_WEP_IV)
    elif enc not in ("OPEN", ""):
        rows.append(_FLOW_EAPOL)
    rows += [_FLOW_INJECT, _FLOW_DEAUTH]
    return rows


# ---------------------------------------------------------------------------
# TARGET — identity + freshness.
# ---------------------------------------------------------------------------


def format_duration(seconds: int) -> str:
    """Human-readable duration for the 'Last Beacon' line.
    Examples: '5s', '1m 12s', '1h 4m', '2d 3h'. Drops the lower unit when it's
    zero to keep the line tight."""
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
    """Ellipsize an SSID that overflows the endpoint width, trimming trailing
    whitespace before the … so a padded name doesn't end '  …'."""
    if len(ssid) <= maxlen:
        return ssid
    return ssid[:maxlen - 1].rstrip() + "…"


def beacon_rate(ap, samples: deque, now: float, window_s: float = 5.0):
    """Windowed beacons/s + cumulative count. A windowed rate (not a
    since-first-seen average) shows how RX is doing *right now*; an average
    flattens out and hides a card going deaf. Appends ``(now, ap.beacons)`` to
    ``samples`` and trims to the window — the caller owns the deque. Returns
    ``(rate_or_None, count)``; the rate is None while warming up (< 1 s)."""
    samples.append((now, ap.beacons))
    while len(samples) > 1 and now - samples[0][0] > window_s:
        samples.popleft()
    oldest_t, oldest_n = samples[0]
    span = now - oldest_t
    rate = (ap.beacons - oldest_n) / span if span >= 1.0 else None
    return rate, ap.beacons


# ---------------------------------------------------------------------------
# SECURITY — WPS / PMF / WPA3-downgrade.
# ---------------------------------------------------------------------------


def _fmt_eta(secs: Optional[float]) -> str:
    if secs is None:
        return "?"
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        return f"{int(secs / 60)}m"
    return f"{secs / 3600:.1f}h"


def _compact_count(n: int) -> str:
    """Width-bounded counter — keeps a value to <=4 chars so the narrow
    SECURITY row never truncates: 0..999 verbatim, then 1.5k / 15k."""
    if n < 1000:
        return str(n)
    if n < 10000:
        return f"{n / 1000:.1f}k"            # 1500 -> "1.5k"
    return f"{n // 1000}k"                    # 15000 -> "15k"


def wps_status_markup(camp) -> str:
    """Compact WPS-PIN campaign status (~29 chars before the SECURITY panel
    truncates). The static WPS/PMF row carries the beacon-level 🔒 (the 'hard
    lock'); this row carries live PIN progress + our soft/hard backoff state."""
    st = camp.state
    if st.found_pin:
        return (f"[black bold on cyan] PIN CRACKED: ✓ "
                f"{escape(st.found_pin)} [/black bold on cyan]")
    tested = _compact_count(st.tested)
    if camp.status == "locked":
        # Countdown updates each tick; kind disambiguates the 🔒 in the row above
        # (hard = AP says no; soft = our own backoff).
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
        # First half locked in — the meaningful keyspace is the second half (1k
        # candidates), so the denominator narrows from 11k to 1k.
        return (f"WPS PIN: [cyan]{st.p2_index}[/cyan]/1k · "
                f"[green]p1={escape(st.first_half)}[/green] [dim]{eta}[/dim]")
    return f"WPS PIN: [cyan]{tested}[/cyan]/11k · [dim]ETA {eta}[/dim]"


# ---------------------------------------------------------------------------
# CAPTURE — handshake / PMKID (WPA) ; IVs / replay / crack / fake-auth (WEP).
# ---------------------------------------------------------------------------


def count_handshakes(ap):
    """``(complete, partial, msg_counts)`` across this AP's handshakes.

    ``complete`` counts distinct captured INSTANCES (by ANonce), so a client
    that re-handshakes several times reads x2/x3 instead of collapsing to one
    Handshake object. ``msg_counts`` is the raw per-message frame tally (the
    4-way validity logic dedups by (msg, replay), but the user still wants every
    landed M1/M3 retry visible as progress)."""
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
    """Just the fake-auth status value (no 'Fake-Auth:' label) — the state
    machine: associating / associated (+ re-auth countdown) / failed / idle.
    ``compact`` drops the re-auth countdown (the flow footer is width-tight)."""
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


def wep_status_lines(ap, iface, campaign, now: float) -> list[str]:
    """The WEP status footer (v2), as separate lines so neither scrunches on a
    narrow terminal: live fake-auth status (only while a campaign runs — some
    routers guard against fake-auth, so its state is vital) on its own line, then
    the always-on usable-IV count (red at 0, cyan once IVs are flowing). 'Usable'
    is the crack-sample count — what the crack threshold (CRACK_READY_THRESHOLD)
    is measured against, so it directly answers 'how close to cracking?'."""
    samples = iface.wep_store.crack_sample_count(ap.bssid) if iface else 0
    n = f"[cyan]{samples:,}[/cyan]" if samples else "[red]0[/red]"
    # /10k tags the crack threshold (distinct from the gross "wep iv" rate above)
    # while we're below it; once crossed the denominator is meaningless, so drop
    # it and just show the climbing count.
    ivs = (n if samples >= CRACK_READY_THRESHOLD
           else f"{n}[dim]/{CRACK_READY_THRESHOLD // 1000}k[/dim]")
    lines = []
    if campaign is not None:
        lines.append(
            f"[dim]Fake-Auth:[/dim] {fakeauth_value_markup(campaign, now, compact=True)}")
    lines.append(f"[dim]Usable IVs:[/dim] {ivs}")
    return lines


def encryption_chip(ap) -> str:
    """The encryption family for the 'Target acquired' log — the Scanner cell's
    own markup (matching its colours), compact form (no pairwise cipher)."""
    return format_encryption_markup(ap, detailed=False)


def pmf_status_markup(ap) -> str:
    """PMF status for the Focus footer — an escalating 'will deauth work?'
    gradient: Disabled (dim) → Optional (orange) → Required (red)."""
    if ap.pmf_required:
        return "[red]Required[/red]"
    if ap.pmf_capable:
        return "[dark_orange]Optional[/dark_orange]"
    return "[dim]Disabled[/dim]"


def status_footer_lines(ap, iface, campaign, now: float) -> list[str]:
    """The flow-channel footer lines for this target, painted in the channel's
    vertical slack below the sparklines. WEP → fake-auth + usable IVs; every
    other family → the encryption string, then a combined PMF + WPS line.

    PMF is abbreviated (the full 'Protected Management Frames' is one long lonely
    grey line when Disabled) and WPS rejoins it on the same row (it was dropped in
    the v2 footer): ``PMF: Disabled  ·  WPS: 1.0 🔓``. The WPS lock glyph is the
    beacon-level attackability (green 🔓 open, red 🔒 locked)."""
    if is_wep(ap):
        return wep_status_lines(ap, iface, campaign, now)
    lines = [f"[dim]Encryption:[/dim] {format_encryption_markup(ap, detailed=True)}"]
    parts = []
    if ap.akms or ap.wpa3:              # RSN (WPA2/3) — PMF is meaningful
        parts.append(f"[dim]PMF:[/dim] {pmf_status_markup(ap)}")
    if getattr(ap, "wps", None):
        lock = "[red]🔒[/red]" if ap.wps_locked else "[green]🔓[/green]"
        ver = f"{ap.wps_version} " if ap.wps_version else ""
        parts.append(f"[dim]WPS:[/dim] {ver}{lock}")
    if parts:
        lines.append("  ·  ".join(parts))
    return lines


# ---------------------------------------------------------------------------
# Attack-button eligibility.
# ---------------------------------------------------------------------------


@dataclass
class ButtonState:
    """One attack button's derived state. ``visible`` hides buttons that can't
    plausibly work against this AP (clutter, not just disable); ``disabled``
    greys an inapplicable-right-now button (e.g. the cross-attack TX mutex)."""
    visible: bool = False
    disabled: bool = False
    label: str = ""
    variant: str = "primary"


def derive_buttons(ap) -> dict[str, ButtonState]:
    """Per-button state, keyed by button id — registry-driven. Each Campaign class
    declares its own visible()/ineligible_reason() + labels; running-state is the
    single ``Campaign.active`` mutex (its Stop button, everyone else disabled).
    WEP's ChopChop is a WEP sub-action (live only while the WEP campaign runs),
    painted specially. Pure — the screen tears down finished campaigns BEFORE
    calling this, so ``Campaign.active`` is current."""
    active = Campaign.active
    states: dict[str, ButtonState] = {}
    for cls in BUTTON_CAMPAIGNS:
        vis = cls.visible(ap)
        if active is not None and active.key == cls.key and cls.stoppable:
            states[cls.button_id] = ButtonState(
                visible=vis, disabled=False,
                label=cls.run_label, variant=cls.run_variant)
        else:
            other = active is not None and active.key != cls.key
            states[cls.button_id] = ButtonState(
                visible=vis,
                disabled=cls.ineligible_reason(ap) is not None or other,
                label=cls.idle_label, variant=cls.idle_variant)
    # ChopChop — a WEP sub-action, enabled only while the WEP campaign runs.
    wep_running = active is not None and active.key == "wep"
    chopping = wep_running and getattr(active, "chop_active", False)
    states["btn-chop"] = ButtonState(
        visible=WepCampaign.visible(ap),
        disabled=not wep_running,
        label="Stop Chop" if chopping else "ChopChop",
        variant="warning" if chopping else "primary",
    )
    return states


def deauth_blocked(ap) -> bool:
    """Deauth bursts are dead when a campaign owns the radio OR the AP requires PMF
    (it rejects unauthenticated deauth). The cursor gate (Selected needs a
    highlighted row) is the caller's — a UI-state concern, not derivable here."""
    return other_long_running_tx() or ap.pmf_required


# ---------------------------------------------------------------------------
# Clients.
# ---------------------------------------------------------------------------


def client_rows(ap, iface) -> list[ClientRow]:
    """The target's real clients — our own forged/self STAs (fake-auth, replay
    source, PMKID client) are filtered out, they aren't the target's devices."""
    rows: list[ClientRow] = []
    forged = iface.forged_macs
    for mac, client in iface.clients.items():
        if client.bssid != ap.bssid:
            continue
        if mac in forged or client.is_self:
            continue
        rows.append(ClientRow(bssid=mac, power=client.signal, packets=client.packets))
    return rows


# ---------------------------------------------------------------------------
# Card endpoint dynamic line + the synthesized CAMPAIGN HEADLINE.
# ---------------------------------------------------------------------------


def card_dynamic(campaigns: Campaigns) -> str:
    """What the card is doing right now, shown under the card art. Empty when
    idle (passive capture). Deauth is a brief one-shot burst (no held handle),
    so it isn't surfaced here."""
    if campaigns.wep is not None:
        if getattr(campaigns.wep, "chop_active", False):
            return "● chopping"
        return "● replaying"
    if campaigns.wps is not None:
        return "● WPS PIN"
    if campaigns.wpa3_down is not None:
        return "● downgrading"
    if campaigns.pbc_busy:
        return "● WPS PBC"
    return ""


def wep_action_phrase(campaign) -> str:
    """Short present-tense phrase for what the WEP campaign's TX side is doing
    right now — drives the headline (and reads alongside the concurrent crack).
    A replay can drop back from 'Replaying ARP' to 'Waiting for a packet' or
    'Chopping' mid-crack, so this is read live, not pinned at start."""
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


def derive_headline(ap, iface, campaigns: Campaigns) -> list[str]:
    """The CAMPAIGN HEADLINE — up to 3 markup lines naming the dominant current
    activity (the focal point of the v2 view). Priority, highest first:
    active attack (WEP replay/crack, WPS PBC/PIN, WPA3-down) → recovered creds
    (WEP key / WPS PSK) → captured handshake/PMKID → partial capture → passive
    listening. An ACTIVE attack outranks a past win so re-running it on an
    already-cracked AP (re-test, or after a password change) shows live progress;
    and a recovered credential outranks listening so the win never decays back to
    'Listening'. Line 2/3 carry the salient detail for the headline state."""
    enc = (ap.encryption or "").upper()
    wep = enc == "WEP"

    # 1. WEP active attack — cracking / replaying / chopping. Outranks the
    # recovered-key banner below (a running campaign is the dominant activity even
    # when a key was found on a previous run).
    camp = campaigns.wep
    if camp is not None:
        n_ivs = ap.wep.unique_ivs if ap.wep else 0
        cracker_samples = getattr(getattr(camp, "cracker", None), "sample_count", 0)
        action = wep_action_phrase(camp)
        if cracker_samples >= CRACK_READY_THRESHOLD:
            # Replay/chop and cracking run concurrently — name BOTH. The live TX
            # action (the thermally-intensive part) can drop back to waiting or
            # chopping while the cracker keeps crunching, so surface it, not just
            # a frozen "Cracking".
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

    # 2. Live WPS attack — opportunistic PBC auto-invade or PIN brute-force.
    # Outranks the recovered banner + listening (an active attack is the dominant
    # activity), same as an active WEP campaign above.
    wps = campaigns.wps
    if campaigns.pbc_busy:
        return ["[bold green]● WPS PushButton[/bold green] window — capturing PSK"]
    if wps is not None:
        if wps.state.found_pin:
            return ["[black bold on green] ✓ WPS PIN cracked [/black bold on green]",
                    f"[dim]PIN {escape(wps.state.found_pin)}[/dim]"]
        return ["[bold cyan]● WPS PIN brute-force[/bold cyan]",
                f"[dim]{wps_status_markup(wps)}[/dim]"]

    # 3. WPA3 downgrade daemon running.
    if campaigns.wpa3_down is not None:
        st = campaigns.wpa3_down.stats
        return ["[bold cyan]● WPA Downgrade active[/bold cyan]",
                f"[dim]spoofing WPA2-only · {st.responses_sent} responses[/dim]"]

    # 4. Recovered credentials (terminal win), when idle — WEP key / WPS PSK.
    # known_psk covers a PSK recovered live this session (PBC or PIN) AND a
    # persisted one, so the banner survives the campaign being torn down (the win
    # shouldn't decay back to "Listening").
    if ap.wep_key is not None or any(p.kind == "WEP" for p in ap.persisted):
        return ["[black bold on green] ✓ WEP key recovered [/black bold on green]",
                "[dim]see the event log for the key[/dim]"]
    if ap.known_psk:
        return ["[black bold on green] ✓ WPS PSK recovered [/black bold on green]",
                "[dim]see the event log for the passphrase[/dim]"]

    # 4–5. Passive capture state — captured / partial / listening.
    if wep:
        n_ivs = ap.wep.unique_ivs if ap.wep else 0
        if n_ivs:
            return ["[green]● Listening for WEP IVs[/green]",
                    f"[dim]{n_ivs:,} captured · press Replay to generate more[/dim]"]
        return ["[green]● Listening for WEP IVs[/green]"]

    n_complete, n_partial, msg_counts = count_handshakes(ap)
    n_pmkid = sum(1 for hs in ap.handshakes.values() if hs.pmkid)
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
                f"[dim]{breakdown} — deauth a client to force a re-handshake[/dim]"]
    if enc in ("OPEN", ""):
        return ["[dim]● Open network — no handshake to capture[/dim]"]
    return ["[green]● Listening for handshake + PMKID[/green]",
            "[dim]passive — deauth a client to force a handshake[/dim]"]


# ---------------------------------------------------------------------------
# Card identity (best-effort from the driver).
# ---------------------------------------------------------------------------


def card_identity(iface) -> tuple[str, str | None]:
    """``(chipset/label, own_bssid_or_None)`` for the card endpoint. The chipset
    label prefers a driver-exposed name, falling back to the human card
    description; the BSSID is shown only when the driver exposes its own MAC."""
    if iface is None:
        return "no card", None
    driver = getattr(iface, "driver", None)
    label = (getattr(driver, "chipset", None)
             or getattr(iface, "description", None)
             or getattr(iface, "name", None) or "card")
    # DeviceID descriptions are "chipset / marketing name" (e.g. "Mediatek
    # MT7921AU / ALFA AWUS036AXML"). The endpoint wants just the chipset — and
    # the full string overflows the 20-col card column, truncating to a dangling
    # "/". Keep the part before the slash.
    label = str(label).split("/")[0].strip() or "card"
    mac = getattr(driver, "card_mac", None) or getattr(driver, "mac", None)
    if isinstance(mac, (bytes, bytearray)) and len(mac) == 6:
        mac = ":".join(f"{b:02x}" for b in mac)
    return str(label), (str(mac) if mac else None)


# ---------------------------------------------------------------------------
# Snapshot factory (v2) + the demo snapshot (no-target fallback / screenshots).
# ---------------------------------------------------------------------------


def build_snapshot(ap, iface, campaigns: Campaigns, samples: deque,
                   now: float) -> FocusSnapshot:
    """Compose a :class:`FocusSnapshot` from the derivations for the v2 layout.
    ``samples`` is the caller-owned beacon-rate window (see :func:`beacon_rate`).
    The event log is wired live by the screen, so ``log_lines`` is left empty."""
    rate, _count = beacon_rate(ap, samples, now)
    chipset, card_bssid = card_identity(iface)
    btns = derive_buttons(ap)
    button_labels = [btns[bid].label for bid in _BUTTON_ORDER if btns[bid].visible]
    clients = client_rows(ap, iface) if iface else []
    essid = truncate_ssid(ap.ssid) if ap.ssid else "‹hidden›"
    return FocusSnapshot(
        status=derive_headline(ap, iface, campaigns),
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
        flow=flow_rows(ap),
        clients=clients,
        log_lines=[],
    )


def fake_snapshot() -> FocusSnapshot:
    """The WPA2-downgrade scenario from the redesign mockup — every region
    populated, so the shell exercises the full layout with no live data. Used
    when v2 is opened with no target/interface (the geometry tests, the
    ``shoot_focus_v2.py`` screenshots)."""
    return FocusSnapshot(
        status=[
            "● WPA Downgrade active",
            "deauthing 2 clients · waiting for M1·M2",
            "handshake:  M1 ✓   M2 —",
        ],
        power_dbm=-71,
        signal=6.0,
        card_chipset="rtl8187l",
        card_bssid="00:c0:ca:11:22:33",
        card_dynamic="● deauthing",
        buttons=["Extract PMKID", "WPA Downgrade", "WPS Brute Force"],
        ap_essid="NETGEAR91",
        ap_bssid="a8:fc:b7:0e:1d:42",
        ap_channel=6,
        ap_encryption="WPA2/CCMP",
        flow=[
            FlowRow("beacon", "beacon", "cyan", 10),
            FlowRow("data", "data", "blue", 240),
            FlowRow("eapol", "eapol", "green", 4, as_rate=False),
            FlowRow("inject", "inject", "orange1", 30),
            FlowRow("deauth", "deauth", "red", 12),
        ],
        clients=[
            ClientRow("fa:11:22:33:44:aa", -79, 10),
            ClientRow("04:2e:c1:51:43:b8", -80, 134),
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
            "19:42:06  Deauth ▸ 04:2e:…:b8",
            "19:42:07  Client reassoc",
            "19:42:08  M1 captured (ANonce)",
            "19:42:09  Waiting for M2…",
        ],
    )
