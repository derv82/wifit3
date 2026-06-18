"""Shared Focus view-model — the campaign-value picture, decoupled from layout.

Both the v1 ``FocusView`` and the v2 ``FocusViewV2`` paint the same campaign
derivations; this module holds those derivations as pure functions so neither
screen re-derives them. A function takes ``(ap, iface, campaigns)`` (plus, where
a sliding window is involved, the caller-owned state) and returns a render-ready
string / number / small struct. The layout is then the only disposable part.

Two consumption styles share one set of brains:

* v1 calls the individual ``*_markup`` / ``derive_*`` helpers from ``update_ui``,
  one per Label — a behavior-preserving refactor (its existing markup strings are
  reproduced verbatim, so its tests stay green).
* v2 calls :func:`build_snapshot`, which composes the helpers into a
  :class:`FocusSnapshot` — a per-tick, render-ready description its widgets paint.

Side effects (auto-save, campaign teardown, capture-event logging, PBC spawning)
deliberately stay in the screens — only pure derivations live here.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Optional

from rich.markup import escape

from .encryption_format import (
    format_encryption_markup,
    format_pmf_markup,
)
from ..engine.attacks.wep.crack import CRACK_READY_THRESHOLD

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


def other_long_running_tx(campaigns: Campaigns, exclude: str = "") -> bool:
    """True if any long-running TX activity is running, EXCLUDING the named one
    (``"wep"`` / ``"wpa3down"`` / ``"wps"`` / ``"pbc"``). Drives the cross-attack
    button mutex — the half-duplex radio is never shared. Pass the running
    attack's own name to keep its Stop button live."""
    if exclude != "wep" and campaigns.wep is not None:
        return True
    if exclude != "wpa3down" and campaigns.wpa3_down is not None:
        return True
    if exclude != "wps" and campaigns.wps is not None:
        return True
    if exclude != "pbc" and campaigns.pbc_busy:
        return True
    return False


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
# neither shows on OPEN (mirrors PacketDashboard's encryption gating). The keys
# match wlan.packet_stats.PACKET_CLASSES so the v2 channel can sample by key.
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


def ssid_chip_markup(ap, maxlen: int = 24) -> str:
    """The TARGET name as a black-on-cyan chip (truncated), or an italic
    ‹hidden› marker for a cloaked AP."""
    if ap.ssid:
        return f"[black on cyan] {escape(truncate_ssid(ap.ssid, maxlen))} [/black on cyan]"
    return "[italic cyan]‹hidden›[/italic cyan]"


def last_beacon_markup(ap, now: float) -> str:
    """Staleness chip for the last beacon — doubles as an 'is the card still
    on-channel?' readout. An active AP sits at 'now'; any drift escalates hard
    (orange chip by 1 s, red by 3 s) so a deaf card can't be missed."""
    last_seen_s = max(0, int(now - ap.last_seen))
    if last_seen_s == 0:
        return "[green]now[/green]"
    if last_seen_s < 3:
        return f"[black bold on orange1] {last_seen_s}s [/black bold on orange1]"
    return f"[black bold on red] {format_duration(last_seen_s)} [/black bold on red]"


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


def wps_pmf_markup(ap) -> Optional[str]:
    """The static WPS + PMF line. WPS shows a version + a lock glyph (green 🔓
    attackable, red 🔒 dead end); PMF only in RSN (WPA2/3). None when neither
    applies, so the caller hides the line."""
    if ap.wps:
        lock = "[red]🔒[/red]" if ap.wps_locked else "[green]🔓[/green]"
        ver = f"{ap.wps_version} " if ap.wps_version else ""
        wps_part = f"WPS: {ver}{lock}"
    else:
        wps_part = None
    pmf_part = f"PMF: {format_pmf_markup(ap)}" if (ap.akms or ap.wpa3) else None
    parts = [p for p in (wps_part, pmf_part) if p]
    return "  ·  ".join(parts) if parts else None


def wpa3_down_markup(attack) -> Optional[str]:
    """Live WPA2-downgrade status — the per-probe counts surface here instead of
    flooding the event log. None when the daemon isn't running."""
    if attack is None:
        return None
    st = attack.stats
    return (f"WPA2↓: [bold green]✓ ON[/bold green] "
            f"[dim]({st.directed_probes} dir., {st.wildcard_probes} wild.)[/dim]")


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
        if not hs.is_complete and hs.total_eapol_frames > 0
    )
    msg_counts: Counter = Counter()
    for hs in ap.handshakes.values():
        for f in hs.eapol_frames:
            if f.msg_num:
                msg_counts[f.msg_num] += 1
    return n_complete, n_partial, msg_counts


