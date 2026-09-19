"""View-model for the Focus screen."""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

from rich.markup import escape

from .encryption_format import format_encryption_markup
from ..campaigns.campaign import Campaign
from ..campaigns.pmkid import PmkidHarvestAttack
from ..campaigns.wep import WepCampaign
from wifit3.crack.wep import CRACK_READY_THRESHOLD
from wifit3.crack.handshake import pmkid_crackable
from wifit3.models import IdKey, IdSource
from wifit3.persist.config import Config
from ..campaigns.pin import WpsCampaign
from ..campaigns.deauth import DeauthCampaign
from ..campaigns.eviltwin import EvilTwinCampaign

if TYPE_CHECKING:
    from wifit3.models.access_point import AccessPoint

# Attack-button campaigns in button-row order.
BUTTON_CAMPAIGNS = [WepCampaign, DeauthCampaign, PmkidHarvestAttack, WpsCampaign, EvilTwinCampaign]


CAMPAIGN_BY_KEY = {cls.key: cls for cls in BUTTON_CAMPAIGNS}


def campaign_blocked(cls, ap) -> Optional[str]:
    """Why cls's attack button is disabled right now, or None if it can start:
    the AP is silenced, another campaign owns the radio, or the campaign's own
    ineligible_reason (hidden SSID, WPS locked, unconfirmed encryption, …)."""
    if Config.is_silenced(ap.bssid):
        return "AP silenced"
    active = Campaign.active
    if active is not None and active.key != cls.key:
        return f"Blocked ({active.key} is active)"
    return cls.ineligible_reason(ap)


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


def router_identity_details(ap: AccessPoint) -> str | None:
    ident = ap.identity
    if ident is None or not ident.summary:
        return None
    rows = [f"[bold]{escape(ident.summary)}[/bold]"]
    mfr_src: IdSource | None = None
    if ident.model:
        model_src = getattr(ident, "model_source", None) or ident.get(IdKey.MODEL_NAME)[1] or ident.get(IdKey.MODEL_NUMBER)[1]
        src_label = model_src.label if model_src else ""
        rows.append(f"[dim]Model:[/dim] {escape(ident.model)} [dim]({src_label})[/dim]")
    if ident.manufacturer:
        mfr_src = getattr(ident, "manufacturer_source", None) or ident.get(IdKey.MANUFACTURER)[1]
        src_label = mfr_src.label if mfr_src else ""
        rows.append(f"[dim]Manufacturer:[/dim] {escape(ident.manufacturer)} [dim]({src_label})[/dim]")
    if ident.device_name and ident.device_name != ident.model:
        dev_src = ident.get(IdKey.DEVICE_NAME)[1]
        src_label = dev_src.label if dev_src else ""
        rows.append(f"[dim]Device Name:[/dim] {escape(ident.device_name)} [dim]({src_label})[/dim]")
    if ident.serial_number:
        sn_src = ident.get(IdKey.SERIAL_NUMBER)[1]
        src_label = sn_src.label if sn_src else ""
        rows.append(f"[dim]Serial:[/dim] {escape(ident.serial_number)} [dim]({src_label})[/dim]")
    if ident.device_type:
        dt_src = ident.get(IdKey.DEVICE_TYPE)[1]
        src_label = dt_src.label if dt_src else ""
        rows.append(f"[dim]Device Type:[/dim] {escape(ident.device_type)} [dim]({src_label})[/dim]")

    wsc_mfr = ident.get_source_value(IdKey.MANUFACTURER, IdSource.WSC_M1) or ident.get_source_value(IdKey.MANUFACTURER, IdSource.WSC_BEACON)
    if wsc_mfr and wsc_mfr != ident.manufacturer:
        rows.append(f"[dim]Chipset:[/dim] {escape(wsc_mfr)} [dim](WSC)[/dim]")

    oui_vendor = ident.get_source_value(IdKey.MANUFACTURER, IdSource.OUI)
    if oui_vendor and (oui_vendor != ident.manufacturer or mfr_src != IdSource.OUI):
        rows.append(f"[dim]IEEE OUI:[/dim] {escape(oui_vendor)}")

    return "\n".join(rows)



def status_under_dash(ap, array, now: float) -> list[str]:
    """The dashboard footer lines for this target."""
    active = Campaign.active
    if active:
        dash = active.status_under_dash(array, now)
        if dash is not None:
            return dash

    if is_wep(ap):
        return wep_status_lines(ap, array, None, now)
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


def deauth_blocked(ap) -> bool:
    """Deauth bursts are dead when a campaign owns the radio OR the AP requires PMF."""
    return other_long_running_tx() or ap.pmf_required


def status_under_card() -> str:
    """What the card is doing right now, shown under the card art (reads the active campaign)."""
    active = Campaign.active
    if active:
        status = active.status_under_card()
        if status is not None:
            return status
    return ""


def status_headlines(ap, array, vault) -> list[str]:
    """The Campaign headline: up to 3 markup lines of current activity (reads the active campaign)."""
    active = Campaign.active
    if active:
        headlines = active.status_headlines(vault)
        if headlines is not None:
            return headlines

    # Passive capture state (no active campaign on this AP)
    enc = (ap.encryption or "").upper()
    wep = enc == "WEP"

    # 4. Recovered credentials, when idle: WEP key / WPS PSK.
    if ap.wep_key is not None or vault.has_wep_key(ap):
        return ["[black bold on green] ✓ WEP key recovered [/black bold on green]",
                "[dim]see the event log for the key[/dim]"]
    if vault.known_psk(ap):
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
                f"[dim]saved to {Config.captures_dir}[/dim]"]
    if n_partial:
        breakdown = " · ".join(f"M{m}×{msg_counts[m]}" for m in sorted(msg_counts))
        return ["[yellow]◌ Capturing handshake[/yellow]",
                f"[dim]{breakdown}: deauth a client to force a re-handshake[/dim]"]
                
    if getattr(ap, "wpa3", False) and not getattr(ap, "transition_mode", False):
        return ["[dim]● WPA3/SAE: passive capture not applicable[/dim]"]
        
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


