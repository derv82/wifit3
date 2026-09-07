"""Content-deduped auto-save for recovered artifacts: handshakes, PMKIDs,
WEP keys, WPS credentials. One save_* per kind, each returning a SaveResult
(or None when there's nothing worth saving). Dedupe never overwrites."""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from wifit3.crack.hc22000_format import eapol_hashlines, pmkid_hashline
from wifit3.models import AccessPoint
from wifit3.persist.common import (
    LEGACY_CAPTURE_RE,
    WEP_KEY_HEX_RE,
    WPS_PIN_RE,
    WPS_PSK_RE,
    bssid_to_colon,
    bssid_to_dashed,
    parse_hc22000,
    safe_ssid,
)
from wifit3.persist.config import Config
from wifit3.persist.pcap import write_pcap


@dataclass(frozen=True)
class SaveResult:
    """Outcome of a save_*; was_new is False when a dedupe hit returned an existing path."""
    path: Path
    was_new: bool


def _fresh_path(captures_dir: Path, ssid: str | None, bssid: str, suffix: str) -> Path:
    """Build captures_dir/<safe_ssid>_<bssid-dashed>_<epoch><suffix>, bumping to
    the smallest free epoch. Distinct content saved in the same second would
    otherwise collide. Dedupe only catches identical content, so structurally
    different artifacts (new ANonce / rotated PSK) need their own files."""
    base = f"{safe_ssid(ssid)}_{bssid_to_dashed(bssid)}"
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
    dashed = bssid_to_dashed(bssid)
    out: list[Path] = []
    for p in captures_dir.iterdir():
        if not p.is_file():
            continue
        name = p.name.lower()
        if dashed in name and name.endswith(suffix):
            out.append(p)
    return out


# ----- Handshake / PMKID ----------------------------------------------------

def _pcap_records_for(ap: AccessPoint, client_mac: str) -> list[tuple[bytes, float]]:
    """Beacon (once, if available) + every EAPOL frame for the client, each
    paired with its capture timestamp for the pcap. The beacon has no
    per-frame time, so it's placed first and stamped with the earliest EAPOL
    timestamp (or the AP's last-seen beacon time when there's none)."""
    hs = ap.handshakes.get(client_mac)
    if hs is None:
        return []
    eapol = [(f.raw, f.timestamp) for f in hs.messages]
    records: list[tuple[bytes, float]] = []
    if hs.beacon_frame:
        beacon_ts = min((ts for _, ts in eapol if ts > 0), default=ap.last_seen)
        records.append((hs.beacon_frame, beacon_ts))
    records.extend(eapol)
    return records


def _read_anonces(path: Path) -> set[str]:
    out: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        entry = parse_hc22000(line)
        if entry and entry.kind == "02" and entry.anonce:
            out.add(entry.anonce)
    return out


def _read_pmkids(path: Path) -> set[str]:
    out: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        entry = parse_hc22000(line)
        if entry and entry.kind == "01" and entry.pmkid_or_mic:
            out.add(entry.pmkid_or_mic)
    return out


