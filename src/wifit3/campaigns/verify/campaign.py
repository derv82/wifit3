"""VerifyCampaign: checks one stored credential against the live target, as a client on Focus's channel."""
from __future__ import annotations

import asyncio
from enum import Enum, auto
import logging
import time
from typing import Optional

from wifit3.campaigns.auth_assoc import build_client_leaving
from wifit3.campaigns.campaign import Campaign
from wifit3.dot11 import str_to_mac
from wifit3.models import CREDENTIAL_NAMES, CaptureType
from wifit3.wlan.lease import SPOOFABLE

logger = logging.getLogger(__name__)

_BEACON_AGE_S = 2.0        # the target counts as on the air when heard this recently
_BEACON_WAIT_S = 1.5
_LEAVING_ECHO_S = 0.5      # our MAC stays registered while the leaving deauth echoes back


class VerifyOutcome(Enum):
    VERIFIED = auto()
    REJECTED = auto()
    ERROR = auto()


class VerifyCampaign(Campaign):
    """One live check of ``value``; Focus reads ``outcome`` and ``error`` once ``done``."""

    key = "verify"
    stoppable = False
    idle_label = "Verify"
    credential_type: CaptureType

    def __init__(self, array, target, value: str, log=None):
        super().__init__(ap=target, array=array)
        self.target = target
        self.value = value
        self.log = log or logger.info
        self.step = "waiting for a beacon"
        self.outcome: Optional[VerifyOutcome] = None
        self.error: Optional[str] = None

    @classmethod
    def ineligible_reason(cls, ap) -> Optional[str]:
        if ap.is_hidden:
            return "hidden SSID: can't associate"
        return None

    def status_under_card(self) -> str:
        return "● Verify"

    def status_headlines(self, vault) -> list[str]:
        return [f"[bold cyan]● Verifying {CREDENTIAL_NAMES[self.credential_type]}[/bold cyan]", f"[dim]{self.step}[/dim]"]

    def _log_step(self, step: str) -> None:
        self.step = step
        self.log(step)

    async def _loop(self) -> None:
        bssid = self.target.bssid.lower()
        heard = await self.array.wait_until(
            lambda: time.time() - self.array.access_points.get(bssid, self.target).last_seen <= _BEACON_AGE_S,
            timeout=_BEACON_WAIT_S)
        if not heard:
            self.outcome, self.error = VerifyOutcome.ERROR, f"AP not heard on channel {self.target.channel}"
            return
        bssid_mac = str_to_mac(bssid)
        lease = self.array.lease(channel=self.target.channel, fake_mac=SPOOFABLE, bssid=bssid_mac,
                                 ack_tally=True, iface=self.iface)
        async with lease as iface:
            if lease.mac is None:
                self.outcome, self.error = VerifyOutcome.ERROR, "this card can't ACK as a client"
                return
            our_mac = str_to_mac(lease.mac)
            try:
                self.outcome, self.error = await self._exchange(iface, our_mac, lease.mac)
            except Exception as exc:
                logger.exception("verify %s crashed", CREDENTIAL_NAMES[self.credential_type])
                self.outcome, self.error = VerifyOutcome.ERROR, str(exc) or type(exc).__name__
            finally:
                await iface.send_no_wait(build_client_leaving(bssid_mac, our_mac))
                await asyncio.sleep(_LEAVING_ECHO_S)

    async def _exchange(self, iface, our_mac: bytes, our_mac_str: str) -> tuple[VerifyOutcome, Optional[str]]:
        """The protocol exchange as the client ``our_mac``: the outcome, and the error when it is ERROR."""
        raise NotImplementedError
