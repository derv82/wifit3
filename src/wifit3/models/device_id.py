"""``DeviceID``: the VID:PID a driver claims (a leaf dataclass, used across the whole app)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

_SILICON = {"RTL": "Realtek", "MT": "MediaTek", "RT": "Ralink", "AR": "Atheros"}  # RTL before RT


@dataclass(frozen=True)
class DeviceID:
    """One VID:PID a driver claims. ``chipset`` is the bare silicon (e.g. ``"RTL8812AU"``);
    ``vendor`` is the retail brand when the VID:PID names one unambiguously (else None, pending an
    OUI read); ``product_name`` is the bare model, with the brand inlined when ``vendor`` is None.
    ``extras`` carries driver-specific construction hints."""
    vid: int
    pid: int
    chipset: str
    vendor: Optional[str] = None
    product_name: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)
    bus: Optional[int] = None       # None on the static SUPPORTED_IDS catalog entries;
    address: Optional[int] = None   # set on the live devices find_devices() returns

    @property
    def instance_key(self) -> tuple:
        """Identity of one physical card on the bus. ``address`` is assigned per-bus, so two identical
        models on different host controllers can share it; ``bus`` and ``address`` together are unique.
        vid/pid ride along so a catalog entry (bus/address None) never collides with a live instance."""
        return (self.vid, self.pid, self.bus, self.address)

    @property
    def silicon_vendor(self) -> str:
        """The chip maker, derived from the chipset prefix (Realtek/MediaTek/Ralink/Atheros)."""
        return next(v for p, v in _SILICON.items() if self.chipset.startswith(p))

    @property
    def description(self) -> str:
        """Human label ``"CHIPSET (brand model)"`` (or just ``"CHIPSET"``). Back-compat shim for
        the call sites that render a device as one string."""
        brand = " ".join(x for x in (self.vendor, self.product_name) if x)
        return f"{self.chipset} ({brand})" if brand else self.chipset
