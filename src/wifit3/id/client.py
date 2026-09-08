"""Offline vendor lookup for a MAC address OUI. Categorizes/emojifies known vendors."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .vendors import VENDOR_BY_OUI


@dataclass(frozen=True)
class Fingerprint:
    emoji: str
    label: str


@dataclass(frozen=True)
class Rule:
    emoji: str
    pattern: Optional[str] = None
    ouis: Optional[frozenset] = None
    label: Optional[str] = None


def _ouis(macs_text: str) -> frozenset:
    """Convert space (or line) -delimited OUIs to a set of hexdigits.
    ```
    _ouis("aa:bb:cc 01:23:45 aa:bb:cc") -> frozenset(["aabbcc", "01:23:45"])
    ```
    """
    return frozenset(entry.replace(":", "").replace("-", "").lower() for entry in macs_text.split())


_RING_OUIS = _ouis("""
    00:B4:63 18:7F:88 24:2B:D6 34:3E:A4 50:E4:67 54:E0:19 5C:47:5E 64:9A:63
    90:48:6C 9C:76:13 AC:9F:C3 C4:DB:AD CC:3B:FB
""")

_ROKU_OUIS = _ouis("""
    00:0D:4B 08:05:81 10:59:32 20:EF:BD 34:5E:08 50:06:F5 54:4E:F0 60:92:C8
    7C:67:AB 84:EA:ED 88:DE:A9 8A:C7:2E 8C:49:62 9C:F1:D4 A8:B5:7C AC:3A:7A
    AC:AE:19 B0:A7:37 B0:EE:7B B8:3E:59 B8:A1:75 BC:D7:D4 C8:3A:6B CC:6D:A0
    D0:4D:2C D4:BE:DC D4:E2:2F D8:31:34 DC:3A:5E EC:9B:75 F8:B2:2C
""")

_SONOS_OUIS = _ouis("""
    00:0E:58 34:7E:5C 38:42:0B 48:A6:B8 54:2A:1B 5C:AA:FD 60:F6:20 74:CA:60
    78:28:CA 80:4A:F2 94:9F:3E B8:E9:37 C4:38:75 EA:BE:A7 F0:F6:C1 F8:5C:24
