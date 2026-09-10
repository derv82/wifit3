from __future__ import annotations

from wifit3.models.identity import canonical_vendor
from .vendors import VENDOR_BY_OUI

_PREFIX_LENGTHS = (9, 7, 6)


def hex_mac(mac: str) -> str:
    """Normalize MAC address string by stripping colons/hyphens and uppercasing."""
    return mac.replace(":", "").replace("-", "").upper()


def lookup_oui(mac: str) -> str | None:
    """Return raw vendor name from IEEE OUI registry, or None if unknown."""
    oui = hex_mac(mac)
    return next((VENDOR_BY_OUI[oui[:n]] for n in _PREFIX_LENGTHS if oui[:n] in VENDOR_BY_OUI), None)


def vendor_for_mac(mac: str) -> str | None:
    """Return canonical vendor name for a MAC address, or None if unknown."""
    return canonical_vendor(lookup_oui(mac))
