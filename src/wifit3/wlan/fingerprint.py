"""Coarse client device-class fingerprinting from OUI prefix alone: no network lookup (an
audited network is exactly the wrong place to make outbound calls to a third-party MAC-vendor
API -- it leaks who's being audited, and plenty of engagements are on networks with no internet
access at all), just two local, offline tables.

Two confidence tiers:
- **high**: a small hand-curated table (real IEEE OUI assignments, maclookup.app, 2026-08) for
  vendors whose product line is a single device class end to end (Ring is always a
  doorbell/camera, PlayStation is always a console), so the OUI alone names the actual thing.
- **low**: ``fingerprint_vendors.py``, GENERATED from Wireshark's weekly-updated ``manuf`` feed
  (every registered vendor, ~58k entries, including IEEE's finer-grained 28-/36-bit
  sub-allocations -- see scripts/generators/gen_fingerprint_vendors.py), since a vendor whose OUI
  blocks span multiple device classes (Apple/Google/Samsung/Microsoft/... all sell phones,
  laptops, and smart-home hardware off the same blocks) can only ever be named, not classified. A
  handful of well-known giants get a recognizable icon by name match; everyone else gets a
  generic tag. Disambiguating a low-confidence vendor into an actual device class needs IE
  fingerprinting (probe/assoc request tag sequences), not attempted here; see
  docs/planning/FEATURES.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

from .fingerprint_vendors import VENDOR_BY_OUI

Confidence = Literal["high", "low"]


@dataclass(frozen=True)
class Fingerprint:
    emoji: str
    label: str
    confidence: Confidence = "high"


def _ouis(text: str) -> frozenset:
    """Each entry's own hex length is kept (not forced to 6): a longer one names an IEEE
    28-/36-bit sub-allocation precisely, not a plain 24-bit OUI -- see _TESLA below."""
    return frozenset(entry.replace(":", "").replace("-", "").lower() for entry in text.split())


# ----- high confidence: single device class end to end -----------------------

_RING = _ouis("""
    00:B4:63 18:7F:88 24:2B:D6 34:3E:A4 50:E4:67 54:E0:19 5C:47:5E 64:9A:63
    90:48:6C 9C:76:13 AC:9F:C3 C4:DB:AD CC:3B:FB
""")

_ROKU = _ouis("""
    00:0D:4B 08:05:81 10:59:32 20:EF:BD 34:5E:08 50:06:F5 54:4E:F0 60:92:C8
    7C:67:AB 84:EA:ED 88:DE:A9 8A:C7:2E 8C:49:62 9C:F1:D4 A8:B5:7C AC:3A:7A
    AC:AE:19 B0:A7:37 B0:EE:7B B8:3E:59 B8:A1:75 BC:D7:D4 C8:3A:6B CC:6D:A0
    D0:4D:2C D4:BE:DC D4:E2:2F D8:31:34 DC:3A:5E EC:9B:75 F8:B2:2C
""")

_SONOS = _ouis("""
    00:0E:58 34:7E:5C 38:42:0B 48:A6:B8 54:2A:1B 5C:AA:FD 60:F6:20 74:CA:60
    78:28:CA 80:4A:F2 94:9F:3E B8:E9:37 C4:38:75 EA:BE:A7 F0:F6:C1 F8:5C:24
""")

_NINTENDO = _ouis("""
    00:09:BF 00:16:56 00:17:AB 00:19:1D 00:19:FD 00:1A:E9 00:1B:7A 00:1B:EA
    00:1C:BE 00:1D:BC 00:1E:35 00:1E:A9 00:1F:32 00:1F:C5 00:21:47 00:21:BD
    00:22:4C 00:22:AA 00:22:D7 00:23:31 00:23:CC 00:24:1E 00:24:44 00:24:F3
    00:25:A0 00:26:59 00:27:09 04:03:D6 18:2A:7B 1C:45:86 20:0B:CF 20:1C:3A
    28:CF:51 2C:10:C1 30:89:EC 34:2F:BD 34:AF:2C 38:70:35 38:C6:CE 3C:A9:AB
    40:44:F7 40:D2:8A 40:F4:07 48:31:77 48:A5:E7 48:F1:EB 4C:30:6A 50:23:6D
    58:2F:40 58:B0:3E 58:BD:A3 5C:0C:E6 5C:52:1E 60:1A:C7 60:6B:FF 64:B5:C6
    70:2C:09 70:48:F7 70:F0:88 74:84:69 74:F9:CA 78:20:A5 78:81:8C 78:A2:A0
    7C:BB:8A 80:D2:E5 84:C0:65 8C:56:C5 8C:CD:E8 90:45:28 94:58:CB 94:8E:6D
    98:41:5C 98:B6:E9 98:E2:55 98:E8:FA 9C:E6:35 A4:38:CC A4:5C:27 A4:C0:E1
    A4:C1:E8 AC:FA:E4 B8:68:70 B8:78:26 B8:8A:EC B8:AE:6E BC:74:4B BC:89:A6
    BC:9E:BB BC:CE:25 C0:A4:CF C8:48:05 C8:91:43 CC:5B:31 CC:9E:00 CC:FB:65
    D0:55:09 D4:F0:57 D8:6B:83 D8:6B:F7 DC:68:EB DC:CD:18 E0:0C:7F E0:E7:51
    E0:EF:BF E0:F6:B5 E8:4E:CE E8:A0:CD E8:DA:20 EC:C4:0D
