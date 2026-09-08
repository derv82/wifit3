"""Device and router identity resolution and fingerprinting."""
from __future__ import annotations

from .client import Fingerprint, fingerprint, fingerprint_client
from .router import fingerprint_router
from .router_types import RouterClaim, RouterEvidence, RouterFingerprint
from .vendors import VENDOR_BY_OUI

__all__ = [
    "Fingerprint",
    "RouterClaim",
    "RouterEvidence",
    "RouterFingerprint",
    "VENDOR_BY_OUI",
    "fingerprint",
    "fingerprint_client",
    "fingerprint_router",
]
