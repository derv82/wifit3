"""Display helpers for AP events: labels and the Rich-markup fragments the EAPOL aggregator
and the Scanner/Focus logs paint."""
from __future__ import annotations

from typing import List, Optional

from wifit3.ui.colors import PASSWORD_MISMATCH_COLOR
from wifit3.watchers.ap_watcher import EapolFrameSeen


# Human-readable labels for AccessPoint.decloak_method / ApEvent.method.
DECLOAK_METHOD_LABELS = {
    "beacon": "Beacon Leak",
    "probe_resp": "Probe Response",
    "assoc_req": "Association Request",
    "reassoc_req": "Reassociation Request",
    "assoc": "Association Response",
}


def _tick(ok: bool) -> str:
    return "[green]✓[/green]" if ok else "[red]✗[/red]"


def short_sta(mac: str) -> str:
    """A STA MAC trimmed to its last 3 octets (``…44:55:66``): enough to tell clients apart
    in the log without spending 17 chars per line. Shared with the Focus banners."""
    return ("…" + mac[-8:]) if mac else "…"


def _eapol_fields(ev: EapolFrameSeen) -> List[str]:
    """The per-field ``label✓/✗`` fragments for one EAPOL frame. Which fields show is
    message-specific: hashcat wants an ANonce from M1/M3 and SNonce + MIC + complete EAPOL
    from the M2/M4 keystone, so we tick exactly those."""
    has_nonce = bool(ev.has_nonce)
    has_mic = bool(ev.has_mic)
    complete = bool(ev.eapol_complete)
    eapol_field = f"EAPOL{_tick(complete)}"
    if ev.msg_num == 1:        # ANonce donor, no MIC by design
        # PMKID rides M1's key data; ev.has_pmkid is set (True/False) only for
        # M1, so a ✓ here pre-explains the "PMKID captured" banner that follows.
        fields = [f"ANonce{_tick(has_nonce)}"]
        if ev.has_pmkid is not None:
            fields.append(f"PMKID{_tick(bool(ev.has_pmkid))}")
        return fields
    if ev.msg_num == 2:        # keystone: SNonce + MIC + complete EAPOL
        return [f"SNonce{_tick(has_nonce)}", f"MIC{_tick(has_mic)}", eapol_field]
    if ev.msg_num == 3:        # ANonce donor (its own MIC/EAPOL unused by hashcat)
        return [f"ANonce{_tick(has_nonce)}", f"MIC{_tick(has_mic)}"]
    if ev.msg_num == 4:        # conditional keystone: needs an echoed (non-zero) SNonce
        return [f"SNonce{_tick(has_nonce)}", f"MIC{_tick(has_mic)}", eapol_field]
    return []                  # unclassified (group rekey etc.): label only


def saved_psk_markup(matches_saved_psk: Optional[bool]) -> str:
    """`` saved PSK matches`` (green) or `` saved PSK doesn't match`` (orange) for a new
    capture; empty when there was no saved PSK to check."""
    if matches_saved_psk is None:
        return ""
    if matches_saved_psk:
        return " [green]✓ saved PSK matches[/green]"
    return f" [{PASSWORD_MISMATCH_COLOR}]✗ saved PSK doesn't match[/]"


def saved_psk_chip(matches_saved_psk: Optional[bool]) -> Optional[str]:
    if matches_saved_psk is None:
        return None
    if matches_saved_psk:
        return "[black bold on cyan] Saved PSK Still Works [/black bold on cyan] [dim](see vault)[/dim]"
    return (f"[black bold on {PASSWORD_MISMATCH_COLOR}] No Saved PSKs Work [/] "
            f"[dim](password changed)[/dim]")
