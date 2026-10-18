from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wifit3.crack.hc22000_format import eapol_hashlines, parse_hc22000, pmkid_hashline
from wifit3.dot11.parser import WlanFrameParser
from wifit3.models import AccessPoint
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

    targets: set[Path] = set()
    for bssid, (ssid, lines, observed) in groups.items():
        ap = observed or AccessPoint(bssid=bssid, ssid=ssid)
        save_ap_metadata(ap, include_observed_data=observed is not None)
        if not lines:
            continue
        target = root / f"{safe_ssid(ssid)}_{bssid_to_dashed(bssid)}.hc22000"
        target.write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8")
        targets.add(target)

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
    )
