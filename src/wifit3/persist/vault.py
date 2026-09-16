"""In-memory index over captures/ plus the single write path for capture
artifacts. Loaded once at startup and refreshed on each save, so reads never
re-scan the directory. Wraps persist.save + persist.capture_history."""
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from wifit3.models import PersistedCapture
from wifit3.persist import save
from wifit3.persist.capture_history import load_capture_index, summarize
from wifit3.persist.common import LEGACY_CAPTURE_RE, bssid_to_dashed, safe_ssid
from wifit3.persist.config import Config
from wifit3.persist.save import SaveResult

if TYPE_CHECKING:
    from wifit3.models import AccessPoint


class Vault:
    """Caches the on-disk capture index (from Config.captures_dir) and is the
    sole read/write path for handshake, PMKID, WEP, and WPS artifacts."""

    # Capture-kind -> human label, in display order. The one place these map.
    _KIND_LABELS = {"HS": "Handshake", "PMKID": "PMKID", "WEP": "WEP Key", "WPS": "WPS PSK"}

    def __init__(self) -> None:
        self._index: Dict[str, List[PersistedCapture]] = {}
        self.refresh()

    def refresh(self) -> None:
        """Re-scan Config.captures_dir into the cache."""
        self._index = load_capture_index()

    # ----- reads -----

    def persisted(self, bssid: str) -> List[PersistedCapture]:
        """This AP's saved captures, newest-first (empty if none)."""
        return self._index.get(bssid, [])

    def summary(self) -> Optional[str]:
        """One-line count of every saved capture, e.g. '3 handshakes, 1 WEP key',
        or None when nothing is saved."""
        hs, pmkid, wep, wps = summarize(self._index)
        parts = []
        if hs:
            parts.append(f"{hs} handshake{'s' * (hs != 1)}")
        if pmkid:
            parts.append(f"{pmkid} PMKID{'s' * (pmkid != 1)}")
        if wep:
            parts.append(f"{wep} WEP key{'s' * (wep != 1)}")
        if wps:
            parts.append(f"{wps} WPS PSK{'s' * (wps != 1)}")
        return ", ".join(parts) or None

    def detailed_summary(self, ap: "AccessPoint") -> dict[str, tuple[str | None, int, int]]:
        """Per-kind rollup for the Focus 'Existing captures' panel:
        ``{human label: (key_or_None, count, newest_timestamp)}``, absent kinds
        omitted, ordered by kind."""
        caps = self.persisted(ap.bssid)
        result: dict[str, tuple[str | None, int, int]] = {}
        for kind, label in self._KIND_LABELS.items():
            matching = [c for c in caps if c.type == kind]
            if matching:
                newest = max(matching, key=lambda c: c.timestamp)
                result[label] = (newest.value, len(matching), newest.timestamp)
        return result

    def known_psk(self, ap: "AccessPoint") -> Optional[str]:
        """The passphrase held for this AP: recovered this session (PBC/PIN) or
        loaded from a prior session's WPS file, else None. A WPS PIN alone does
        not count."""
        return (
            ap.wps_pbc_psk
            or ap.wps_pin_psk
            or next((p.value for p in self.persisted(ap.bssid)
                     if p.type == "WPS" and p.value), None)
        )

    def has_psk(self, ap: "AccessPoint") -> bool:
        """True once we hold this AP's passphrase (see known_psk)."""
        return self.known_psk(ap) is not None

    def has_handshake(self, ap: "AccessPoint") -> bool:
        return self._has(ap.bssid, "HS")

    def has_pmkid(self, ap: "AccessPoint") -> bool:
        return self._has(ap.bssid, "PMKID")

    def has_wep_key(self, ap: "AccessPoint") -> bool:
        return self._has(ap.bssid, "WEP")

    def has_wps_psk(self, ap: "AccessPoint") -> bool:
        return self._has(ap.bssid, "WPS")

    def wps_capture(self, ap: "AccessPoint") -> Optional[PersistedCapture]:
        """Newest saved WPS credential for this AP (PIN- or PBC-derived), or None."""
        return next((c for c in self.persisted(ap.bssid) if c.type == "WPS" and c.value), None)

    def _has(self, bssid: str, kind: str) -> bool:
        return any(c.type == kind for c in self.persisted(bssid))

    # ----- writes (persist to disk, then fold into the cache) -----

    def save_handshake(self, ap: "AccessPoint", client_mac: str) -> Optional[SaveResult]:
        result = save.save_handshake(ap, client_mac)
        if result and result.was_new:
            self._index.setdefault(ap.bssid, []).insert(
                0, PersistedCapture(type="HS", timestamp=int(time.time()), path=str(result.path)))
        return result

    def save_pmkid(self, ap: "AccessPoint", client_mac: str) -> Optional[SaveResult]:
        result = save.save_pmkid(ap, client_mac)
        if result and result.was_new:
            self._index.setdefault(ap.bssid, []).insert(
                0, PersistedCapture(type="PMKID", timestamp=int(time.time()), path=str(result.path)))
        return result

    def save_wep_key(self, ap: "AccessPoint", key: bytes) -> Optional[SaveResult]:
        result = save.save_wep_key(ap, key)
        if result and result.was_new:
            self._index.setdefault(ap.bssid, []).insert(
                0, PersistedCapture(type="WEP", timestamp=int(time.time()),
                                    path=str(result.path), value=key.hex()))
        return result

    def save_wps_pin(self, ap: "AccessPoint", pin: str, psk: str) -> Optional[SaveResult]:
        result = save.save_wps_pin(ap, pin, psk)
        if result and result.was_new:
            self._index.setdefault(ap.bssid, []).insert(
                0, PersistedCapture(type="WPS", timestamp=int(time.time()),
                                    path=str(result.path), value=psk))
        return result

    def save_wps_pbc(self, ap: "AccessPoint", psk: str) -> Optional[SaveResult]:
        result = save.save_wps_pbc(ap, psk)
        if result and result.was_new:
            self._index.setdefault(ap.bssid, []).insert(
                0, PersistedCapture(type="WPS", timestamp=int(time.time()),
                                    path=str(result.path), value=psk))
        return result

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