class HcFiles:
    """Manages .hc22000 capture files on disk for a single AP."""

    def __init__(self, captures_dir: Path, ssid: str | None, bssid: str) -> None:
        self.captures_dir = captures_dir
        self.ssid = ssid or ""
        self.bssid = bssid
        self.agg_path = captures_dir / f"{safe_ssid(self.ssid)}_{bssid_to_dashed(bssid)}.hc22000"

    def find_existing_anonce(self, anonce: str) -> Path | None:
        """Find if ANonce already exists in either the aggregate file or any split file."""
        if self.agg_path.exists() and anonce in _read_anonces(self.agg_path):
            return self.agg_path
        for p in _existing(self.captures_dir, self.bssid, "_handshake.hc22000"):
            if anonce in _read_anonces(p):
                return p
        return None

    def find_existing_pmkid(self, pmkid: str) -> Path | None:
        """Find if PMKID already exists in either the aggregate file or any split file."""
        if self.agg_path.exists() and pmkid in _read_pmkids(self.agg_path):
            return self.agg_path
        for p in _existing(self.captures_dir, self.bssid, "_pmkid.hc22000"):
            if pmkid in _read_pmkids(p):
                return p
        return None

    def write_handshake(self, lines: list[str]) -> Path:
        """Persist handshake lines to the AP's .hc22000 file."""
        self.captures_dir.mkdir(parents=True, exist_ok=True)
        if self.agg_path.exists():
            existing_anonces = _read_anonces(self.agg_path)
            new_lines = []
            for ln in lines:
                entry = parse_hc22000(ln)
                if entry and entry.anonce and entry.anonce not in existing_anonces:
                    new_lines.append(ln)
                    existing_anonces.add(entry.anonce)
            if new_lines:
                with self.agg_path.open("a", encoding="utf-8") as f:
                    f.write("\n".join(new_lines) + "\n")
        else:
            self.agg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.agg_path

    def write_pmkid(self, line: str) -> Path:
        """Persist PMKID line to the AP's .hc22000 file."""
        self.captures_dir.mkdir(parents=True, exist_ok=True)
        if self.agg_path.exists():
            existing_pmkids = _read_pmkids(self.agg_path)
            entry = parse_hc22000(line)
            if entry and entry.pmkid_or_mic and entry.pmkid_or_mic not in existing_pmkids:
                with self.agg_path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        else:
            self.agg_path.write_text(line + "\n", encoding="utf-8")
        return self.agg_path

    def consolidate(self, legacy_paths: list[Path]) -> tuple[int, int]:
        """Merge legacy paths into self.agg_path and unlink legacy files."""
        seen_anonces: set[str] = set()
        seen_pmkids: set[str] = set()
        merged_lines: list[str] = []

        if self.agg_path.exists():
            try:
                for ln in self.agg_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    ln = ln.strip()
                    if not ln:
                        continue
                    e = parse_hc22000(ln)
                    if e:
                        if e.kind == "01" and e.pmkid_or_mic:
                            seen_pmkids.add(e.pmkid_or_mic)
                        elif e.kind == "02" and e.anonce:
                            seen_anonces.add(e.anonce)
                    merged_lines.append(ln)
            except OSError:
                pass

        files_to_delete: list[Path] = []
        for lf in legacy_paths:
            try:
                text = lf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            for ln in text.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                e = parse_hc22000(ln)
                if e:
                    if e.kind == "01" and e.pmkid_or_mic:
                        if e.pmkid_or_mic in seen_pmkids:
                            continue
                        seen_pmkids.add(e.pmkid_or_mic)
                    elif e.kind == "02" and e.anonce:
                        if e.anonce in seen_anonces:
                            continue
                        seen_anonces.add(e.anonce)
                merged_lines.append(ln)

            files_to_delete.append(lf)

        deleted = 0
        if merged_lines:
            self.agg_path.write_text("\n".join(merged_lines) + "\n", encoding="utf-8")
            for lf in files_to_delete:
                if lf.resolve() != self.agg_path.resolve():
                    try:
                        lf.unlink()
                        deleted += 1
                    except OSError:
                        pass
            return 1, deleted
        return 0, 0


def save_handshake(ap: AccessPoint, client_mac: str) -> Optional[SaveResult]:
    """Save (unless deduped) 4-way handshake for ap.handshakes[client_mac]"""
    if not ap.ssid:
        return None
    hs = ap.handshakes.get(client_mac)
    if hs is None:
        return None
    lines = eapol_hashlines(ap.ssid, hs)
    if not lines:
        return None

    new_anonces = set()
    for ln in lines:
        entry = parse_hc22000(ln)
        if entry and entry.anonce:
            new_anonces.add(entry.anonce)

    if not new_anonces:
        return None

    hc = HcFiles(Path(Config.captures_dir), ap.ssid, ap.bssid)

    existing_path = None
    all_seen = True
    for anonce in new_anonces:
        p = hc.find_existing_anonce(anonce)
        if p is None:
            all_seen = False
            break
        existing_path = p

    if all_seen and existing_path:
        return SaveResult(path=existing_path, was_new=False)

    target_path = hc.write_handshake(lines)

    if Config.save_pcap:
        pcap_path = _fresh_path(hc.captures_dir, ap.ssid, ap.bssid, "_handshake.pcap")
        write_pcap(pcap_path, _pcap_records_for(ap, client_mac))

    return SaveResult(path=target_path, was_new=True)