""")

# Nest Labs' own legacy blocks, registered before the Google acquisition.
_NEST_OUIS = _ouis("18:B4:30 64:16:66")

_IROBOT_OUIS = _ouis("4C:B9:EA 50:14:79 AC:F4:73")

# DC:44:27:1 is a OUI-28: The first 24-bits are shared across 16 vendors (Tesla=:1x nibble)
_TESLA_OUIS = _ouis("0C:29:8F 4C:FC:AA 54:F8:F0 90:E6:43 98:ED:5C D4:4F:14 DC:44:27:1")


# No rule matched (404 emoji not found).
_GENERIC_EMOJI = " "

_RULES: tuple[Rule, ...] = (
    # Brand OUIs
    Rule("📦", pattern=r"\bamazon\b"),
    Rule("🍎", pattern=r"\bapple\b"),
    Rule("🤖", pattern=r"\bgoogle\b"),
    Rule("🪟", pattern=r"\bmicrosoft\b"),
    Rule("🍓", pattern=r"\braspberry pi\b"),
    Rule("🔵", pattern=r"\bsamsung\b"),
    # Audio/Speakers
    Rule("🔊", ouis=_SONOS_OUIS, label="Sonos speaker"),
    Rule("🔊", pattern=r"^bose$", label="Bose speaker"),  # Headsets are BT, not WiFi
    Rule("🔊", pattern=r"\bsennheiser\b", label="Sennheiser speaker"),
    # Gaming consoles
    Rule("🎮", pattern=r"\bSony Interactive Entertainment\b", label="PlayStation"),
    Rule("🎮", pattern=r"\bNintendo\b", label="Nintendo console"),
    Rule("🥽", pattern=r"\bOculus\b", label="Oculus VR"),
    # Home IoTs
    Rule("🔔", ouis=_RING_OUIS),
    Rule("🧹", ouis=_IROBOT_OUIS, label="iRobot vacuum"),
    Rule("🏠", ouis=_NEST_OUIS, label="Nest device"),
    Rule("🏠", pattern=r"\b(tuya|Smart Innovation)\b"),
    # Lights
    Rule("💡", pattern=r"\b(GE Lighting|signify|lutron)\b"),
    # Phones
    Rule("📱", pattern=r"\b(Xiaomi Mobile)\b", label="Xiaomi phone"),
    Rule("📱", pattern=r"\b(Motorola Mobility)\b", label="Motorola phone"),
    # Printers
    Rule("📑", pattern=r"(\bhewlett packard\b|^hp [^t]|^hp$)", label="HP printer"),
    Rule("📑", pattern=r"\bcanon\b", label="Canon printer"),
    Rule("📑", pattern=r"\bbrother\b", label="Brother printer"),
    Rule("📑", pattern=r"^seiko epson", label="Epson printer"),
    # Networking/Routers
    Rule("🛜", pattern=r"\bComcast\b", label="Comcast router"),
    Rule("🛜", pattern=r"\bVantiva\b", label="Vantiva router"),
    Rule("🛜", pattern=r"\bASUSTek\b", label="ASUS router"),
    Rule("🛜", pattern=r"\b(CommScope|arris)\b", label="ARRIS router"),  # CommScope acquired ARRIS
    Rule("🛜", pattern=r"\b(arcadyan|ubiquiti|tp-link|netgear|linksys|aruba|eero)\b"),
    # Security/locks
    Rule("🔒", pattern=r"\bassa abloy\b", label="Assa Abloy device"),
    Rule("📹", pattern=r"\bwyze labs\b", label="Wyze device"),
    Rule("🚨", pattern=r"\bsimplisafe\b", label="SimpliSafe device"),
    # TVs
    Rule("📺", ouis=_ROKU_OUIS, label="Roku"),
    Rule("📺", pattern=r"\bvizio\b", label="Vizio TV"),
    # Misc
    Rule("🚘", ouis=_TESLA_OUIS, label="Tesla vehicle"),
    Rule("⌚", pattern=r"\b(garmin|fitbit)\b"),
    Rule("🚲", pattern=r"\bpeloton\b", label="Peleton bike"),
    Rule("🌄", pattern=r"\baura home\b", label="Aura photo frame"),
)

# IEEE OUIs: MA-L (36b), MA-M (32b), MA-S (28b); ordered by decreasing granularity.
_PREFIX_LENGTHS = (9, 7, 6)


def _longest_prefix_match(hex_mac: str, table: dict[str, str]) -> Optional[str]:
    return next((v for length in _PREFIX_LENGTHS
                 if (v := table.get(hex_mac[:length].upper())) is not None), None)


def _rule_matches(rule: Rule, hex_mac: str, vendor: Optional[str]) -> bool:
    if rule.ouis is not None and any(hex_mac[:n] in rule.ouis for n in _PREFIX_LENGTHS):
        return True
    return (rule.pattern is not None and vendor is not None
            and re.search(rule.pattern, vendor, re.I) is not None)


def fingerprint(mac: str) -> Optional[Fingerprint]:
    """The device vendor for the ``mac``'s OUI, or None if missing from ``VENDOR_BY_OUI``."""
    hex_mac = mac.replace(":", "").replace("-", "").lower()
    vendor = _longest_prefix_match(hex_mac, VENDOR_BY_OUI)
    for rule in _RULES:
        if _rule_matches(rule, hex_mac, vendor):
            return Fingerprint(rule.emoji, rule.label or f"{vendor} device")
    if vendor is None:
        return None
    return Fingerprint(_GENERIC_EMOJI, f"{vendor} device")


fingerprint_client = fingerprint
