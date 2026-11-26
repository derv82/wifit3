"""Load previously-saved captures from captures/ back into per-AP history, so a
recovered key or captured handshake/PMKID re-surfaces as a badge + Focus summary
on the next scan. Classification is by filename; both .hc22000 and .pcap files are
indexed (a handshake may have either or both). Counterpart: persist.save."""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from wifit3.crack.hc22000_format import Hc22000Line, read_hc22000
from wifit3.models import PSK_CAPTURE_TYPES, CaptureType, PersistedCapture
from wifit3.persist.common import (
    AGGREGATED_HC22000_RE,
    LEGACY_CAPTURE_RE,
    WEP_KEY_HEX_RE,
    WPS_PSK_RE,
    bssid_to_colon,
    parse_wps_pin,
    safe_ssid,
)
from wifit3.persist.config import Config

logger = logging.getLogger(__name__)

_SSID_LINE_RE = re.compile(r"^SSID:[ \t]*(.*)$", re.MULTILINE)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.debug("capture_history: unreadable %s: %s", path.name, e)
        return None


def read_wep_key(text: str) -> str | None:
    """Extract the hex WEP key from a ``_wep_key.txt`` body, or None."""
    m = WEP_KEY_HEX_RE.search(text)
    return m.group(1).lower() if m else None


def read_wps_psk(text: str) -> str | None:
    """Extract the PSK from a ``_wps_pin.txt`` or ``_wps_pbc.txt`` body, or None."""
    m = WPS_PSK_RE.search(text)
    return m.group(1).strip() if m else None


def body_ssid(text: str, filename_ssid: str) -> str:
    m = _SSID_LINE_RE.search(text)
    if m and m.group(1) and safe_ssid(m.group(1)) == filename_ssid:
        return m.group(1)
    return filename_ssid


def _essid(lines: list[tuple[str, Hc22000Line]], filename_ssid: str) -> tuple[str, Optional[bytes]]:
    """The SSID naming the file, and its bytes when the first hashline's SSID names it."""
    if lines and lines[0][1].ssid and safe_ssid(lines[0][1].ssid) == filename_ssid:
        return lines[0][1].ssid, bytes.fromhex(lines[0][1].essid)
    return filename_ssid, None


def _parse_aggregate_hc(path: Path, bssid: str) -> List[PersistedCapture]:
    lines = read_hc22000(path)
    try:
        mtime = int(path.stat().st_mtime)
    except OSError:
        mtime = 0
    m = AGGREGATED_HC22000_RE.match(path.name)
    ssid, ssid_bytes = _essid(lines, m.group("ssid")) if m else (None, None)
    out: List[PersistedCapture] = []
    if any(line.is_pmkid for _, line in lines):
        out.append(PersistedCapture(type=CaptureType.PMKID, timestamp=mtime, path=str(path),
                                    bssid=bssid, ssid=ssid, ssid_bytes=ssid_bytes))
    if any(not line.is_pmkid for _, line in lines):
        out.append(PersistedCapture(type=CaptureType.HS, timestamp=mtime, path=str(path),
                                    bssid=bssid, ssid=ssid, ssid_bytes=ssid_bytes))
    return out


def _parse_file(path: Path, bssid: str) -> List[PersistedCapture]:
    """Parse one captures/ file into zero or more PersistedCapture entries."""
    if AGGREGATED_HC22000_RE.match(path.name):
        return _parse_aggregate_hc(path, bssid)

    m = LEGACY_CAPTURE_RE.match(path.name)
    if not m:
        return []
    epoch = int(m.group("epoch"))
    kind = m.group("kind")
    ext = m.group("ext")
    text = _read_text(path) or "" if ext == "txt" else ""
    lines = read_hc22000(path) if ext == "hc22000" else []
    ssid_bytes: Optional[bytes] = None
    if ext == "hc22000":
        ssid, ssid_bytes = _essid(lines, m.group("ssid"))
    elif ext == "txt":
        ssid = body_ssid(text, m.group("ssid"))
    else:
        ssid = m.group("ssid")

    if kind == "wep_key" and ext == "txt":
        key = read_wep_key(text)
        if key is None:
            return []
        return [PersistedCapture(type=CaptureType.WEP, timestamp=epoch, path=str(path),
                                 bssid=bssid, value=key, ssid=ssid)]
    if kind == "wps_pin" and ext == "txt":
        return [PersistedCapture(type=CaptureType.WPS_PIN, timestamp=epoch, path=str(path),
                                 bssid=bssid, value=read_wps_psk(text), ssid=ssid,
                                 pin=parse_wps_pin(text))]
    if kind == "wps_pbc" and ext == "txt":
        return [PersistedCapture(type=CaptureType.WPS_PBC, timestamp=epoch, path=str(path),
                                 bssid=bssid, value=read_wps_psk(text), ssid=ssid)]
    if kind == "wpa_psk" and ext == "txt":
        psk = read_wps_psk(text)
        if psk is None:
            return []
        return [PersistedCapture(type=CaptureType.WPA_PSK, timestamp=epoch, path=str(path),
                                 bssid=bssid, value=psk, ssid=ssid)]
    if kind == "handshake" and ext in ("hc22000", "pcap"):
        return [PersistedCapture(type=CaptureType.HS, timestamp=epoch, path=str(path),
                                 bssid=bssid, ssid=ssid, ssid_bytes=ssid_bytes)]
    if kind == "pmkid" and ext in ("hc22000", "pcap"):
        return [PersistedCapture(type=CaptureType.PMKID, timestamp=epoch, path=str(path),
                                 bssid=bssid, ssid=ssid, ssid_bytes=ssid_bytes)]
    return []


def load_capture_index() -> Dict[str, List[PersistedCapture]]:
    """Scan ``captures_dir``, return bssid->captures sorted by newest-first."""
    index: Dict[str, List[PersistedCapture]] = defaultdict(list)
    root = Path(Config.captures_dir)
    if not root.is_dir():
        return {}
    for path in root.iterdir():
        if not path.is_file():
            continue
        m = LEGACY_CAPTURE_RE.match(path.name)
        if m:
            bssid = bssid_to_colon(m.group("bssid"))
            index[bssid].extend(_parse_file(path, bssid))
            continue
        m_agg = AGGREGATED_HC22000_RE.match(path.name)
        if m_agg:
            bssid = bssid_to_colon(m_agg.group("bssid"))
            index[bssid].extend(_parse_file(path, bssid))
    for caps in index.values():
        caps.sort(key=lambda c: c.timestamp, reverse=True)
    return {b: c for b, c in index.items() if c}


def summarize(index: Dict[str, List[PersistedCapture]]) -> tuple[int, int, int, int]:
    """Counts of *AP*s that have (handshakes, pmkids, wep_keys, psks); max +1 per AP."""
    hs = pmkid = wep = psk = 0
    for caps in index.values():
        types = {c.type for c in caps}
        hs += CaptureType.HS in types
        pmkid += CaptureType.PMKID in types
        wep += CaptureType.WEP in types
        psk += any(c.type in PSK_CAPTURE_TYPES and c.value for c in caps)
    return hs, pmkid, wep, psk
