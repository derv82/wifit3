"""Rich-markup rendering for capture events — the presentation layer ``capture_events.py``
omits so the detector stays structural and testable without a terminal.

Currently the per-frame EAPOL trace line: an ``Mx`` label plus a ✓/✗ per hashcat-relevant
field. Background chips (``[… on green]`` etc.) are reserved for actual captures (HANDSHAKE /
PMKID banners, credential wins), so a green chip in the log always means a win. The lone
exception is the orange ``SAE``/``FT`` chip on an uncrackable frame (anti-win marker).
"""
from __future__ import annotations

from typing import List

from wifit3.ui.capture_events import CaptureEvent


def _tick(ok: bool) -> str:
    return "[green]✓[/green]" if ok else "[red]✗[/red]"


def short_sta(mac: str) -> str:
    """A STA MAC trimmed to its last 3 octets (``…44:55:66``) — enough to tell clients apart
    in the log without spending 17 chars per line. Shared with the Focus banners."""
    return ("…" + mac[-8:]) if mac else "…"


def _sta_dir(ev: CaptureEvent) -> str:
    """The client (last 3 octets) plus a direction arrow: M1/M3 travel AP→STA, M2/M4 STA→AP.
    Shows which client a frame belongs to and its direction without memorising the map."""
    sta = short_sta(ev.client_mac)
    if ev.msg_num in (2, 4):
        return f"{sta}→AP"
    if ev.msg_num in (1, 3):
        return f"AP→{sta}"
    return sta                 # unclassified — direction unknown


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


def eapol_message_markup(ev: CaptureEvent) -> str:
    """One per-frame EAPOL trace line as Rich markup. Label bold-cyan when the frame helps
    form a crackable pair (``ev.useful``), else dim-cyan; the dim prefix names client +
    direction so a lone line is self-explanatory.

    When the association's AKM is confirmed uncrackable,  the trace mustn't read like progress.
    ``crackable`` None (transition AP, pre-M2) renders normally."""
    uncrackable = ev.crackable is False
    style = "red" if uncrackable else ("bold cyan" if ev.useful else "dim cyan")
    label = f"[{style}]M{ev.msg_num}[/{style}]" if ev.msg_num else f"[{style}]EAPOL-?[/{style}]"
    fields = _eapol_fields(ev)
    body = f"{label} " + " ".join(fields) if fields else label
    line = f"[dim]4-Way Handshake ({_sta_dir(ev)}):[/dim] "
    if uncrackable:
        chip = ev.akm_label or "SAE"
        line += f"[bold black on orange1] {chip} [/bold black on orange1] "
    line += body
    return line
