from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from wifit3.campaigns.auth_assoc import Association, WlanTransport, build_client_leaving
from wifit3.campaigns.probe.base import BaseApProbe, ProbeResult
from wifit3.dot11 import str_to_mac
from wifit3.dot11.wsc import messages as M
from wifit3.dot11.wsc.assoc_ie import WPS_REQ_REGISTRAR, wps_assoc_ie
from wifit3.dot11.wsc.identity import WpsM1Identity, identity_from_attrs
from wifit3.models import IdKey, IdSource

if TYPE_CHECKING:
    from wifit3.models import AccessPoint
    from wifit3.wlan.interface import WlanInterface


async def _trigger_m1(
    transport: WlanTransport,
    bssid: bytes,
    our_mac: bytes,
    tries: int = 10,
    timeout: float = 3.0,
) -> Optional[WpsM1Identity]:
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
            return identity_from_attrs(parsed.attrs)
    return None


class WpsM1Probe(BaseApProbe):
    """Probes an AP for WPS M1 identity attributes via Open Association."""
    name = "wps_m1"

    def can_probe(self, ap: AccessPoint) -> bool:
        return bool(ap.wps)

    async def probe(self, iface: WlanInterface, ap: AccessPoint) -> ProbeResult:
        bssid_bytes = str_to_mac(ap.bssid.lower())
        await iface.set_channel(ap.channel)
        fake_mac = await iface.set_fake_mac(None, bssid_bytes)
        our_mac_str = fake_mac or (iface.mac_address if isinstance(iface.mac_address, str) else None)
        if our_mac_str is None:
            return ProbeResult(False, detail="active monitor unavailable")
        our_mac = str_to_mac(our_mac_str)

        assoc = Association(
            iface, ap.bssid.lower(), ap.ssid or "", ap.channel, our_mac=our_mac,
            assoc_trailer_ies=wps_assoc_ie(WPS_REQ_REGISTRAR),
        )
        transport = WlanTransport(iface, bssid_bytes, our_mac)
        assoc.start()
        try:
            if not await assoc.associate():
                return ProbeResult(False, detail=assoc.fail_reason or "no association response")
            transport.start()
            identity = await _trigger_m1(transport, bssid_bytes, our_mac)
        finally:
            transport.stop()
            assoc.stop()
            try:
                await iface.send_no_wait(build_client_leaving(bssid_bytes, our_mac))
            except Exception:
                pass
            await iface.clear_fake_mac()

        if identity is None or not identity.present:
            return ProbeResult(False, detail="no WPS M1 response")

        ap.identity.set(IdSource.WSC_M1, IdKey.MANUFACTURER, identity.manufacturer)
        ap.identity.set(IdSource.WSC_M1, IdKey.MODEL_NAME, identity.model_name)
        ap.identity.set(IdSource.WSC_M1, IdKey.MODEL_NUMBER, identity.model_number)
        ap.identity.set(IdSource.WSC_M1, IdKey.DEVICE_NAME, identity.device_name)
        return ProbeResult(True, source="wps.m1", wps_identity=identity)