""")

_PLAYSTATION = _ouis("""
    00:04:1F 00:13:15 00:15:C1 00:19:C5 00:1D:0D 00:1F:A7 00:24:8D 00:D9:D1
    00:E4:21 04:F7:78 0C:70:43 0C:FE:45 28:0D:FC 28:40:DD 2C:9E:00 2C:CC:44
    50:B0:3B 54:E6:FD 5C:84:3C 5C:96:66 68:28:6C 70:66:2A 70:9E:29 78:C8:81
    84:E6:57 90:47:48 98:FA:2E 9C:37:CB A8:E3:EE B4:0A:D8 B4:1F:4D BC:33:29
    BC:60:A7 C0:15:1B C8:4A:A0 C8:63:F1 D4:F7:D5 E8:6E:3A EC:74:8C F4:64:12
    F8:46:1C F8:D0:AC FC:0F:E6 FC:CA:40
""")

# Nest Labs' own legacy blocks, registered before the Google acquisition -- distinct from
# Google's own registered blocks (which also carry Nest's newer hardware, resolved at low
# confidence below since Google's blocks aren't Nest-exclusive).
_NEST = _ouis("18:B4:30 64:16:66")

_IROBOT = _ouis("4C:B9:EA 50:14:79 AC:F4:73")

# DC:44:27:1 is a 28-bit (OUI-28) entry, not a full 24-bit OUI: that 24-bit block is actually
# split between 16 organizations, Tesla owning only the :1x nibble (fingerprint_vendors.py's
# generator resolves this precisely from Wireshark's manuf feed; see its module docstring).
_TESLA = _ouis("0C:29:8F 4C:FC:AA 54:F8:F0 90:E6:43 98:ED:5C D4:4F:14 DC:44:27:1")

_HIGH_CONFIDENCE: tuple[tuple[frozenset, Fingerprint], ...] = (
    (_RING, Fingerprint("🔔", "Ring device")),
    (_ROKU, Fingerprint("📺", "Roku")),
    (_SONOS, Fingerprint("🎵", "Sonos speaker")),
    (_NINTENDO, Fingerprint("🍄", "Nintendo console")),
    (_PLAYSTATION, Fingerprint("🎮", "PlayStation")),
    (_NEST, Fingerprint("🌡️", "Nest device")),
    (_IROBOT, Fingerprint("🧹", "iRobot vacuum")),
    (_TESLA, Fingerprint("🚗", "Tesla vehicle")),
)

# ----- low confidence: vendor known (from the full generated registry), product line ambiguous --

# A well-known giant gets a recognizable icon by name match; everyone else gets a generic tag.
# Matched against the generated table's cleaned name, lowercased -- e.g. "Samsung Electronics".
_ICON_OVERRIDES: tuple[tuple[str, str], ...] = (
    ("apple", "🍎"),
    ("google", "🤖"),
    ("samsung", "🔵"),
    ("microsoft", "🪟"),
    ("amazon", "🛒"),
)
_GENERIC_VENDOR_ICON = "🏷️"


# IEEE only ever allocates at these three widths (MA-L/MA-M/MA-S); both tables key by hex
# nibbles (bits // 4), longest first, so a 28-/36-bit entry always wins over its containing
# 24-bit block. Add a width here (nowhere else) if IEEE ever introduces one.
_PREFIX_LENGTHS = (9, 7, 6)


def _low_confidence(hex_mac: str) -> Optional[Fingerprint]:
    vendor = next((v for length in _PREFIX_LENGTHS
                   if (v := VENDOR_BY_OUI.get(hex_mac[:length].upper())) is not None), None)
    if vendor is None:
        return None
    low = vendor.lower()
    # Word-boundary, not substring: "amazon" as a plain `in` check also matched "...da Amazonia
    # Ltda" (unrelated Brazilian companies). A plain .startswith() would fix that but break
    # "Blink by Amazon" (a real Amazon brand with the needle mid-string, not at the start).
    emoji = next((icon for needle, icon in _ICON_OVERRIDES
                 if re.search(rf"\b{needle}\b", low)), _GENERIC_VENDOR_ICON)
    return Fingerprint(emoji, f"{vendor} device", "low")


def fingerprint(mac: str) -> Optional[Fingerprint]:
    """The device-class fingerprint for ``mac``'s OUI: hand-curated high-confidence table first,
    then the generated full vendor registry at low confidence. None if the OUI isn't registered."""
    hex_mac = mac.replace(":", "").replace("-", "").lower()
    for length in _PREFIX_LENGTHS:
        prefix = hex_mac[:length]
        for ouis, fp in _HIGH_CONFIDENCE:
            if prefix in ouis:
                return fp
    return _low_confidence(hex_mac)
