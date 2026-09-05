from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from wifit3.campaigns.mikrotik_probe import probe_mikrotik
from wifit3.campaigns.wps.m1_probe import WpsM1Identity, probe_wps_m1
from wifit3.models import AccessPoint
from wifit3.wlan.router_fingerprint import RouterClaim


@dataclass(frozen=True)
class RouterProbeResult:
    ok: bool
    source: str = ""
    detail: str = ""
    wps_identity: Optional[WpsM1Identity] = None
    claims: tuple[RouterClaim, ...] = ()


async def probe_router_info(array, ap: AccessPoint, iface=None) -> RouterProbeResult:
    failures = []
    if ap.wps:
        result = await probe_wps_m1(array, ap, iface=iface)
        if result.ok:
            return RouterProbeResult(ok=True, source="wps.m1", wps_identity=result.identity)
        failures.append(f"WPS M1: {result.detail}")

    result = await probe_mikrotik(array, ap, iface=iface)
    if result.ok:
        return RouterProbeResult(ok=True, source="mikrotik.winbox", claims=result.claims)
    failures.append(f"MikroTik WinBox: {result.detail}")
    return RouterProbeResult(False, detail="; ".join(failures))
