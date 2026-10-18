from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import time

from wifit3.crack.hc22000_format import eapol_hashlines, parse_hc22000, pmkid_hashline
from wifit3.dot11.parser import WlanFrameParser
from wifit3.models import AccessPoint, CaptureType
from wifit3.persist.capture_history import load_capture_index
from wifit3.persist.common import bssid_to_dashed, safe_ssid
from wifit3.persist.config import Config
from wifit3.persist.metadata import save_ap_metadata
from wifit3.persist.pcap import read_pcap
from wifit3.wlan.sink import WlanSink


@dataclass(frozen=True)
class RepairResult:
    aps: int
    hash_lines: int
    pcaps_read: int
    files_renamed: int
    errors: tuple[str, ...]
    backup_dir: Path | None


def _colon_mac(compact: str) -> str:
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


def _decoded_ssid(encoded: str) -> str | None:
    try:
        value = bytes.fromhex(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return value or None


def repair_vault() -> RepairResult:
    """Rebuild derived HC22000 files, metadata, and pcap names without touching credentials."""
    root = Path(Config.captures_dir)
    groups: dict[str, tuple[str, set[str], AccessPoint | None]] = {}
    parsed_hc_paths: set[Path] = set()
    errors: list[str] = []
    pcaps_read = 0
    renamed = 0
    source_paths = [*root.glob("*.hc22000"), *root.glob("*.pcap")]
    backup_dir: Path | None = None
    if source_paths:
        backup_dir = root / f"v1-backup-{int(time.time())}"
        counter = 1
        while backup_dir.exists():
            backup_dir = root / f"v1-backup-{int(time.time())}-{counter}"
            counter += 1
        backup_dir.mkdir(parents=True)
        for source in source_paths:
            try:
                shutil.copy2(source, backup_dir / source.name)
            except OSError as exc:
                errors.append(f"Could not preserve {source.name}: {exc}")
        if errors:
            return RepairResult(0, 0, 0, 0, tuple(errors), backup_dir)

    for path in root.glob("*.hc22000"):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        accepted = False
        for line in lines:
            entry = parse_hc22000(line)
            if entry is None or entry.kind not in ("01", "02"):
                continue
            bssid = _colon_mac(entry.mac_ap)
            ssid = _decoded_ssid(entry.essid)
            if ssid is None:
                continue
            current_ssid, stored_lines, observed = groups.get(bssid, (ssid, set(), None))
            stored_lines.add(line.strip())
            groups[bssid] = (ssid or current_ssid, stored_lines, observed)
            accepted = True
        if accepted:
            parsed_hc_paths.add(path)

    for path in root.glob("*.pcap"):
        sink = WlanSink()
        try:
            for frame, timestamp in read_pcap(path):
                packet = WlanFrameParser.parse_80211_frame(frame, 0)
                if packet is not None:
                    sink.update(packet, "vault-repair")
                    if packet.type == "eapol" and packet.client_mac:
                        ap = sink.access_points.get(packet.bssid)
                        if ap is not None:
                            handshake = ap.handshakes.get(packet.client_mac)
                            if handshake and handshake.messages:
                                handshake.messages[-1].timestamp = timestamp
        except (OSError, ValueError) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        pcaps_read += 1
        for bssid, ap in sink.access_points.items():
            if not ap.ssid or ap.ssid == "<hidden>":
                continue
            current_ssid, lines, _observed = groups.get(bssid, (ap.ssid, set(), None))
            for handshake in ap.handshakes.values():
                lines.update(eapol_hashlines(ap.ssid, handshake))
                pmkid = pmkid_hashline(ap.ssid, handshake)
                if pmkid:
                    lines.add(pmkid)
            groups[bssid] = (ap.ssid or current_ssid, lines, ap)

            name = path.name
            marker = f"_{bssid_to_dashed(bssid)}_"
            if marker in name:
                suffix = name.split(marker, 1)[1]
                destination = root / f"{safe_ssid(ap.ssid)}{marker}{suffix}"
                if destination != path and not destination.exists():
                    path.replace(destination)
                    renamed += 1

    capture_index = load_capture_index()
    for bssid, captures in capture_index.items():
        if bssid not in groups:
            ssid = next((capture.ssid for capture in captures if capture.ssid), None)
            groups[bssid] = (ssid or "", set(), None)

    targets: set[Path] = set()
    for bssid, (ssid, lines, observed) in groups.items():
        ap = observed or AccessPoint(bssid=bssid, ssid=ssid)
        save_ap_metadata(ap, include_observed_data=observed is not None)
        if not lines:
            continue
        target = root / f"{safe_ssid(ssid)}_{bssid_to_dashed(bssid)}.hc22000"
        target.write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8")
        targets.add(target)

    for bssid, captures in capture_index.items():
        ssid = next((capture.ssid for capture in captures if capture.ssid), None)
        ap = AccessPoint(bssid=bssid, ssid=ssid)
        for capture in captures:
            psk = capture.value if capture.type in (
                CaptureType.WPA_PSK, CaptureType.WPS_PIN, CaptureType.WPS_PBC
            ) else None
            pin = capture.pin if capture.type == CaptureType.WPS_PIN else None
            wep_key = capture.value if capture.type == CaptureType.WEP else None
            if psk or pin or wep_key:
                save_ap_metadata(
                    ap,
                    psk=psk,
                    wps_pin=pin,
                    wep_key=wep_key,
                    include_observed_data=False,
                )

    for path in parsed_hc_paths - targets:
        try:
            path.unlink()
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")

    return RepairResult(
        aps=len(groups),
        hash_lines=sum(len(lines) for _ssid, lines, _ap in groups.values()),
        pcaps_read=pcaps_read,
        files_renamed=renamed,
        errors=tuple(errors),
        backup_dir=backup_dir,
    )
