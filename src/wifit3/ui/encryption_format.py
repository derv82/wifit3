"""Render AccessPoint security info as Rich-markup for the UI."""
from __future__ import annotations

from typing import List, Optional

from wifit3.models import AccessPoint


# Actionability palette: color signals what's worth targeting:
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
    ``detailed=False`` (scanner) drops the pairwise cipher entirely.
    ``muted`` overrides the default Rich ``"dim"`` attribute."""
    akms_tok = _simplified_akms(ap.akms)
    cipher = ap.pairwise_cipher
    show_cipher = detailed and cipher is not None

    # WPA3 Transition (SAE + PSK) — render as WPA3→2.
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
        # Attackable now (IV capture → replay → crack).
        head = f"[{_ATTACKABLE}]WEP[/{_ATTACKABLE}]"
        if detailed:
            return head
        n = ap.wep.unique_ivs if ap.wep else 0
        return head + f"[{muted}]·{_format_iv_count(n)} IVs[/{muted}]"
    if enc.startswith("WPA-") or enc == "WPA":
        # Legacy WPA1 vendor IE — TKIP universal. Out of scope for wifit3.
        head = f"[{_OUT_OF_SCOPE}]WPA[/{_OUT_OF_SCOPE}]"
        tail = f" [{muted}](PSK·TKIP)[/{muted}]" if detailed else f" [{muted}](PSK)[/{muted}]"
        return head + tail

    # Unknown — show raw string muted.
    return f"[{muted}]{enc}[/{muted}]"


def wep_key_ascii(key_hex: str) -> str:
    """A recovered WEP key as ``<hex> = "<ascii>"``."""
    try:
        kb = bytes.fromhex(key_hex)
    except ValueError:
        return key_hex
    if kb and all(0x20 <= b < 0x7F for b in kb):
        return f'{key_hex} = "{kb.decode("ascii")}"'
    return key_hex
