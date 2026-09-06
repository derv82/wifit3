from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from wifit3.campaigns.mikrotik_probe import probe_mikrotik
from wifit3.campaigns.ubiquiti_probe import probe_ubnt
from wifit3.campaigns.wps.m1_probe import probe_wps_m1
from wifit3.dot11.wsc.identity import WpsM1Identity
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
            return RouterProbeResult(ok=True, source="wps.m1", wps_identity=_ap_wps_identity(ap))
        failures.append(f"WPS M1: {result.detail}")

    result = await probe_mikrotik(array, ap, iface=iface)
    if result.ok:
        return RouterProbeResult(ok=True, source=result.source, claims=result.claims)
    failures.append(f"MikroTik WinBox: {result.detail}")

    result = await probe_ubnt(array, ap, iface=iface)
    if result.ok:
        return RouterProbeResult(ok=True, source="ubnt.discovery", claims=result.claims)
    failures.append(f"UBNT discovery: {result.detail}")
    return RouterProbeResult(False, detail="; ".join(failures))


def _ap_wps_identity(ap: AccessPoint) -> WpsM1Identity:
    return WpsM1Identity(
        manufacturer=ap.wps_manufacturer,
        model_name=ap.wps_model_name,
        model_number=ap.wps_model_number,
        device_name=ap.wps_device_name,
    )
