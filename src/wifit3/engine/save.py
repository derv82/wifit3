"""Auto-save: typed, content-deduped persistence of every recoverable artifact.

One function per saveable kind (handshake, PMKID, WEP key, WPS-PIN, WPS-PBC),
each returning a ``SaveResult`` (path + was_new flag) on success, or ``None``
when there's nothing saveable at all (hidden SSID, no valid pair, empty key).
A dedupe hit returns ``SaveResult(path=<existing>, was_new=False)`` — the
caller gets to log "saved" vs "already on disk as X" without re-deriving the
path. Log formatting lives in the caller: tree-leaf vs root-line is a UI
decision and the save layer can't know which context it's in.

Filename scheme: ``<safe_ssid>_<bssid-dashed>_<epoch>_<kind>.<ext>``. Kind ∈
``{handshake, pmkid, wep_key, wps_pin, wps_pbc}``; ext follows from kind.

Dedupe is existence-based — we scan files matching ``*_<bssid>_*_<kind>.<ext>``
and compare the kind-specific fingerprint (ANonce for handshake, the PMKID for
PMKID, key hex for WEP, (PIN, PSK) for wps_pin, PSK for wps_pbc). No
overwrites, no destructive cleanup. Rotation (same client, fresh ANonce / fresh
PSK) writes a new file.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from wifit3.engine.hc22000 import eapol_hashlines, pmkid_hashline
from wifit3.engine.models import AccessPoint
from wifit3.engine.pcap import write_pcap


@dataclass(frozen=True)
class SaveResult:
    """Outcome of a save_* call. ``was_new`` is True for a fresh write, False
    when the dedupe predicate matched an existing file at ``path``."""
    path: Path
    was_new: bool


_SSID_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
_SSID_MAX = 32


def _safe_ssid(ssid: Optional[str]) -> str:
    base = _SSID_SAFE_RE.sub("_", ssid or "")[:_SSID_MAX]
    return base or "hidden"


def _fresh_path(captures_dir: Path, ap: AccessPoint, suffix: str) -> Path:
    """Build ``captures_dir/<safe_ssid>_<bssid-dashed>_<epoch><suffix>`` with the
    smallest non-colliding epoch ≥ now. Two saves of different content in the
    same wall-clock second otherwise overwrite each other — dedupe only catches
    *identical* content; structurally distinct artifacts (different ANonces /
    rotated PSK) must land in different files."""
    base = f"{_safe_ssid(ap.ssid)}_{ap.bssid.replace(':', '-')}"
    epoch = int(time.time())
    while True:
        candidate = captures_dir / f"{base}_{epoch}{suffix}"
        if not candidate.exists():
            return candidate
        epoch += 1


def _existing(captures_dir: Path, bssid: str, suffix: str) -> list[Path]:
    """Files in ``captures_dir`` whose name carries this BSSID + ``_<suffix>``
    (kind + extension, e.g. ``_handshake.hc22000``)."""
    if not captures_dir.is_dir():
        return []
    bssid_dashed = bssid.replace(":", "-").lower()
    out: list[Path] = []
    for p in captures_dir.iterdir():
        if not p.is_file():
            continue
        name = p.name.lower()
        if bssid_dashed in name and name.endswith(suffix):
            out.append(p)
    return out


# ----- Handshake / PMKID ----------------------------------------------------

def _eapol_frames_for(ap: AccessPoint, client_mac: str) -> list[bytes]:
    """Beacon (once, if available) + every EAPOL frame for the given client."""
    hs = ap.handshakes.get(client_mac)
    if hs is None:
        return []
    frames: list[bytes] = []
    if hs.beacon_frame:
        frames.append(hs.beacon_frame)
    for f in hs.eapol_frames:
        frames.append(f.raw)
    return frames


def _read_hashline_field(path: Path, line_prefix: str, field_index: int) -> set[str]:
    """Asterisk-split each ``WPA*NN*…`` line in *path*; return the values at
    ``field_index``. Field 0 is ``WPA``. Used by dedupe predicates."""
    out: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        if not line.startswith(line_prefix):
            continue
        parts = line.split("*")
        if len(parts) > field_index:
            out.add(parts[field_index].lower())
    return out


def save_handshake(
    ap: AccessPoint, client_mac: str,
    *, captures_dir: Path = Path("captures"),
) -> Optional[SaveResult]:
    """Persist a WPA 4-way handshake for ``ap.handshakes[client_mac]``.

    Dedupes by (BSSID, ANonce). Writes ``_handshake.hc22000`` + companion
    ``_handshake.pcap``. Returns a SaveResult (was_new=True on fresh write,
    False with the existing file's path on dedupe), or None if the SSID is
    hidden / no crackable pair has been captured.
    """
    if not ap.ssid:
        return None
    hs = ap.handshakes.get(client_mac)
    if hs is None:
        return None
    lines = eapol_hashlines(ap.ssid, hs)
    if not lines:
        return None

    # ANonce is asterisk-field 6 (0-indexed: WPA*02*mic*ap*sta*essid*anonce*…).
    new_anonces = {ln.split("*")[6].lower() for ln in lines if len(ln.split("*")) > 6}
    if new_anonces:
        for p in _existing(captures_dir, ap.bssid, "_handshake.hc22000"):
            if new_anonces.issubset(_read_hashline_field(p, "WPA*02*", 6)):
                return SaveResult(path=p, was_new=False)

    captures_dir = Path(captures_dir)
    captures_dir.mkdir(parents=True, exist_ok=True)
    hc_path = _fresh_path(captures_dir, ap, "_handshake.hc22000")
    pcap_path = hc_path.with_name(hc_path.name[:-len(".hc22000")] + ".pcap")
    hc_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_pcap(pcap_path, _eapol_frames_for(ap, client_mac))
    return SaveResult(path=hc_path, was_new=True)


def save_pmkid(
    ap: AccessPoint, client_mac: str,
    *, captures_dir: Path = Path("captures"),
) -> Optional[SaveResult]:
    """Persist the PMKID on ``ap.handshakes[client_mac]``.

    Dedupes by (BSSID, PMKID-value). Writes ``_pmkid.hc22000`` + companion
    ``_pmkid.pcap`` (the M1 frame carries the PMKID KDE; we keep the same
    frame set the handshake save would, since one capture often produces
    both). Returns None on hidden SSID / missing PMKID.
    """
    if not ap.ssid:
        return None
    hs = ap.handshakes.get(client_mac)
    if hs is None:
        return None
    line = pmkid_hashline(ap.ssid, hs)
    if not line:
        return None
    pmkid_value = line.split("*")[2].lower()

    for p in _existing(captures_dir, ap.bssid, "_pmkid.hc22000"):
        if pmkid_value in _read_hashline_field(p, "WPA*01*", 2):
            return SaveResult(path=p, was_new=False)

    captures_dir = Path(captures_dir)
    captures_dir.mkdir(parents=True, exist_ok=True)
    hc_path = _fresh_path(captures_dir, ap, "_pmkid.hc22000")
    pcap_path = hc_path.with_name(hc_path.name[:-len(".hc22000")] + ".pcap")
    hc_path.write_text(line + "\n", encoding="utf-8")
    write_pcap(pcap_path, _eapol_frames_for(ap, client_mac))
    return SaveResult(path=hc_path, was_new=True)


# ----- WEP key --------------------------------------------------------------

_WEP_HEX_RE = re.compile(r"WEP key \(hex\):\s*([0-9a-fA-F]+)")


def save_wep_key(
    ap: AccessPoint, key: bytes,
    *, captures_dir: Path = Path("captures"),
) -> Optional[SaveResult]:
    """Persist a recovered WEP key. Dedupes by exact key value for this BSSID."""
    if not key:
        return None
    key_hex = key.hex()
    for p in _existing(captures_dir, ap.bssid, "_wep_key.txt"):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _WEP_HEX_RE.search(text)
        if m and m.group(1).lower() == key_hex:
            return SaveResult(path=p, was_new=False)

    captures_dir = Path(captures_dir)
    captures_dir.mkdir(parents=True, exist_ok=True)
    path = _fresh_path(captures_dir, ap, "_wep_key.txt")
    lines = [
        f"SSID:  {ap.ssid or '<hidden>'}",
        f"BSSID: {ap.bssid}",
        f"WEP key (hex):   {key_hex}",
    ]
    if all(0x20 <= b < 0x7F for b in key):
        lines.append(f'WEP key (ASCII): "{key.decode("ascii")}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return SaveResult(path=path, was_new=True)


# ----- WPS PIN / PBC --------------------------------------------------------

_PSK_RE = re.compile(r"^PSK:\s*(.+)$", re.MULTILINE)
_PIN_RE = re.compile(r"^PIN:\s*(.+)$", re.MULTILINE)


def save_wps_pin(
    ap: AccessPoint, pin: str, psk: str,
    *, captures_dir: Path = Path("captures"),
) -> Optional[SaveResult]:
    """Persist a WPS-PIN credential. Dedupes by (PIN, PSK) for this BSSID."""
    if not pin or not psk:
        return None
    for p in _existing(captures_dir, ap.bssid, "_wps_pin.txt"):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        psk_match = _PSK_RE.search(text)
        pin_match = _PIN_RE.search(text)
        if (psk_match and psk_match.group(1).strip() == psk
                and pin_match and pin_match.group(1).strip() == pin):
            return SaveResult(path=p, was_new=False)

    captures_dir = Path(captures_dir)
    captures_dir.mkdir(parents=True, exist_ok=True)
    path = _fresh_path(captures_dir, ap, "_wps_pin.txt")
    body = (
        f"SSID: {ap.ssid or ''}\n"
        f"BSSID: {ap.bssid}\n"
        f"PSK: {psk}\n"
        f"PIN: {pin}\n"
    )
    path.write_text(body, encoding="utf-8")
    return SaveResult(path=path, was_new=True)


def save_wps_pbc(
    ap: AccessPoint, psk: str,
    *, captures_dir: Path = Path("captures"),
) -> Optional[SaveResult]:
    """Persist a WPS-PBC credential. Dedupes by PSK for this BSSID."""
    if not psk:
        return None
    for p in _existing(captures_dir, ap.bssid, "_wps_pbc.txt"):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _PSK_RE.search(text)
        if m and m.group(1).strip() == psk:
            return SaveResult(path=p, was_new=False)

    captures_dir = Path(captures_dir)
    captures_dir.mkdir(parents=True, exist_ok=True)
    path = _fresh_path(captures_dir, ap, "_wps_pbc.txt")
    body = (
        f"SSID: {ap.ssid or ''}\n"
        f"BSSID: {ap.bssid}\n"
        f"PSK: {psk}\n"
    )
    path.write_text(body, encoding="utf-8")
    return SaveResult(path=path, was_new=True)
