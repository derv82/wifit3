"""View-model for the Focus screen."""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

from rich.markup import escape
from wifit3.wlan.array import WlanArray

from .encryption_format import format_encryption_markup, wep_key_ascii
from ..campaigns.campaign import Campaign
from ..campaigns.pmkid import PmkidHarvestAttack
from ..campaigns.wep import WepCampaign
from wifit3.crack.wep import CRACK_READY_THRESHOLD
from wifit3.crack.handshake import pmkid_crackable
from wifit3.dot11.eap import method_name
from wifit3.dot11.pmf import PmfPosture
from wifit3.dot11.rsn import SAE_AKM_SUITES
from wifit3.models import CaptureType, IdKey, IdSource
from wifit3.models.access_point import eap_only, no_passphrase
from wifit3.ui.encryption_format import psk_capture_supported
from wifit3.persist.config import Config
from ..campaigns.pin import WpsCampaign
from ..campaigns.deauth import DeauthCampaign
from ..campaigns.eviltwin import EvilTwinCampaign
from ..campaigns.decloak import DecloakCampaign

if TYPE_CHECKING:
    from wifit3.models.access_point import AccessPoint
    from wifit3.persist.vault import Vault

# Attack-button campaigns in button-row order.
BUTTON_CAMPAIGNS = [WepCampaign, DeauthCampaign, PmkidHarvestAttack, WpsCampaign,
                    EvilTwinCampaign, DecloakCampaign]


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


# Identity, Notification, Nak: EAP framing, not the authentication method.
_EAP_FRAMING_TYPES = frozenset({1, 2, 3})


def eap_methods_markup(ap: AccessPoint) -> str | None:
    """Observed EAP methods for the Focus footer, or None before any EAPOL-Type-0."""
    seen = ap.observed_eap_methods
    if not seen:
        return None
    types = sorted(t for t in seen if t not in _EAP_FRAMING_TYPES) or sorted(seen)
    labels = [method_name(t) or f"type {t}" for t in types]
    return "[dim]EAP:[/dim] " + " · ".join(labels)


def pmf_status_markup(ap: AccessPoint) -> str:
    """PMF status for the Focus footer, off the 802.11w posture.
    Disabled (dim) → Optional (orange) → Required (red); Invalid (red) = MFPR without MFPC."""
    posture = ap.pmf_posture
    if posture is PmfPosture.REQUIRED:
        return "[red]Required[/red]"
    if posture is PmfPosture.INVALID:
        return "[red]Invalid[/red]"
    if posture is PmfPosture.OPTIONAL:
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



def status_under_dash(ap: AccessPoint, array: WlanArray, now: float) -> list[str]:
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
    if (ap.akms and ap.rsn_valid) or ap.wpa3:   # RSN (WPA2/3): PMF is meaningful
        parts.append(f"[dim]PMF:[/dim] {pmf_status_markup(ap)}")
    if getattr(ap, "wps", None):
        lock = "[red]🔒[/red]" if ap.wps_locked else "[green]🔓[/green]"
        ver = f"{ap.wps_version} " if ap.wps_version else ""
        parts.append(f"[dim]WPS:[/dim] {ver}{lock}")
    eap = eap_methods_markup(ap)
    if eap:
        parts.append(eap)
    if parts:
        lines.append("  ·  ".join(parts))
    return lines


def deauth_blocked(ap: AccessPoint) -> bool:
    """True while radio TX is busy or the AP's PMF is required or invalid."""
    return other_long_running_tx() or ap.pmf_posture in (PmfPosture.REQUIRED, PmfPosture.INVALID)


def status_under_card() -> str:
    """What the card is doing right now, shown under the card art (reads the active campaign)."""
    active = Campaign.active
    if active:
        status = active.status_under_card()
        if status is not None:
            return status
    return ""


def status_headlines(ap: AccessPoint, array: WlanArray, vault: Vault) -> list[str]:
    """The Campaign headline: up to 3 markup lines of current activity (reads the active campaign)."""
    active = Campaign.active
    if active:
        headlines = active.status_headlines(vault)
        if headlines is not None:
            return headlines

    # Passive capture state (no active campaign on this AP)
    enc = (ap.encryption or "").upper()
    wep = enc == "WEP"

    # 802.1X/Enterprise (incl. FILS): login attacks not yet supported; checked before any stale PSK
    # or old 4-way left from a previous (PSK) config can surface a misleading headline.
    if eap_only(ap):
        if ap.wpa3 and ap.transition_mode:
            return ["[dim]● WPA3/Enterprise transition: not yet supported[/dim]"]
        if ap.wpa3:
            return ["[dim]● WPA3/Enterprise: not yet supported[/dim]"]
        wpa_label = "WPA2" if ap.rsn_valid else "WPA"
        return [f"[dim]● {wpa_label}/Enterprise: not yet supported[/dim]"]

    if no_passphrase(ap):
        return ["[dim]● OWE (Enhanced Open): Diffie-Hellman key, nothing to crack[/dim]"]

    unsupported = bool(ap.akm_suites) and psk_capture_supported(ap) is False

    # 4. Recovered credentials, when idle: WEP key / PSK (WPS- or handshake-derived).
    wep_key_hex = (ap.wep_key.hex() if ap.wep_key is not None
                   else next((c.value for c in vault.persisted(ap.bssid)
                              if c.type == CaptureType.WEP and c.value), None))
    if wep_key_hex:
        return [f"[black bold on green] ✓ WEP key: {escape(wep_key_ascii(wep_key_hex))} [/black bold on green]",
                "[dim]● Listening for WEP IVs[/dim]"]
    psk = vault.known_psk(ap)
    if psk:
        tail = ("[dim]● Not yet supported: no crack path for this AKM[/dim]"
                if unsupported else "[dim]● Listening for handshake + PMKID[/dim]")
        return [f"[black bold on green] ✓ PSK: {escape(psk)} [/black bold on green]", tail]

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

    if (ap.wpa3 and not ap.transition_mode
            and SAE_AKM_SUITES.intersection(ap.akm_suites)):
        return ["[dim]● WPA3/SAE: passive capture not applicable[/dim]"]

    if unsupported:
        return ["[dim]● Not yet supported: no crack path for this AKM[/dim]"]

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

    if enc in ("OPEN", ""):
        return ["[dim]● Open network: no handshake to capture[/dim]"]
    return ["[green]● Listening for handshake + PMKID[/green]",
            "[dim]passive: deauth a client to force a handshake[/dim]"]


def card_identity(array: WlanArray) -> tuple[str, str | None]:
    """``(chipset/label, own_bssid_or_None)`` for the card endpoint."""
    if array is None:
        return "no card", None
    if array.members is None or len(array.members) == 0:
        return "no card", None
    if len(array.members) > 1:
        return f"{len(array.members)} cards", None
    iface = array.preferred or array.members[0]
    label = iface.chipset
    if not label:
        # legacy fallback: strip the "(Make Model)" suffix off a description/name
        label = str(iface.description or iface.name or "card").split("(")[0].strip()
    label = label or "card"
    mac = iface.driver.mac_address
    if isinstance(mac, (bytes, bytearray)) and len(mac) == 6:
        mac = ":".join(f"{b:02x}" for b in mac)
    return str(label), (str(mac) if mac else None)

