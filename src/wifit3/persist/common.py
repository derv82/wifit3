"""Shared persistence helpers: filename parsing, regexes, and Hashcat 22000 lines."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_SSID_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
_SSID_MAX = 32

LEGACY_CAPTURE_RE = re.compile(
    r"^(?P<ssid>.+)_"
    r"(?P<bssid>[0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})_"
    r"(?P<epoch>\d+)_"
    r"(?P<kind>handshake|pmkid|wep_key|wps_pin|wps_pbc)"
    r"\.(?P<ext>pcap|hc22000|txt)$"
)

AGGREGATED_HC22000_RE = re.compile(
    r"^(?P<ssid>.+)_"
    r"(?P<bssid>[0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})"
    r"\.hc22000$"
)

WEP_KEY_HEX_RE = re.compile(r"WEP key \(hex\):\s*([0-9a-fA-F]+)")
WPS_PSK_RE = re.compile(r"^PSK:\s*(.+)$", re.MULTILINE)
WPS_PIN_RE = re.compile(r"^PIN:\s*(.+)$", re.MULTILINE)


def safe_ssid(ssid: Optional[str]) -> str:
    """Sanitize an SSID for filesystem path safety."""
    base = _SSID_SAFE_RE.sub("_", ssid or "")[:_SSID_MAX]
    return base or "hidden"


def bssid_to_dashed(bssid: str) -> str:
    """``aa:bb:cc:dd:ee:ff`` -> ``aa-bb-cc-dd-ee-ff``."""
    return bssid.replace(":", "-").lower()


def bssid_to_colon(dashed: str) -> str:
    """``aa-bb-cc-dd-ee-ff`` -> ``aa:bb:cc:dd:ee:ff``."""
    return dashed.replace("-", ":").lower()


@dataclass(frozen=True, slots=True)
class Hc22000Line:
    """Parsed Hashcat mode 22000 record (PMKID or EAPOL handshake)."""
    kind: str           # "01" (PMKID) or "02" (EAPOL)
    pmkid_or_mic: str   # field 2: PMKID hex (if 01) or MIC (if 02)
    mac_ap: str         # field 3
    mac_sta: str        # field 4
    essid: str          # field 5
    anonce: str = ""    # field 6 (EAPOL only)
    eapol: str = ""     # field 7 (EAPOL only)
    message_pair: str = ""  # field 8 (EAPOL only)


def parse_hc22000(line: str) -> Optional[Hc22000Line]:
    """Parse one ``WPA*01*…`` or ``WPA*02*…`` line into an Hc22000Line."""
    parts = line.strip().split("*")
    if len(parts) < 6 or parts[0] != "WPA":
        return None
    return Hc22000Line(
        kind=parts[1],
        pmkid_or_mic=parts[2].lower(),
        mac_ap=parts[3].lower(),
        mac_sta=parts[4].lower(),
        essid=parts[5],
        anonce=parts[6].lower() if len(parts) > 6 else "",
        eapol=parts[7] if len(parts) > 7 else "",
        message_pair=parts[8] if len(parts) > 8 else "",
    )
