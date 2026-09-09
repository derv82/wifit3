from __future__ import annotations

from typing import TYPE_CHECKING

from wifit3.campaigns.probe.base import BaseApProbe, ProbeResult
from wifit3.campaigns.probe.wps_m1 import WpsM1Probe

if TYPE_CHECKING:
    from wifit3.models import AccessPoint
    from wifit3.wlan.interface import WlanInterface

DEFAULT_PROBES: tuple[BaseApProbe, ...] = (
    WpsM1Probe(),
)


async def probe_ap(
    iface: WlanInterface,
    ap: AccessPoint,
    probes: tuple[BaseApProbe, ...] = DEFAULT_PROBES,
) -> ProbeResult:
    """Run applicable active identity probes against an AP on the provided interface."""
    applicable = [p for p in probes if p.can_probe(ap)]
    if not applicable:
        return ProbeResult(ok=False, detail="no suitable probes for network type")

    failures: list[str] = []
    for probe in applicable:
        result = await probe.probe(iface, ap)
        if result.ok:
            return result
        failures.append(f"{probe.name}: {result.detail}")

    return ProbeResult(ok=False, detail="; ".join(failures))
