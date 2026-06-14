"""Render AccessPoint security info as Rich-markup for the UI.

ScannerView and FocusView both call into here so the color scheme + AKM
formatting stay in lock-step. Pure functions — no Textual, no I/O.
"""
from __future__ import annotations

from typing import List, Optional

from wifit3.engine.models import AccessPoint


# Actionability palette: color signals what's worth targeting, not "how
# strong" the protocol is. The scanner is a target-picker, so colors
# should answer "is wifit3 going to crack this?"
#   bright_green = attackable today (we have an attack)
#   yellow       = interesting but no attack yet (PRs welcome)
#   red          = out of scope (we don't support cracking this protocol)
#   dim (muted)  = no attack needed (OPEN networks)
_ATTACKABLE = "bright_green"
_NO_ATTACK_YET = "yellow"
_OUT_OF_SCOPE = "red"


def _format_iv_count(n: int) -> str:
    """Compact IV count for the ENCRYPT cell: 1234 → '1.2k', 12345 → '12.3k'."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M")


def _simplified_akms(akms: List[str]) -> str:
    """Compact AKM token string for the Scanner view's ENCRYPT column."""
    parts: List[str] = []
    for name, tok in (
        ("PSK", "PSK"), ("PSK-SHA256", "PSK256"), ("PSK-SHA384", "PSK384"),
        ("FT-PSK", "FT-PSK"), ("FT-PSK-SHA384", "FT-PSK384"),
    ):
        if name in akms:
            parts.append(tok)
    if any("SAE" in a for a in akms):          # SAE / FT-SAE / SAE-EXT-KEY / …
        parts.append("SAE")
    if any(a.startswith("EAP") or a.startswith("FT-EAP") for a in akms):
        parts.append("EAP")
    if "OWE" in akms and not parts:
        parts.append("OWE")
    if not parts:
        # Fall back to whatever the parser gave us (unknown / vendor AKMs).
        return "/".join(akms) if akms else ""
    return "/".join(parts)


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

    ``detailed=False`` (scanner) drops the pairwise cipher entirely — from
    an attacker's perspective CCMP / GCMP-128 / GCMP-256 all funnel into
    the same hashcat ``-m 22000`` hashline, so the cipher choice doesn't
    open or close any wifit3 attack. Keep the scanner cell tight.
    ``detailed=True`` (focus view) keeps the cipher visible for reference.

    ``muted`` overrides the default Rich ``"dim"`` attribute for the
    parenthesised detail / fallback strings. Pass a concrete hex color
    (e.g. resolved from ``app.theme_variables["foreground-darken-3"]``)
    when the caller needs the muted text to participate in a color
    blend — Rich's ``dim`` has no color triplet and is skipped by blenders.
    """
    akms_tok = _simplified_akms(ap.akms)
    cipher = ap.pairwise_cipher
    show_cipher = detailed and cipher is not None

    # WPA3 Transition (SAE + PSK) — render as WPA3→2, both sides _ATTACKABLE since the WPA2
    # lane is reachable via PMKID. "WPA3→2" already encodes PSK+SAE, so scanner mode suppresses
    # the AKM detail; detailed mode still surfaces it (rare PSK+SAE+EAP enterprise transition).
    if ap.wpa3 and ap.transition_mode:
        head = f"[{_ATTACKABLE}]WPA3[/{_ATTACKABLE}]→[{_ATTACKABLE}]2[/{_ATTACKABLE}]"
        if not detailed:
            return head
        return head + _detail_markup(akms_tok, cipher, show_cipher, muted)

    # Pure WPA3-SAE — no usable attack yet.
    if ap.wpa3:
        head = f"[{_NO_ATTACK_YET}]WPA3[/{_NO_ATTACK_YET}]"
        return head + _detail_markup(akms_tok, cipher, show_cipher, muted)

    # OWE (Enhanced Open) — no attack yet.
    if "OWE" in ap.akms:
        return f"[{_NO_ATTACK_YET}]OWE[/{_NO_ATTACK_YET}]"

    # Any RSN-based modern WPA2 — attackable.
    if ap.akms:
        head = f"[{_ATTACKABLE}]WPA2[/{_ATTACKABLE}]"
        return head + _detail_markup(akms_tok, cipher, show_cipher, muted)

    # Fallback to the legacy encryption string for OPEN/WEP/WPA1.
    enc = (ap.encryption or "").upper()
    if enc == "OPEN" or not enc or enc == "UNKNOWN":
        return f"[{muted}]OPEN[/{muted}]"
    if enc == "WEP":
        # Attackable now (IV capture → replay → crack). Scanner cell carries a
        # live unique-IV count so a target with IVs already banked stands out;
        # the Focus CAPTURE panel owns the count in detailed mode.
        head = f"[{_ATTACKABLE}]WEP[/{_ATTACKABLE}]"
        if detailed:
            return head
        n = ap.wep.unique_ivs if ap.wep else 0
        # Dot separator (no parens) — the ENCRYPT column clips a trailing ')'.
        return head + f"[{muted}]·{_format_iv_count(n)} IVs[/{muted}]"
    if enc.startswith("WPA-") or enc == "WPA":
        # Legacy WPA1 vendor IE — TKIP universal. Out of scope for wifit3.
        head = f"[{_OUT_OF_SCOPE}]WPA[/{_OUT_OF_SCOPE}]"
        tail = f" [{muted}](PSK·TKIP)[/{muted}]" if detailed else f" [{muted}](PSK)[/{muted}]"
        return head + tail

    # Unknown — show raw string muted.
    return f"[{muted}]{enc}[/{muted}]"


def format_pmf_markup(ap: AccessPoint) -> str:
    """Color-coded PMF status for the SECURITY panel, read from the attacker's
    POV: green = no protection (deauth-based attacks work), orange = mixed,
    red = locked down (deauth-based attacks fail)."""
    if ap.pmf_required:
        return "[red]Required[/red]"
    if ap.pmf_capable:
        return "[dark_orange]Optional[/dark_orange]"
    return "[green]Disabled[/green]"


# WPS Config Methods bitmask bits (WSC spec, attr 0x1008).
_WPS_CM_PIN = 0x0004 | 0x0008 | 0x0100   # Label | Display | Keypad
_WPS_CM_PBC = 0x0080                      # PushButton
_WPS_CM_NFC = 0x0010 | 0x0020 | 0x0040    # NFC token / interface
_WPS_PWID_PBC = 0x0004                     # Device Password ID = PushButton


def _wps_methods(config_methods: int) -> List[tuple]:
    """Decode the Config Methods bitmask into (label, is_attack_surface)
    pairs in display order. PIN (Label/Display/Keypad) is the only WPS
    attack surface — Reaver brute / Pixie-Dust. PushButton + NFC are FYI."""
    out: List[tuple] = []
    if config_methods & _WPS_CM_PIN:
        out.append(("PIN", True))
    if config_methods & _WPS_CM_PBC:
        out.append(("PushButton", False))
    if config_methods & _WPS_CM_NFC:
        out.append(("NFC", False))
    return out


def format_wps_markup(ap: AccessPoint) -> Optional[str]:
    """Actionability-colored WPS sub-line for the SECURITY panel.

    Returns ``None`` when the AP doesn't advertise WPS (caller hides the
    line). green = actionable (PIN method / Unlocked / live PBC window),
    dim = FYI capability (PushButton, NFC), red = Locked (PIN attempts
    rate-limited → dead end). When locked, the whole line is greyed since
    nothing is usable right now.
    """
    if not ap.wps:
        return None

    ver_label = f"WPS{ap.wps_version}" if ap.wps_version else "WPS"
    methods = _wps_methods(ap.wps_config_methods)

    if ap.wps_locked:
        detail = ver_label
        if methods:
            detail += " · " + "·".join(name for name, _ in methods)
        return f"🔒 [red]Locked[/red] · [dim]{detail}[/dim]"

    # Version is neutral, not an attackability axis: Pixie-Dust is chipset-PRNG-dependent
    # (hits 1.0 and 2.0 alike), and 2.0's mandatory lockout is already carried by the Locked
    # flag. Left uncolored so it inherits the panel fg (theme-safe).
    head = f"[{_ATTACKABLE}]Unlocked[/{_ATTACKABLE}] · {ver_label}"
    if methods:
        rendered = "·".join(
            f"[{_ATTACKABLE}]{name}[/{_ATTACKABLE}]" if attack else f"[dim]{name}[/dim]"
            for name, attack in methods
        )
        head += f" ({rendered})"
    if ap.wps_device_password_id == _WPS_PWID_PBC:
        head += f" · [{_ATTACKABLE}]PBC active[/{_ATTACKABLE}]"
    return head


def format_wpa3_mode_markup(ap: AccessPoint) -> Optional[str]:
    """Actionability-colored WPA3 sub-line for the SECURITY panel.

    Returns ``None`` when the AP isn't WPA3 — caller should hide the line.
    """
    if not ap.wpa3:
        return None
    if ap.transition_mode:
        return (
            f"[{_ATTACKABLE}]WPA3[/{_ATTACKABLE}]→"
            f"[{_ATTACKABLE}]2[/{_ATTACKABLE}] [dim](Transition)[/dim]"
        )
    return f"[{_NO_ATTACK_YET}]Pure WPA3-SAE[/{_NO_ATTACK_YET}]"


def wep_key_ascii(key_hex: str) -> str:
    """A recovered WEP key as ``<hex> = "<ascii>"`` when it's printable ASCII
    (e.g. ``abcde``), bare hex otherwise (e.g. a 104-bit binary key). The
    display form shared by Focus's key chip and the Scanner win-line."""
    try:
        kb = bytes.fromhex(key_hex)
    except ValueError:
        return key_hex
    if kb and all(0x20 <= b < 0x7F for b in kb):
        return f'{key_hex} = "{kb.decode("ascii")}"'
    return key_hex