def save_pmkid(ap: AccessPoint, client_mac: str) -> Optional[SaveResult]:
    """Save (unless deduped) PMKID for ap.handshakes[client_mac]"""
    if not ap.ssid:
        return None
    hs = ap.handshakes.get(client_mac)
    if hs is None:
        return None
    line = pmkid_hashline(ap.ssid, hs)
    if not line:
        return None
    entry = parse_hc22000(line)
    if not entry or not entry.pmkid_or_mic:
        return None

    hc = HcFiles(Path(Config.captures_dir), ap.ssid, ap.bssid)

    existing_path = hc.find_existing_pmkid(entry.pmkid_or_mic)
    if existing_path:
        return SaveResult(path=existing_path, was_new=False)

    target_path = hc.write_pmkid(line)
    return SaveResult(path=target_path, was_new=True)


def consolidate_hc_files(captures_dir: Path) -> tuple[int, int]:
    """Merge legacy timestamped .hc22000 files into 1 .hc22000 file per AP.

    Returns (migrated_target_count, deleted_legacy_count).
    """
    if not captures_dir.is_dir():
        return 0, 0

    groups: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for p in captures_dir.iterdir():
        if not p.is_file():
            continue
        m = LEGACY_CAPTURE_RE.match(p.name)
        if m and m.group("ext") == "hc22000" and m.group("kind") in ("handshake", "pmkid"):
            groups[(m.group("ssid"), bssid_to_colon(m.group("bssid")))].append(p)

    migrated_total = 0
    deleted_total = 0
    for (ssid, bssid), legacy_paths in groups.items():
        hc = HcFiles(captures_dir, ssid, bssid)
        migrated, deleted = hc.consolidate(legacy_paths)
        migrated_total += migrated
        deleted_total += deleted

    return migrated_total, deleted_total


# ----- WEP key --------------------------------------------------------------

def save_wep_key(ap: AccessPoint, key: bytes) -> Optional[SaveResult]:
    """Persist a recovered WEP key. Dedupes by exact key value for this BSSID."""
    captures_dir = Path(Config.captures_dir)
    if not key:
        return None
    key_hex = key.hex()
    for p in _existing(captures_dir, ap.bssid, "_wep_key.txt"):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = WEP_KEY_HEX_RE.search(text)
        if m and m.group(1).lower() == key_hex:
            return SaveResult(path=p, was_new=False)

    captures_dir.mkdir(parents=True, exist_ok=True)
    path = _fresh_path(captures_dir, ap.ssid, ap.bssid, "_wep_key.txt")
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

def save_wps_pin(ap: AccessPoint, pin: str, psk: str) -> Optional[SaveResult]:
    """Persist a WPS-PIN credential. Dedupes by (PIN, PSK) for this BSSID."""
    captures_dir = Path(Config.captures_dir)
    if not pin or not psk:
        return None
    for p in _existing(captures_dir, ap.bssid, "_wps_pin.txt"):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        psk_match = WPS_PSK_RE.search(text)
        pin_match = WPS_PIN_RE.search(text)
        if (psk_match and psk_match.group(1).strip() == psk
                and pin_match and pin_match.group(1).strip() == pin):
            return SaveResult(path=p, was_new=False)

    captures_dir.mkdir(parents=True, exist_ok=True)
    path = _fresh_path(captures_dir, ap.ssid, ap.bssid, "_wps_pin.txt")
    body = (
        f"SSID: {ap.ssid or ''}\n"
        f"BSSID: {ap.bssid}\n"
        f"PSK: {psk}\n"
        f"PIN: {pin}\n"
    )
    path.write_text(body, encoding="utf-8")
    return SaveResult(path=path, was_new=True)


def save_wps_pbc(ap: AccessPoint, psk: str) -> Optional[SaveResult]:
    """Persist a WPS-PBC credential. Dedupes by PSK for this BSSID."""
    captures_dir = Path(Config.captures_dir)
    if not psk:
        return None
    for p in _existing(captures_dir, ap.bssid, "_wps_pbc.txt"):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = WPS_PSK_RE.search(text)
        if m and m.group(1).strip() == psk:
            return SaveResult(path=p, was_new=False)

    captures_dir.mkdir(parents=True, exist_ok=True)
    path = _fresh_path(captures_dir, ap.ssid, ap.bssid, "_wps_pbc.txt")
    body = (
        f"SSID: {ap.ssid or ''}\n"
        f"BSSID: {ap.bssid}\n"
        f"PSK: {psk}\n"
    )
    path.write_text(body, encoding="utf-8")
    return SaveResult(path=path, was_new=True)
