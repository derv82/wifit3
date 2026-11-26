"""Import of capture files, directories and zips into the captures directory."""
from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from wifit3.crack.handshake import hc22000_line
from wifit3.crack.hc22000_format import parse_hc22000, pmkid_hashline
from wifit3.models import CaptureType
from wifit3.persist.capture_history import body_ssid, read_wep_key, read_wps_psk
from wifit3.persist.common import LEGACY_CAPTURE_RE, bssid_to_colon
from wifit3.persist.config import Config
from wifit3.persist.hc22000 import Hc22000Import
from wifit3.persist.pcap import FourWayHandshake, pcap_frames, read_access_points, read_handshakes
from wifit3.persist.save import existing_files, fresh_path
from wifit3.persist.vault import Vault

PCAP_EXTENSIONS = (".cap", ".pcap", ".pcapng")
IMPORT_EXTENSIONS = (".hc22000", *PCAP_EXTENSIONS, ".txt", ".meta")


@dataclass
class CaptureImport(Hc22000Import):
    """One import: the selected files and directories (recursive), zips read in memory; other file types
    and anything in Config.captures_dir are skipped."""
    files: int = 0
    four_ways: dict[tuple[str, str, bytes], FourWayHandshake] = field(default_factory=dict)  # (BSSID, client, ANonce)
    psks: list[tuple[str, str, str, str]] = field(default_factory=list)    # (name, SSID, BSSID, PSK)
    keys: int = 0

    @property
    def summary(self) -> str:
        """``Imported 41 handshakes, 12 PMKIDs & 3 keys (38 APs) from 120 files, 9 already in Vault``."""
        text = (f"Imported {self.handshakes} handshakes, {self.pmkids} PMKIDs & {self.keys} keys "
                f"({len(self.bssids)} APs) from {self.files} files")
        if self.duplicates:
            text += f", {self.duplicates} already in Vault"
        return text

    def add_paths(self, paths: list[Path]) -> None:
        captures_dir = Path(Config.captures_dir).resolve()
        for path in paths:
            if path.is_dir():
                file_paths = sorted(p for p in path.rglob("*") if p.is_file())
            else:
                file_paths = [path]
            for file_path in file_paths:
                if file_path.resolve().is_relative_to(captures_dir):
                    continue
                name = file_path.relative_to(path.parent).as_posix()
                if file_path.suffix.lower() == ".zip":
                    self._add_zip(name, file_path)
                    continue
                if not self._is_import_file(file_path.name):
                    continue
                try:
                    data = file_path.read_bytes()
                    date = int(file_path.stat().st_mtime)
                except OSError:
                    self.problems.append((name, "unreadable"))
                    continue
                self._add_file(name, data, date)
        if Config.save_pcap:
            self._write_pcaps()
        self.write_meta()

    @staticmethod
    def _is_import_file(filename: str) -> bool:
        """An importable extension; a .txt only when wifit3 named it (a key file)."""
        path = PurePosixPath(filename)
        if path.suffix.lower() == ".txt":
            return LEGACY_CAPTURE_RE.match(path.name) is not None
        return path.suffix.lower() in IMPORT_EXTENSIONS

    def _add_zip(self, name: str, path: Path) -> None:
        try:
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    entry_name = f"{name}/{info.filename}"
                    if info.filename.lower().endswith(".zip"):
                        self.problems.append((entry_name, "zip inside zip, skipped"))
                        continue
                    if not self._is_import_file(info.filename):
                        continue
                    self._add_file(entry_name, archive.read(info), int(datetime(*info.date_time).timestamp()))
        except zipfile.BadZipFile:
            self.problems.append((name, "corrupt zip"))
        except OSError:
            self.problems.append((name, "unreadable"))

    def _add_file(self, name: str, data: bytes, date: int) -> None:
        self.files += 1
        extension = PurePosixPath(name).suffix.lower()
        if extension == ".hc22000":
            self.add_file(name, data.decode("utf-8", errors="replace"), date)
        elif extension in PCAP_EXTENSIONS:
            self._add_pcap(name, data)
        elif extension == ".txt":
            self._add_key_file(name, data)
        else:
            self.problems.append((name, "not supported yet"))

    def _add_key_file(self, name: str, data: bytes) -> None:
        """Copy a WEP key or WPS PIN file in under its own name; keep a WPA or WPS PBC PSK for save_psks."""
        filename = PurePosixPath(name).name
        match = LEGACY_CAPTURE_RE.match(filename)
        text = data.decode("utf-8", errors="replace")
        ssid = body_ssid(text, match.group("ssid"))
        bssid = bssid_to_colon(match.group("bssid"))
        kind = match.group("kind")
        if kind in ("wpa_psk", "wps_pbc"):
            psk = read_wps_psk(text)
            if psk is None:
                self.problems.append((name, "no PSK found"))
            else:
                self.psks.append((name, ssid, bssid, psk))
        elif kind == "wep_key" and read_wep_key(text) is None:
            self.problems.append((name, "no WEP key found"))
        elif kind in ("wep_key", "wps_pin"):
            path = Path(Config.captures_dir) / filename
            if path.exists():
                self.duplicates += 1
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self.keys += 1
            self.bssids.add(bssid)

    def save_psks(self, vault: Vault) -> None:
        """Save each PSK as typed in, for an AP the Vault has a handshake or PMKID of; then for its related APs."""
        for name, ssid, bssid, psk in self.psks:
            captures = [c for c in vault.persisted(bssid) if c.type in (CaptureType.HS, CaptureType.PMKID)]
            if not captures:
                self.problems.append((name, "no handshake or PMKID of this AP, PSK skipped"))
                continue
            result = vault.save_wpa_psk(ssid, bssid, psk, manual=True)
            if result is None or not result.was_new:
                self.duplicates += 1
                continue
            self.keys += 1
            self.bssids.add(bssid)
            vault.propagate_verified_psk(bssid, ssid, psk)

    def _add_pcap(self, name: str, data: bytes) -> None:
        """Each 4-way's and PMKID's hashline, dated by its frame; the 4-ways are kept for _write_pcaps."""
        found = False
        for ap in read_access_points(name, pcap_frames(data)):
            pairs_by_client = {hs.client_mac: hs.valid_pairs_by_instance() for hs in ap.handshakes.values()}
            crackable = [hs for hs in ap.handshakes.values() if pairs_by_client[hs.client_mac] or hs.pmkid]
            if not crackable:
                continue
            found = True
            if not ap.ssid_bytes:
                self.problems.append((name, f"AP {ap.bssid} has no SSID, skipped"))
                continue
            for hs in crackable:
                for anonce, pair in pairs_by_client[hs.client_mac].items():
                    hashline = hc22000_line(ap.ssid_bytes, hs, pair)
                    self.add_hashline(hashline, parse_hc22000(hashline), int(pair.mic_frame.timestamp))
                    handshake = FourWayHandshake.from_pair(hs, pair, ap.ssid or "", ap.ssid_bytes,
                                                           ap.decloak_reveal_frame)
                    key = (ap.bssid, hs.client_mac, anonce)
                    if key not in self.four_ways or len(handshake.messages) > len(self.four_ways[key].messages):
                        self.four_ways[key] = handshake
                pmkid = pmkid_hashline(ap.ssid_bytes, hs)
                if pmkid is not None:
                    date = min((m.timestamp for m in hs.messages if hs.pmkid in m.raw), default=ap.last_seen)
                    self.add_hashline(pmkid, parse_hc22000(pmkid), int(date))
        if not found:
            self.problems.append((name, "no handshakes or PMKIDs found"))

    def _write_pcaps(self) -> None:
        """Each 4-way the AP has no pcap of, in its own pcap named by its first frame's time."""
        captures_dir = Path(Config.captures_dir)
        saved_by_bssid: dict[str, set[tuple[str, bytes]]] = {}
        for (bssid, client_mac, anonce), handshake in self.four_ways.items():
            if bssid not in saved_by_bssid:
                saved = read_handshakes(bssid, handshake.ssid, existing_files(captures_dir, bssid, ".pcap"))
                saved_by_bssid[bssid] = {(h.client_mac, bytes.fromhex(h.hashline.anonce)) for h in saved if h.hashline}
            if (client_mac, anonce) in saved_by_bssid[bssid]:
                continue
            handshake.write(fresh_path(captures_dir, handshake.ssid, bssid, "_handshake.pcap", int(handshake.first_seen)))
