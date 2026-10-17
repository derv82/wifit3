"""Shared persistence helpers: filename parsing and regexes."""
from __future__ import annotations

import re
from typing import Optional

_SSID_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
_SSID_MAX = 32

LEGACY_CAPTURE_RE = re.compile(
    r"^(?P<ssid>.+)_"
    r"(?P<bssid>[0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})_"
    r"(?P<epoch>\d+)_"
    r"(?P<kind>handshake|pmkid|wep_key|wps_pin|wps_pbc|wpa_psk)"
    r"\.(?P<ext>pcap|hc22000|txt)$"
)

AGGREGATED_HC22000_RE = re.compile(
    r"^(?P<ssid>.+)_"
    r"(?P<bssid>[0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})"
    r"\.hc22000$"
)

WEP_KEY_HEX_RE = re.compile(r"WEP key \(hex\):\s*([0-9a-fA-F]+)")
WPS_PSK_RE = re.compile(r"^PSK:\s*(.+)$", re.MULTILINE)
WPS_PIN_RE = re.compile(r"^PIN:[ \t]*(.*)$", re.MULTILINE)


def parse_wps_pin(text: str) -> Optional[str]:
    """Read a WPS PIN value, translating the legacy empty-PIN display marker."""
    match = WPS_PIN_RE.search(text)
    if match is None:
        return None
    pin = match.group(1).strip()
    return "" if pin == "<empty>" else pin


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
