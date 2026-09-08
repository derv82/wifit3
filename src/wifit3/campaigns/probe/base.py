from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from wifit3.dot11.wsc.identity import WpsM1Identity
    from wifit3.id import RouterClaim
    from wifit3.models import AccessPoint
    from wifit3.wlan.interface import WlanInterface


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of an active AP probe exchange."""
    ok: bool
    source: str = ""
    detail: str = ""
    claims: tuple[RouterClaim, ...] = ()
    wps_identity: Optional[WpsM1Identity] = None


RouterProbeResult = ProbeResult


class BaseApProbe(ABC):
    """Abstract identity probe against an AccessPoint on a dedicated interface."""
    name: str

    @abstractmethod
    def can_probe(self, ap: AccessPoint) -> bool:
        """Predicate checking if the AP meets probe prerequisites."""

    @abstractmethod
    async def probe(self, iface: WlanInterface, ap: AccessPoint) -> ProbeResult:
        """Execute active probe exchange on iface and return result."""