def handshake_value_markup(ap) -> str:
    """The Handshake line's VALUE (caller prepends 'Handshake: '). Persisted
    (captures/) counts back-fill the live ones so a saved handshake reads
    green + (history) even after a fresh focus."""
    n_complete, n_partial, msg_counts = count_handshakes(ap)
    breakdown = " · ".join(f"M{m}×{msg_counts[m]}" for m in sorted(msg_counts))
    persisted_hs = sum(1 for p in ap.persisted if p.kind == "HS")
    if n_complete:
        t = f"[bold green]Captured x{n_complete}[/bold green]"
        if n_partial:
            t += f" [dim](+{n_partial} partial)[/dim]"
        return t
    if n_partial:
        return f"[yellow]Partial[/yellow] [dim]{breakdown}[/dim]"
    if persisted_hs:
        return f"[bold green]Captured x{persisted_hs}[/bold green] [dim](history)[/dim]"
    return "[dim]Not captured[/dim]"


def pmkid_value_markup(ap) -> str:
    """The PMKID line's VALUE (caller prepends 'PMKID:'). Persisted counts
    back-fill the live ones."""
    n_pmkid = sum(1 for hs in ap.handshakes.values() if hs.pmkid)
    persisted_pmkid = sum(1 for p in ap.persisted if p.kind == "PMKID")
    if n_pmkid:
        return f"[bold green]Captured x{n_pmkid}[/bold green]"
    if persisted_pmkid:
        return f"[bold green]Captured x{persisted_pmkid}[/bold green] [dim](history)[/dim]"
    return "[dim]Not captured[/dim]"


def ivs_value_markup(ap, iface) -> str:
    """The WEP IVs line's VALUE (caller prepends 'IVs: '): unique-IV count +
    live rate. The usable (crack-sample) count isn't here — it lives in the
    Crack line (N/10k usable IVs), which is what gates cracking."""
    n = ap.wep.unique_ivs if ap.wep else 0
    rate = iface.wep_store.rate(ap.bssid) if iface else 0.0
    count = f"[bold green]{n:,}[/bold green]" if n else "[red]0[/red]"
    return f"{count} [dim]({rate:.0f}/s)[/dim]"


def replay_status_markup(campaign) -> str:
    """The Replay-status row's value. Surfaces ChopChop too — while it runs,
    replay is paused on purpose, so name WHAT'S running (not a bare 'paused' that
    reads like the attack stalled)."""
    if campaign is None:
        return "[dim]not started[/dim]"
    if campaign.chop_active:
        return "forging packet [dim]via[/dim] [bold cyan]ChopChop[/bold cyan]"
    s = campaign.replay.state
    if s == "replaying":
        # target_pps = the smooth P&O rate, not the jittery measured effective_pps.
        return (f"[green]Replaying ARP[/green] "
                f"[dim]({campaign.replay.target_pps:.0f}pps)[/dim]")
    if s == "testing":
        return "[cyan]Trying candidate ARP…[/cyan]"
    if s == "waiting-arp":
        return "[yellow]waiting for ARP[/yellow]"
    if s == "waiting-auth":
        return "[dim]associating…[/dim]"
    if s == "paused":
        return "[dim]paused[/dim]"
    return "[dim]idle[/dim]"


def crack_section(ap, campaign, samples: int):
    """The two-row Crack section: ``(visible, crack_markup, info_markup)``. Only
    visible during a running campaign or once a key is found (live or persisted).
    ``samples`` is the store's usable-IV count for the BSSID."""
    persisted_wep = next((p for p in ap.persisted if p.kind == "WEP"), None)
    if campaign is None and ap.wep_key is None and persisted_wep is None:
        return False, "", ""
    target_k = CRACK_READY_THRESHOLD // 1000

    if ap.wep_key is not None:
        # Short status here — the full black-on-cyan KEY banner lives in the
        # (wide) EVENT LOG (a 104-bit key is too wide for this column).
        return (True, "Crack: [bold green]✓ Key recovered[/bold green]",
                "[dim]see EVENT LOG[/dim]")
    if campaign is None and persisted_wep is not None:
        return (True,
                "Crack: [bold green]✓ Key recovered[/bold green] [dim](history)[/dim]",
                "[dim]see EVENT LOG[/dim]")
    if samples < CRACK_READY_THRESHOLD:
        return (True, f"Crack: [white]{samples:,}/{target_k}k usable IVs[/white]",
                f"[dim]Crack begins at {target_k}k[/dim]")
    # The store crossed the threshold, but the cracker ingests in batches — until
    # ITS sample_count reaches the threshold it's still spinning up, not crunching.
    sc = campaign.cracker.sample_count if campaign else samples
    status = ("[cyan italic]Starting…[/cyan italic]"
              if sc < CRACK_READY_THRESHOLD else "[cyan]Cracking…[/cyan]")
    return (True, f"Crack: {status} [dim]({sc:,} samples)[/dim]",
            "[dim]Some keys require >40K samples[/dim]")


def fakeauth_value_markup(campaign, now: float, compact: bool = False) -> str:
    """Just the fake-auth status value (no 'Fake-Auth:' label) — the state
    machine: associating / associated (+ re-auth countdown) / failed / idle.
    ``compact`` drops the re-auth countdown (the v2 flow footer is width-tight;
    the countdown stays in v1's wide SECURITY panel)."""
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


