from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from wifit3.campaigns.auth_assoc import Association, WlanTransport, build_client_leaving
from wifit3.dot11 import str_to_mac
from wifit3.dot11.wsc import messages as M
from wifit3.dot11.wsc.assoc_ie import WPS_REQ_REGISTRAR, wps_assoc_ie
from wifit3.models import AccessPoint
from wifit3.wlan.lease import SPOOFABLE


@dataclass(frozen=True)
class WpsM1ProbeResult:
    ok: bool
    detail: str = ""
    our_mac: Optional[str] = None


async def _trigger_m1(transport: WlanTransport, bssid: bytes, our_mac: bytes,
                      tries: int = 10, timeout: float = 3.0) -> bool:
    start = M.build_data_frame(bssid, our_mac, bssid, M.eapol_start())
    await transport.send_no_wait(start)
    last = start
    for _ in range(tries):
        frame = await transport.recv(timeout)
        if frame is None:
            await transport.send_no_wait(last)
            continue
        parsed = M.parse_rx_frame(frame)
        if parsed is None:
            continue
        if parsed.is_identity_request:
            last = M.build_data_frame(bssid, our_mac, bssid, M.eap_identity_response(parsed.eap_id))
            await transport.send_no_wait(last)
        elif parsed.wsc_msg_type == M.WPS_M1:
            return True
    return False


async def probe_wps_m1(array, ap: AccessPoint, iface=None) -> WpsM1ProbeResult:
    bssid = ap.bssid.lower()
    bssid_bytes = str_to_mac(bssid)
    try:
        lease = array.lease(channel=ap.channel, fake_mac=SPOOFABLE, bssid=bssid_bytes,
                            ack_tally=True, iface=iface)
    except Exception as exc:
        return WpsM1ProbeResult(False, detail=str(exc))

    async with lease as iface:
        if lease.mac is None:
            return WpsM1ProbeResult(False, detail="active monitor unavailable")
        our_mac = str_to_mac(lease.mac)
        assoc = Association(
            iface, bssid, ap.ssid or "", ap.channel, our_mac=our_mac,
            assoc_trailer_ies=wps_assoc_ie(WPS_REQ_REGISTRAR),
        )
        transport = WlanTransport(iface, bssid_bytes, our_mac)
        assoc.start()
        try:
            if not await assoc.associate():
                return WpsM1ProbeResult(
                    False, detail=assoc.fail_reason or "no association response", our_mac=lease.mac,
                )
            transport.start()
            got_m1 = await _trigger_m1(transport, bssid_bytes, our_mac)
        finally:
            transport.stop()
            assoc.stop()
            try:
                await iface.send_no_wait(build_client_leaving(bssid_bytes, our_mac))
            except Exception:
                pass

    if not got_m1:
        return WpsM1ProbeResult(False, detail="no WPS M1 response", our_mac=lease.mac)
    await _wait_for_sink_identity(array, bssid)
    return WpsM1ProbeResult(True, our_mac=lease.mac)


async def _wait_for_sink_identity(array, bssid: str, timeout: float = 0.5) -> None:
    def has_identity() -> bool:
        ap = array.access_points.get(bssid)
        return bool(ap and (ap.wps_manufacturer or ap.wps_model_name
                            or ap.wps_model_number or ap.wps_device_name))

    wait_until = getattr(array, "wait_until", None)
    if wait_until is not None:
        await wait_until(has_identity, timeout, poll=0.02)
        return
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline and not has_identity():
        await asyncio.sleep(0.02)
