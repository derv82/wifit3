"""Rich-markup rendering for capture events — the presentation layer that
``capture_events.py`` deliberately omits (it stays structural so the detector is
testable without a terminal).

Currently just the per-frame EAPOL trace line: an ``Mx`` label followed by a
✓/✗ tick per hashcat-relevant field, so a fresh reader can see at a glance what
each handshake message contributed. The label is bold-cyan when the frame helps
form a crackable pair and dim-cyan when it arrived degraded (e.g. a clipped M2).

No background chip is ever emitted here: the ``[black bold on green]`` /
``[… on cyan]`` chips are reserved for *actual* captures (the HANDSHAKE / PMKID
banners + recovered-credential wins), so a chip in the log always means a win.
"""
from __future__ import annotations

from typing import List

from wifit3.ui.capture_events import CaptureEvent


def _tick(ok: bool) -> str:
    return "[green]✓[/green]" if ok else "[red]✗[/red]"


def _eapol_fields(ev: CaptureEvent) -> List[str]:
    """The per-field ``label ✓/✗`` fragments for one EAPOL frame, ordered as a
    reader expects. Which fields show is message-specific — hashcat only cares
    about an ANonce from M1/M3, and an SNonce + MIC + complete EAPOL from the
    M2 (or M4) keystone — so we tick exactly those and omit the rest."""
    has_nonce = bool(ev.has_nonce)
    has_mic = bool(ev.has_mic)
    complete = bool(ev.eapol_complete)
    eapol_field = f"{'EAPOL complete' if complete else 'EAPOL clipped'} {_tick(complete)}"
    if ev.msg_num == 1:        # ANonce donor, no MIC by design
        return [f"ANonce {_tick(has_nonce)}"]
    if ev.msg_num == 2:        # keystone: SNonce + MIC + complete EAPOL
        return [f"SNonce {_tick(has_nonce)}", f"MIC {_tick(has_mic)}", eapol_field]
    if ev.msg_num == 3:        # ANonce donor (its own MIC/EAPOL unused by hashcat)
        return [f"ANonce {_tick(has_nonce)}", f"MIC {_tick(has_mic)}"]
    if ev.msg_num == 4:        # conditional keystone: needs an echoed (non-zero) SNonce
        return [f"SNonce {_tick(has_nonce)}", f"MIC {_tick(has_mic)}", eapol_field]
    return []                  # unclassified (group rekey etc.) — label only


def eapol_message_markup(ev: CaptureEvent) -> str:
    """One per-frame EAPOL trace line as Rich markup. Label dim-cyan unless the
    frame contributes to a crackable pair (``ev.useful``), then bold-cyan."""
    style = "bold cyan" if ev.useful else "dim cyan"
    label = f"[{style}]M{ev.msg_num}[/{style}]" if ev.msg_num else f"[{style}]EAPOL-?[/{style}]"
    fields = _eapol_fields(ev)
    return f"{label} " + " · ".join(fields) if fields else label