def fakeauth_markup(campaign, now: float) -> str:
    """The SECURITY-panel Fake-Auth line (v1): the 'Fake-Auth:' label + value.
    ``Off`` when no campaign; otherwise the fake-auth state machine."""
    return f"Fake-Auth: {fakeauth_value_markup(campaign, now)}"


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
    gradient: Disabled (dim) → Optional (orange) → Required (red). Distinct from
    the Scanner's format_pmf_markup (which greens 'Disabled' from a protection
    POV); here Disabled is neutral info."""
    if ap.pmf_required:
        return "[red]Required[/red]"
    if ap.pmf_capable:
        return "[dark_orange]Optional[/dark_orange]"
    return "[dim]Disabled[/dim]"


def status_footer_lines(ap, iface, campaign, now: float) -> list[str]:
    """The flow-channel footer lines for this target, painted in the channel's
    vertical slack below the sparklines. WEP → fake-auth + usable IVs; every
    other family → the encryption string + (for RSN) the PMF status."""
    if is_wep(ap):
        return wep_status_lines(ap, iface, campaign, now)
    lines = [f"[dim]Encryption:[/dim] {format_encryption_markup(ap, detailed=True)}"]
    if ap.akms or ap.wpa3:              # RSN (WPA2/3) — PMF is meaningful
        lines.append(f"[dim]Protected Mgmt Frames:[/dim] {pmf_status_markup(ap)}")
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


@dataclass
class ButtonStates:
    gen_ivs: ButtonState
    chop: ButtonState
    pmkid: ButtonState
    wps_pin: ButtonState
    wpa3_down: ButtonState


def derive_buttons(ap, campaigns: Campaigns) -> ButtonStates:
    """Per-attack visibility + enablement. WEP and WPA targets get disjoint
    sets: the WPA buttons (PMKID / WPS / WPA↓) are meaningless for WEP, so they
    hide and Replay/Chop take their place. Pure — the screen does any campaign
    teardown (e.g. on a recovered key) BEFORE calling this, so it reads the
    current handles."""
    wep = is_wep(ap)

    # --- Replay (WEP campaign switch) + Chop (its sub-attack) ---
    wep_running = campaigns.wep is not None
    gen = ButtonState(
        visible=wep,
        disabled=False,
        label="Stop Replay" if wep_running else "ARP Replay",
        variant="error" if wep_running else "success",
    )
    chopping = bool(campaigns.wep and campaigns.wep.chop_active)
    chop = ButtonState(
        visible=wep,
        disabled=not wep_running,
        label="Stop Chop" if chopping else "ChopChop",
        variant="warning" if chopping else "primary",
    )

    # --- PMKID (one-shot; SAE PMKID isn't crackable → WPA2 + transition only) ---
    pmkid_eligible = (not ap.wpa3) or ap.transition_mode
    pmkid = ButtonState(
        visible=not wep and pmkid_eligible,
        disabled=not pmkid_eligible or other_long_running_tx(campaigns),
        label="PMKID",
        variant="primary",
    )

    # --- WPS PIN (Start/Stop toggle; WPS-capable + unlocked) ---
    if campaigns.wps is not None:
        wps_pin = ButtonState(
            visible=not wep and bool(ap.wps),
            disabled=False, label="Stop PIN", variant="error",
        )
    else:
        wps_eligible = bool(ap.wps and not ap.wps_locked)
        wps_pin = ButtonState(
            visible=not wep and bool(ap.wps),
            disabled=not wps_eligible or other_long_running_tx(campaigns, exclude="wps"),
            label="WPS PIN", variant="primary",
        )

    # --- WPA Downgrade (WPA3-transition APs only; Start/Stop toggle) ---
    wpa3down_eligible = bool(ap.wpa3 and ap.transition_mode)
    if campaigns.wpa3_down is not None:
        wpa3_down = ButtonState(visible=wpa3down_eligible, disabled=False,
                                label="Stop ↓", variant="primary")
    else:
        wpa3_down = ButtonState(
            visible=wpa3down_eligible,
            disabled=not wpa3down_eligible or other_long_running_tx(campaigns, exclude="wpa3down"),
            label="WPA ↓", variant="primary",
        )

    return ButtonStates(gen_ivs=gen, chop=chop, pmkid=pmkid,
                        wps_pin=wps_pin, wpa3_down=wpa3_down)


def deauth_blocked(ap, campaigns: Campaigns) -> bool:
    """Deauth bursts are dead when another long-running TX owns the radio OR the
    AP requires PMF (it rejects unauthenticated deauth). The cursor gate
    (Selected needs a highlighted row) is the caller's — it's a UI-state concern,
    not derivable from ``(ap, iface, campaigns)``."""
    return other_long_running_tx(campaigns) or ap.pmf_required


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
    btns = derive_buttons(ap, campaigns)
    button_labels = [b.label for b in (btns.gen_ivs, btns.chop, btns.pmkid,
                                       btns.wps_pin, btns.wpa3_down) if b.visible]
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
