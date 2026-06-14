"""Hashcat ``-m 22000`` hashline writer (WPA-PBKDF2-PMKID+EAPOL).

The PMKID (``WPA*01``) line is built here; the EAPOL (``WPA*02``) lines and the
crackability decision both live in ``engine.wpa.handshake`` — the single source
of truth shared with the capture-event / auto-save path, so a "captured" verdict
and a writable hashline can never disagree.

Format spec (one line per hash):

    PROTOCOL*TYPE*PMKID_OR_MIC*MACAP*MACSTA*ESSID*ANONCE*EAPOL*MESSAGEPAIR
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from wifit3.engine.models import AccessPoint, Handshake
from wifit3.engine.wpa import handshake as wpa
from wifit3.engine.wpa.handshake import mac_compact, ssid_hex


def pmkid_hashline(ssid: str, hs: Handshake) -> Optional[str]:
    """Return a ``WPA*01*…`` line for the PMKID, or None if not available or not
    crackable. A PMKID carries no AKM of its own, so a SAE-derived one looks
    identical on the wire — ``is_crackable_akm`` gates it off the association's
    AKM (the AP's offered set + relevant M2 client suite); see engine.wpa.handshake."""
    if not ssid or not hs.pmkid or len(hs.pmkid) != 16:
        return None
    if not wpa.is_crackable_akm(hs):
        return None
    return (
        "WPA*01"
        f"*{hs.pmkid.hex()}"
        f"*{mac_compact(hs.bssid)}"
        f"*{mac_compact(hs.client_mac)}"
        f"*{ssid_hex(ssid)}"
        "***"
    )


def eapol_hashlines(ssid: str, hs: Handshake) -> List[str]:
    """One ``WPA*02*…`` line per distinct *crackable* handshake instance (see
    ``wpa.crackable_pairs``). Empty when the SSID is hidden or no instance is
    serialisable (e.g. a clipped MIC frame) — i.e. exactly when there's nothing
    hashcat could crack."""
    if not ssid:
        return []
    return [wpa.hc22000_line(ssid, hs, pair) for pair in wpa.crackable_pairs(hs)]


def eapol_hashline(ssid: str, hs: Handshake) -> Optional[str]:
    """The single best ``WPA*02*…`` line for this handshake, or None."""
    lines = eapol_hashlines(ssid, hs)
    return lines[0] if lines else None


def format_ap_hashlines(ap: AccessPoint) -> List[str]:
    """Every hashline we can produce for this AP across all clients."""
    if not ap.ssid:
        return []  # hidden network → can't fill the ESSID field
    lines: List[str] = []
    for hs in ap.handshakes.values():
        pmkid = pmkid_hashline(ap.ssid, hs)
        if pmkid:
            lines.append(pmkid)
        lines.extend(eapol_hashlines(ap.ssid, hs))
    return lines


def write_hc22000(path: Path, ap: AccessPoint) -> int:
    """Write all hashlines for *ap* to *path*. Returns the count written; writes
    nothing (and returns 0) if none could be produced."""
    lines = format_ap_hashlines(ap)
    if not lines:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)
