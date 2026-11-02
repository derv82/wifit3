"""VerifyPinCampaign: associate with the WPS IE and run one registrar session with the PIN."""
from __future__ import annotations

from typing import Optional

from wifit3.campaigns.auth_assoc import Association, WlanTransport
from wifit3.campaigns.pin import EMPTY_PIN_LABEL
from wifit3.campaigns.wps.registrar import PinResult, WpsRegistrar
from wifit3.dot11 import str_to_mac
from wifit3.dot11.wsc.assoc_ie import WPS_REQ_REGISTRAR, wps_assoc_ie

from .campaign import VerifyCampaign, VerifyOutcome


class VerifyPinCampaign(VerifyCampaign):
    credential_name = "WPS PIN"

    async def _exchange(self, iface, our_mac: bytes, our_mac_str: str) -> tuple[VerifyOutcome, Optional[str]]:
        ap = self.target
        bssid_mac = str_to_mac(ap.bssid)
        wsc_2_0 = ap.wps_version == "2.0"
        association = Association(iface, ap.bssid, ap.ssid or "", ap.channel, our_mac=our_mac,
                                  assoc_trailer_ies=wps_assoc_ie(WPS_REQ_REGISTRAR, wsc_2_0),
                                  should_stop=lambda: self.stopped)
        association.start()
        transport = WlanTransport(iface, bssid_mac, our_mac)
        transport.start()
        try:
            self._log_step("auth/assoc sent")
            if not await association.associate(attempts=2):
                return VerifyOutcome.ERROR, association.fail_reason or "association rejected"
            self._log_step(f"EAPOL-Start sent, trying PIN {self.value or EMPTY_PIN_LABEL}")
            registrar = WpsRegistrar(transport, bssid_mac, our_mac, channel=ap.channel, wsc_2_0=wsc_2_0,
                                     should_stop=lambda: self.stopped)
            attempt = await registrar.try_pin(self.value)
        finally:
            association.stop()
            transport.stop()
        if attempt.result == PinResult.SUCCESS:
            self._log_step("M7 received")
            return VerifyOutcome.VERIFIED, None
        if attempt.result in (PinResult.FIRST_HALF_WRONG, PinResult.SECOND_HALF_WRONG):
            return VerifyOutcome.REJECTED, None
        return VerifyOutcome.ERROR, attempt.detail or attempt.result.value
