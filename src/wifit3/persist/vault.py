"""In-memory index over captures/ plus the single write path for capture
artifacts. Loaded once at startup and refreshed on each save, so reads never
re-scan the directory. Wraps persist.save + persist.capture_history."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
import subprocess
import sys
import time
import unicodedata
import zipfile
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from wifit3.crack import wpa_psk
from wifit3.crack.hc22000_format import Hc22000Line, parse_hc22000
from wifit3.dot11 import wpa
from wifit3.crack.passwords import DEFAULT_PASSWORDS_PATH
from wifit3.models import AccessPoint, CaptureType, PersistedCapture
from wifit3.persist import save
from wifit3.persist.metadata import metadata_path, save_ap_metadata
from wifit3.persist.capture_history import load_capture_index, summarize
from wifit3.persist.common import LEGACY_CAPTURE_RE, bssid_to_dashed, safe_ssid
from wifit3.persist.config import Config
from wifit3.persist.save import SaveResult
from wifit3.vault.manager import JobManager
from wifit3.vault.tools.cpu_crack import CpuCrackTool

logger = logging.getLogger(__name__)

_BAND_SUFFIX_RE = re.compile(r"(?:[\s_-]+(?:2[._]?4|5|6)g(?:hz)?)$", re.IGNORECASE)


def _ssid_family(ssid: str) -> str:
    normalized = unicodedata.normalize("NFKC", ssid).casefold().strip()
    return _BAND_SUFFIX_RE.sub("", normalized).rstrip(" _-")


def _related_ssids(left: str, right: str) -> bool:
    left_family = _ssid_family(left)
    right_family = _ssid_family(right)
    if not left_family or not right_family:
        return False
    if left_family == right_family:
        return True
    shorter, longer = sorted((left_family, right_family), key=len)
    prefixes = tuple(f"{shorter}{separator}" for separator in ("_", "-", " "))
    return len(shorter) >= 4 and longer.startswith(prefixes)


def _related_bssids(left: str, right: str) -> bool:
    left_octets = left.replace("-", ":").lower().split(":")
    right_octets = right.replace("-", ":").lower().split(":")
    return len(left_octets) == 6 and len(right_octets) == 6 and left_octets[:5] == right_octets[:5]


@dataclass(frozen=True, slots=True)
class PskPropagationResult:
    saved_psks: int
    candidate_checks: int
    verified_aps: int
    unverifiable_checks: int


def _open_in_file_manager(path: Path) -> None:
    if sys.platform.startswith("win"):
        import os
        os.startfile(path)                                        # noqa: S606 (Windows only)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _is_m1m2_eapol(line: Hc22000Line) -> bool:
    return line.kind == "02" and int(line.message_pair or "0", 16) & 0x07 == 0


def crosses_auto_crack_threshold(before: List[Hc22000Line], after: List[Hc22000Line]) -> bool:
    """True when a save gives an AP its first line, or its first PMKID / authorized EAPOL
    line after only M1+M2 lines; at most twice per AP."""
    if not before:
        return bool(after)
    return (all(_is_m1m2_eapol(line) for line in before)
            and any(not _is_m1m2_eapol(line) for line in after))


class Vault:
    """Caches the on-disk capture index (from Config.captures_dir) and is the
    backend interface for querying/deleting capture files.
    """

    # Capture-kind -> human label, in display order. The one place these map.
    _KIND_LABELS = {
        CaptureType.HS: "Handshake",
        CaptureType.PMKID: "PMKID",
        CaptureType.WEP: "WEP Key",
        CaptureType.WPS_PIN: "WPS PIN",
        CaptureType.WPS_PBC: "WPS PBC",
        CaptureType.WPA_PSK: "WPA PSK",
        CaptureType.EAP_CREDENTIAL: "EAP Credential",
    }

    _PSK_TYPES = (CaptureType.WPS_PIN, CaptureType.WPS_PBC, CaptureType.WPA_PSK)

    def __init__(self) -> None:
        self._index: Dict[str, List[PersistedCapture]] = {}
        self._change_listeners: list[Callable[[], None]] = []
        self._probed_ssids_source: Callable[[], Iterable[str]] = lambda: ()
        self._metadata_prompted_bssids: set[str] = set()
        self.revision = 0   # bumped on every index change; a cache key for readers
        self.errors: list[str] = []                       # surfaced as toasts by the app on mount
        try:
            self.refresh()
        except Exception as exc:
            logger.exception("Vault: failed to load the capture index")
            self._index = {}
            self.revision += 1
            self.errors.append(f"Failed to load captures: {exc}")
        self.manager = JobManager(self)
        try:
            self.manager.reconcile_on_startup()
        except Exception as exc:
            logger.exception("Vault: job reconciliation failed on startup")
            self.errors.append(f"Failed to reconcile jobs: {exc}")

    def refresh(self) -> None:
        """Re-scan Config.captures_dir into the cache."""
        self._index = load_capture_index()
        self._notify_changed()

    def subscribe(self, listener: Callable[[], None]) -> None:
        self._change_listeners.append(listener)

    def set_probed_ssids_source(self, source: Callable[[], Iterable[str]]) -> None:
        """Where auto-crack reads this session's probed SSIDs, tried as passwords before the wordlist."""
        self._probed_ssids_source = source

    def _notify_changed(self) -> None:
        self.revision += 1
        for listener in self._change_listeners:
            try:
                listener()
            except Exception:
                logger.exception("Vault change listener failed")

    def _insert_capture(self, capture: PersistedCapture) -> None:
        self._index.setdefault(capture.bssid, []).insert(0, capture)
        self._notify_changed()

    # ----- reads -----

    def persisted(self, bssid: str) -> List[PersistedCapture]:
        """This AP's saved captures, newest-first (empty if none)."""
        return self._index.get(bssid, [])

    def all_captures(self) -> List[PersistedCapture]:
        """Every saved capture across all APs, unordered (callers sort as needed)."""
        return [c for caps in self._index.values() for c in caps]

    def missing_metadata_count(self) -> int:
        """Number of saved APs that have no v2 metadata sidecar."""
        return sum(not metadata_path(bssid).is_file() for bssid in self._index)

    def claim_new_missing_metadata(self) -> set[str]:
        """Return metadata-less APs not offered this session, marking them offered."""
        missing = {bssid for bssid in self._index if not metadata_path(bssid).is_file()}
        new_missing = missing - self._metadata_prompted_bssids
        self._metadata_prompted_bssids.update(new_missing)
        return new_missing

    def capture_payload(self, capture: PersistedCapture) -> str:
        """Copyable text for a capture: its stored value (WEP/WPS), else the file's
        own contents (HS/PMKID hashlines). Empty string if unreadable."""
        if capture.value is not None:
            return capture.value
        try:
            return Path(capture.path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""

    def summary(self) -> Optional[str]:
        """One-line count of every saved capture, e.g. '3 handshakes, 1 WEP key',
        or None when nothing is saved."""
        hs, pmkid, wep, psk = summarize(self._index)
        parts = []
        if hs:
            parts.append(f"{hs} handshake{'s' * (hs != 1)}")
        if pmkid:
            parts.append(f"{pmkid} PMKID{'s' * (pmkid != 1)}")
        if wep:
            parts.append(f"{wep} WEP key{'s' * (wep != 1)}")
        if psk:
            parts.append(f"{psk} PSK{'s' * (psk != 1)}")
        return ", ".join(parts) or None

    def detailed_summary(self, ap: "AccessPoint") -> dict[str, tuple[PersistedCapture, int]]:
        """Per-kind rollup for the Focus 'Existing captures' panel:
        ``{human label: (newest_capture, count)}``, absent kinds omitted, kind order."""
        caps = self.persisted(ap.bssid)
        result: dict[str, tuple[PersistedCapture, int]] = {}
        for kind, label in self._KIND_LABELS.items():
            matching = [c for c in caps if c.type == kind]
            if matching:
                result[label] = (max(matching, key=lambda c: c.timestamp), len(matching))
        return result

    def known_psk(self, ap: "AccessPoint") -> Optional[str]:
        """The passphrase held for this AP: recovered this session (PBC/PIN) or
        loaded from a prior session's WPS file, else None. A WPS PIN alone does
        not count."""
        return (
            ap.wps_pbc_psk
            or ap.wps_pin_psk
            or next((p.value for p in self.persisted(ap.bssid)
                     if p.type in self._PSK_TYPES and p.value), None)
        )

    def has_psk(self, ap: "AccessPoint") -> bool:
        """True once we hold this AP's passphrase (see known_psk)."""
        return self.known_psk(ap) is not None

    def verify_wpa_psk(self, bssid: str, psk: str) -> Optional[bool]:
        """Check a PSK against saved HC22000 data, or return None when none is usable."""
        lines_by_essid: dict[bytes, list[Hc22000Line]] = {}
        paths = {
            capture.path for capture in self.persisted(bssid)
            if capture.type in (CaptureType.HS, CaptureType.PMKID)
        }
        compact_bssid = bssid.replace(":", "").lower()
        for path in paths:
            try:
                raw_lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for raw_line in raw_lines:
                line = parse_hc22000(raw_line)
                if line is None or line.mac_ap != compact_bssid or not wpa_psk.line_is_verifiable(line):
                    continue
                try:
                    essid = bytes.fromhex(line.essid)
                except ValueError:
                    continue
                lines_by_essid.setdefault(essid, []).append(line)
        if not lines_by_essid:
            return None
        raw_pmk = len(psk) == 64 and all(character in "0123456789abcdefABCDEF" for character in psk)
        for essid, lines in lines_by_essid.items():
            pmk = bytes.fromhex(psk) if raw_pmk else wpa.derive_pmk(psk, essid)
            if any(wpa_psk.psk_matches(pmk, line) for line in lines):
                return True
        return False

    def _related_psk_candidates(self, source_bssid: str, source_ssid: str | None):
        for bssid, captures in list(self._index.items()):
            if bssid == source_bssid:
                continue
            ssid = next((capture.ssid for capture in captures if capture.ssid), None)
            if not ((_related_ssids(source_ssid, ssid) if source_ssid and ssid else False)
                    or _related_bssids(source_bssid, bssid)):
                continue
            if any(capture.type in self._PSK_TYPES and capture.value for capture in captures):
                continue
            yield bssid, ssid

    def propagate_verified_psk(self, source_bssid: str, source_ssid: str | None,
                               psk: str) -> list[str]:
        """Verify a recovered PSK against related saved APs and persist matching ones."""
        matched: list[str] = []
        for bssid, ssid in self._related_psk_candidates(source_bssid, source_ssid):
            if self.verify_wpa_psk(bssid, psk) is not True:
                continue
            self.save_wpa_psk(ssid, bssid, psk)
            matched.append(bssid)
        return matched

    def verify_saved_psks_for_related_aps(self) -> PskPropagationResult:
        """Try every saved PSK against related AP captures and persist verified matches."""
        sources: list[tuple[str, str | None, str]] = []
        for bssid, captures in self._index.items():
            ssid = next((capture.ssid for capture in captures if capture.ssid), None)
            keys = {capture.value for capture in captures
                    if capture.type in self._PSK_TYPES and capture.value}
            sources.extend((bssid, ssid, key) for key in keys)

        checks = 0
        unverifiable = 0
        verified: set[str] = set()
        for source_bssid, source_ssid, psk in sources:
            for bssid, ssid in self._related_psk_candidates(source_bssid, source_ssid):
                checks += 1
                verdict = self.verify_wpa_psk(bssid, psk)
                if verdict is None:
                    unverifiable += 1
                elif verdict:
                    self.save_wpa_psk(ssid, bssid, psk)
                    verified.add(bssid)
        return PskPropagationResult(len(sources), checks, len(verified), unverifiable)

    def has_handshake(self, ap: "AccessPoint") -> bool:
        return self._has(ap.bssid, CaptureType.HS)

    def has_pmkid(self, ap: "AccessPoint") -> bool:
        return self._has(ap.bssid, CaptureType.PMKID)

    def has_wep_key(self, ap: "AccessPoint") -> bool:
        return self._has(ap.bssid, CaptureType.WEP)

    def has_wps_psk(self, ap: "AccessPoint") -> bool:
        return any(c.type in (CaptureType.WPS_PIN, CaptureType.WPS_PBC) and c.value
                   for c in self.persisted(ap.bssid))

    def wps_capture(self, ap: "AccessPoint") -> Optional[PersistedCapture]:
        """Newest saved WPS credential for this AP (PIN- or PBC-derived), or None."""
        return next((c for c in self.persisted(ap.bssid)
                     if c.type in (CaptureType.WPS_PIN, CaptureType.WPS_PBC) and c.value), None)

    def _has(self, bssid: str, kind: CaptureType) -> bool:
        return any(c.type == kind for c in self.persisted(bssid))

    # ----- writes (persist to disk, then fold into the cache) -----

    def save_handshake(self, ap: "AccessPoint", client_mac: str) -> Optional[SaveResult]:
        before = self._hc_lines(ap)
        result = save.save_handshake(ap, client_mac)
        if result:
            save_ap_metadata(ap)
        if result and result.was_new:
            capture = PersistedCapture(
                type=CaptureType.HS, timestamp=int(time.time()), path=str(result.path),
                bssid=ap.bssid, ssid=ap.ssid)
            self._insert_capture(capture)
            self._auto_crack(ap, capture, before)
        return result

    def save_pmkid(self, ap: "AccessPoint", client_mac: str) -> Optional[SaveResult]:
        before = self._hc_lines(ap)
        result = save.save_pmkid(ap, client_mac)
        if result:
            save_ap_metadata(ap)
        if result and result.was_new:
            capture = PersistedCapture(
                type=CaptureType.PMKID, timestamp=int(time.time()), path=str(result.path),
                bssid=ap.bssid, ssid=ap.ssid)
            self._insert_capture(capture)
            self._auto_crack(ap, capture, before)
        return result

    def _hc_lines(self, ap: "AccessPoint") -> List[Hc22000Line]:
        return save.HcFiles(Path(Config.captures_dir), ap.ssid, ap.bssid).lines()

    def _auto_crack(self, ap: "AccessPoint", capture: PersistedCapture,
                    before: List[Hc22000Line]) -> None:
        if not Config.auto_crack or self.has_psk(ap):
            return
        if crosses_auto_crack_threshold(before, self._hc_lines(ap)):
            self.manager.submit_job(CpuCrackTool.name, capture,
                                    {"wordlists": [str(DEFAULT_PASSWORDS_PATH)],
                                     "extra_passwords": sorted(self._probed_ssids_source())},
                                    automatic=True)

    def save_wep_key(self, ap: "AccessPoint", key: bytes) -> Optional[SaveResult]:
        result = save.save_wep_key(ap, key)
        if result:
            save_ap_metadata(ap, wep_key=key.hex())
        if result and result.was_new:
            self._insert_capture(PersistedCapture(
                type=CaptureType.WEP, timestamp=int(time.time()), path=str(result.path),
                bssid=ap.bssid, value=key.hex(), ssid=ap.ssid))
        return result

    def save_wps_pin(self, ap: "AccessPoint", pin: str, psk: Optional[str]) -> Optional[SaveResult]:
        result = save.save_wps_pin(ap, pin, psk)
        if result:
            save_ap_metadata(ap, psk=psk, wps_pin=pin)
        if result and result.was_new:
            self._insert_capture(PersistedCapture(
                type=CaptureType.WPS_PIN, timestamp=int(time.time()), path=str(result.path),
                bssid=ap.bssid, value=psk, pin=pin, ssid=ap.ssid))
        return result

    def save_wps_pbc(self, ap: "AccessPoint", psk: str) -> Optional[SaveResult]:
        result = save.save_wps_pbc(ap, psk)
        if result:
            save_ap_metadata(ap, psk=psk)
        if result and result.was_new:
            self._insert_capture(PersistedCapture(
                type=CaptureType.WPS_PBC, timestamp=int(time.time()), path=str(result.path),
                bssid=ap.bssid, value=psk, ssid=ap.ssid))
        return result

    def save_wpa_psk(self, ssid: Optional[str], bssid: str, psk: str) -> Optional[SaveResult]:
        result = save.save_wpa_psk(ssid, bssid, psk)
        if result:
            save_ap_metadata(
                AccessPoint(bssid=bssid, ssid=ssid),
                psk=psk,
                include_observed_data=False,
            )
        if result and result.was_new:
            self._insert_capture(PersistedCapture(
                type=CaptureType.WPA_PSK, timestamp=int(time.time()), path=str(result.path),
                bssid=bssid, value=psk, ssid=ssid))
        return result

    def save_eap_credential(
        self, ap: "AccessPoint", username: str, password: str, eap_settings: dict[str, str]
    ) -> Optional[SaveResult]:
        result = save.save_eap_credential(ap, username, password)
        if result:
            save_ap_metadata(ap, eap_credential=(username, password), eap_settings=eap_settings)
        if result and result.was_new:
            self._insert_capture(PersistedCapture(
                type=CaptureType.EAP_CREDENTIAL, timestamp=int(time.time()), path=str(result.path),
                bssid=ap.bssid, value=f"{username}:{password}", ssid=ap.ssid))
        return result

    # ----- filesystem ops (the screen goes through these, never touches disk) -----

    def delete_capture(self, capture: PersistedCapture) -> None:
        """Delete a capture's file and drop every cache entry backed by it (an
        aggregate .hc22000 backs both an HS and a PMKID entry)."""
        Path(capture.path).unlink(missing_ok=True)
        changed = False
        affected_bssids: set[str] = set()
        for bssid in list(self._index):
            remaining = [c for c in self._index[bssid] if c.path != capture.path]
            if len(remaining) == len(self._index[bssid]):
                continue
            changed = True
            affected_bssids.add(bssid)
            if remaining:
                self._index[bssid] = remaining
            else:
                del self._index[bssid]
        for bssid in affected_bssids:
            if bssid not in self._index:
                metadata_path(bssid).unlink(missing_ok=True)
        if changed:
            self._notify_changed()

    def zip_captures(self, captures: List[PersistedCapture],
                     save_as: Optional[Path] = None) -> Optional[Path]:
        """Zip the given captures' files (deduped) into ``save_as``, defaulting to a
        timestamped archive beside captures/. None if none of the files exist."""
        paths: List[Path] = []
        seen: set[str] = set()
        bssids: set[str] = set()
        for c in captures:
            bssids.add(c.bssid)
            p = Path(c.path)
            if p.is_file() and str(p) not in seen:
                seen.add(str(p))
                paths.append(p)
        for bssid in bssids:
            path = metadata_path(bssid)
            if path.is_file() and str(path) not in seen:
                seen.add(str(path))
                paths.append(path)
        if not paths:
            return None
        if save_as is None:
            save_as = Path(Config.captures_dir).parent / f"wifit3_captures_{int(time.time())}.zip"
        with zipfile.ZipFile(save_as, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in paths:
                zf.write(p, arcname=p.name)
        return save_as

    def open_directory(self) -> None:
        """Reveal captures/ in the OS file manager."""
        _open_in_file_manager(Path(Config.captures_dir))

    # ----- legacy .hc22000 consolidation (temporary: pre-aggregate installs) ---

    def _legacy_hc_files(self) -> List[Path]:
        """Pre-aggregate per-capture .hc22000 files still on disk (one file per
        capture, vs today's one aggregate file per AP)."""
        root = Path(Config.captures_dir)
        if not root.is_dir():
            return []
        return [p for p in root.iterdir()
                if p.is_file() and (m := LEGACY_CAPTURE_RE.match(p.name))
                and m.group("ext") == "hc22000"]

    def legacy_hc_file_count(self) -> int:
        """Number of legacy per-capture .hc22000 files that could be consolidated."""
        return len(self._legacy_hc_files())

    def legacy_hc_unique_count(self) -> int:
        """Distinct SSID+BSSID the legacy files would collapse into (one file each)."""
        return len({f"{safe_ssid(m.group('ssid'))}_{bssid_to_dashed(m.group('bssid'))}"
                    for p in self._legacy_hc_files()
                    if (m := LEGACY_CAPTURE_RE.match(p.name))})

    def consolidate_legacy_hc_files(self) -> tuple[int, int]:
        """Merge legacy per-capture .hc22000 files into one per AP, then refresh the
        cache. Returns (migrated_ap_count, deleted_file_count)."""
        migrated, deleted = save.consolidate_hc_files(Path(Config.captures_dir))
        self.refresh()
        return migrated, deleted
