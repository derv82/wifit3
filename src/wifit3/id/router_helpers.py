from __future__ import annotations

from .vendors import VENDOR_BY_OUI
from wifit3.models.identity import canonical_vendor

_PREFIX_LENGTHS = (9, 7, 6)


def hex_mac(mac: str) -> str:
    return mac.replace(":", "").replace("-", "").upper()


def vendor_for_mac(mac: str) -> str | None:
    oui = hex_mac(mac)
    vendor = next((VENDOR_BY_OUI[oui[:n]] for n in _PREFIX_LENGTHS if oui[:n] in VENDOR_BY_OUI), None)
    return canonical_vendor(vendor)
