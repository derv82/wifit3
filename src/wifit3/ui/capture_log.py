"""Rich-markup helpers for EAPOL capture events: the per-field ``✓``/``✗`` fragments the
EAPOL aggregator paints, plus the short-STA formatter shared with the Focus banners."""
from __future__ import annotations

from typing import List

from wifit3.ui.capture_events import CaptureEvent


def _tick(ok: bool) -> str:
    return "[green]✓[/green]" if ok else "[red]✗[/red]"


def short_sta(mac: str) -> str:
    """A STA MAC trimmed to its last 3 octets (``…44:55:66``) — enough to tell clients apart
    in the log without spending 17 chars per line. Shared with the Focus banners."""
    return ("…" + mac[-8:]) if mac else "…"


def _eapol_fields(ev: CaptureEvent) -> List[str]:
    """The per-field ``label✓/✗`` fragments for one EAPOL frame. Which fields show is
    message-specific — hashcat wants an ANonce from M1/M3 and SNonce + MIC + complete EAPOL
    from the M2/M4 keystone — so we tick exactly those."""
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
    return []                  # unclassified (group rekey etc.) — label only
