"""Render AccessPoint security info as Rich-markup for the UI.

ScannerView and FocusView both call into here so the color scheme + AKM
formatting stay in lock-step. Pure functions — no Textual, no I/O.
"""
from __future__ import annotations

from typing import List, Optional

from wifit3.engine.models import AccessPoint


_COLOR_WPA3 = "red"
_COLOR_WPA2 = "green"
_COLOR_WPA_LEGACY = "red"
_COLOR_WEP = "red"
_COLOR_OWE = "yellow"


def _simplified_akms(akms: List[str]) -> str:
    """Reduce the raw AKM list to a short PSK/SAE/EAP/OWE token string."""
    has_sae = any(a == "SAE" or a == "FT-SAE" for a in akms)
    has_psk = any(a in ("PSK", "PSK-SHA256", "FT-PSK") for a in akms)
    has_eap = any(a.startswith("EAP") or a == "FT-EAP" for a in akms)
    has_owe = "OWE" in akms

    parts: List[str] = []
    if has_psk:
        parts.append("PSK")
    if has_sae:
        parts.append("SAE")
    if has_eap:
        parts.append("EAP")
    if has_owe and not parts:
        parts.append("OWE")
    if not parts:
        # Fall back to whatever the parser gave us (unknown / vendor AKMs)
        return "+".join(akms) if akms else ""
    return "+".join(parts)


def _detail_markup(
    akms_tok: str,
    cipher: Optional[str],
    show_cipher: bool,
    muted: str = "dim",
) -> str:
    """Build the muted-parens detail suffix, e.g. ` [dim](PSK)[/dim]`."""
    inner_parts: List[str] = []
    if akms_tok:
        inner_parts.append(akms_tok)
    if show_cipher and cipher:
        inner_parts.append(cipher)
    if not inner_parts:
        return ""
    return f" [{muted}]({'·'.join(inner_parts)})[/{muted}]"


def format_encryption_markup(
    ap: AccessPoint, detailed: bool = False, muted: str = "dim"
) -> str:
    """Return Rich-markup for the ENCRYPT cell.

    ``detailed=False`` (scanner) drops CCMP from the cipher detail since
    it's the universal modern pairwise cipher and just adds noise.
    ``detailed=True`` (focus view) keeps the cipher visible.

    ``muted`` overrides the default Rich ``"dim"`` attribute for the
    parenthesised detail / fallback strings. Pass a concrete hex color
    (e.g. resolved from ``app.theme_variables["foreground-darken-3"]``)
    when the caller needs the muted text to participate in a color
    blend — Rich's ``dim`` has no color triplet and is skipped by blenders.
    """
    akms_tok = _simplified_akms(ap.akms)
    cipher = ap.pairwise_cipher
    # In scanner mode, suppress CCMP. In detailed mode, show it as-is.
    show_cipher = detailed or (cipher is not None and cipher != "CCMP")

    # WPA3 Transition (has both SAE and PSK) — render as WPA3→WPA2.
    if ap.wpa3 and ap.transition_mode:
        head = f"[{_COLOR_WPA3}]WPA3[/{_COLOR_WPA3}]→[{_COLOR_WPA2}]WPA2[/{_COLOR_WPA2}]"
        return head + _detail_markup(akms_tok, cipher, show_cipher, muted)

    # Pure WPA3-SAE.
    if ap.wpa3:
        head = f"[{_COLOR_WPA3}]WPA3[/{_COLOR_WPA3}]"
        return head + _detail_markup(akms_tok, cipher, show_cipher, muted)

    # OWE (Enhanced Open).
    if "OWE" in ap.akms:
        return f"[{_COLOR_OWE}]OWE[/{_COLOR_OWE}]"

    # Any RSN-based modern WPA2 — we have AKMs in the list.
    if ap.akms:
        head = f"[{_COLOR_WPA2}]WPA2[/{_COLOR_WPA2}]"
        return head + _detail_markup(akms_tok, cipher, show_cipher, muted)

    # Fallback to the legacy encryption string for OPEN/WEP/WPA1.
    enc = (ap.encryption or "").upper()
    if enc == "OPEN" or not enc or enc == "UNKNOWN":
        return f"[{muted}]OPEN[/{muted}]"
    if enc == "WEP":
        return f"[{_COLOR_WEP}]WEP[/{_COLOR_WEP}]"
    if enc.startswith("WPA-") or enc == "WPA":
        # Legacy WPA1 vendor IE — TKIP universal.
        head = f"[{_COLOR_WPA_LEGACY}]WPA[/{_COLOR_WPA_LEGACY}]"
        tail = f" [{muted}](PSK·TKIP)[/{muted}]" if detailed else f" [{muted}](PSK)[/{muted}]"
        return head + tail

    # Unknown — show raw string muted.
    return f"[{muted}]{enc}[/{muted}]"


def format_pmf_markup(ap: AccessPoint) -> str:
    """Color-coded PMF status for the SECURITY panel.

    Required = red, Optional = yellow, Disabled = dim.
    """
    if ap.pmf_required:
        return "[red]Required[/red]"
    if ap.pmf_capable:
        return "[yellow]Optional[/yellow]"
    return "[dim]Disabled[/dim]"


def format_wpa3_mode_markup(ap: AccessPoint) -> Optional[str]:
    """Color-coded WPA3 sub-line for the SECURITY panel.

    Returns ``None`` when the AP isn't WPA3 — caller should hide the line.
    """
    if not ap.wpa3:
        return None
    if ap.transition_mode:
        return f"[{_COLOR_WPA3}]WPA3[/{_COLOR_WPA3}]→[{_COLOR_WPA2}]WPA2[/{_COLOR_WPA2}] [dim](Transition)[/dim]"
    return f"[{_COLOR_WPA2}]Pure WPA3-SAE[/{_COLOR_WPA2}]"
