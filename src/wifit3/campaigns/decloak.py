"""Guess a hidden AP's SSID: directed Probe Requests first, then Association Requests.

An AP answers a directed probe only when the probe names it, so the sink's existing
decloak path catches the reply. When every probe goes unanswered, the same candidates are
claimed in Association Requests: 802.11 leaves a refused station AUTHENTICATED, so one
Auth Req covers the whole sweep.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Callable, Optional

from wifit3.campaigns.auth_assoc import Association, AssocState, build_client_leaving
from wifit3.campaigns.campaign import Campaign
from wifit3.dot11 import mac_to_str, str_to_mac
from wifit3.dot11.mac import random_client_mac
from wifit3.dot11.probe import probe_req
from wifit3.models import AccessPoint
from wifit3.wlan.array import WlanArray

logger = logging.getLogger(__name__)

SSID_MAX_OCTETS = 32
PROBE_REPLY_WINDOW_S = 0.3
MIN_CANDIDATE_INTERVAL_S = 0.3
ASSOCIATION_CANDIDATE_LIMIT = 32

# Curated suffix list, kept short on purpose so a full run is ~5 seconds. In-house: keep
# growing it, do not import anyone else's.
SIBLING_SUFFIXES: list[str] = [
    "",
    "-Guest", "_Guest", "-guest", " Guest",
    "-5G", "_5G", "-5GHz",
    "-2G", "_2G", "-2.4G", "-2.4GHz",
    "-IoT", "_IoT",
    "-Setup", "_Setup",
    "-EXT",
]


def candidates_from_sibling(sibling_ssid: str) -> list[str]:
    """Every SIBLING_SUFFIXES entry appended to sibling_ssid, minus duplicates and names
    over 32 octets. Empty when sibling_ssid is empty."""
    if not sibling_ssid:
        return []
    candidates: list[str] = []
    for suffix in SIBLING_SUFFIXES:
        candidate = sibling_ssid + suffix
        if (candidate and candidate not in candidates
                and len(candidate.encode("utf-8")) <= SSID_MAX_OCTETS):
            candidates.append(candidate)
    return candidates


def named_sibling_ssid(array: WlanArray, hidden: AccessPoint) -> str:
    """The SSID of whichever of hidden.siblings has an SSID and the most beacons. Empty
    when it has no siblings, or none of them are named."""
    best_ssid, best_beacons = "", -1
    for bssid in hidden.siblings:
        sibling = array.access_points.get(bssid)
        if sibling and sibling.ssid and sibling.beacons > best_beacons:
            best_ssid, best_beacons = sibling.ssid, sibling.beacons
    return best_ssid


class DecloakCampaign(Campaign):
    """Sends a Probe Request per candidate SSID, then an Assoc Req per candidate, until
    one of them makes the AP name itself."""

    button_id = "btn-decloak"
    key = "decloak"
    hotkey = ("h", "Decloak")
    idle_label = "Decloak"
    run_label = "Stop Decloak"

    @classmethod
    def visible(cls, ap: AccessPoint) -> bool:
        return ap.is_hidden

    def __init__(self, array: WlanArray, target: AccessPoint, *,
                 candidates: Optional[list[str]] = None,
                 candidate_interval: float = MIN_CANDIDATE_INTERVAL_S,
                 log: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(ap=target, array=array)
        self.sibling_ssid = named_sibling_ssid(array, target)
        self.candidates = (list(candidates) if candidates is not None
                           else candidates_from_sibling(self.sibling_ssid))
        self.bssid_bytes = str_to_mac(target.bssid)
        self.source_mac = random_client_mac()
        self.revealed: Optional[str] = None
        self.sent = 0
        self._candidate_interval = candidate_interval
        self._last_candidate_at: Optional[float] = None
        self._log = log or (lambda _message: None)

    def status_under_card(self) -> str:
        return "● Decloak"

    def status_headlines(self, vault) -> list[str]:
        return ["[bold cyan]● Decloak[/bold cyan] guessing SSIDs",
                f"[dim]{self.sent}/{len(self.candidates)} sent[/dim]"]

    async def _loop(self) -> None:
        if not self.candidates:
            self._log("no candidate SSIDs to try")
            return
        if self.iface is None:
            self._log(f"no card can reach channel {self.ap.channel}")
            return
        if self.sibling_ssid:
            self._log(f"guessing from '{self.sibling_ssid}'")
        else:
            self._log(f"trying {len(self.candidates)} candidate SSIDs")
        self.array.register_forged_mac(self.source_mac)
        if self.iface.current_channel != self.ap.channel:
            await self.iface.set_channel(self.ap.channel)
        self.revealed = await self._probe_candidates()
        if self.revealed is None and not self.stopped:
            self.revealed = await self._associate_candidates()
        if self.revealed is None:
            logger.info("[DECLOAK] %s: no candidate matched", self.ap.bssid)

    async def teardown(self) -> None:
        self.array.unregister_own_mac(self.source_mac)

    # ----- probes ------------------------------------------------------------

    async def _probe_candidates(self) -> Optional[str]:
        """One directed Probe Request per candidate. The AP replies to the one naming it,
        and the sink sets ap.ssid off that reply."""
        before = self.ap.ssid
        logger.info("[DECLOAK] %s: probing %d candidates as STA %s", self.ap.bssid,
                    len(self.candidates), mac_to_str(self.source_mac))
        for candidate in self.candidates:
            if self.stopped:
                return None
            if not await self._wait_for_candidate_slot():
                return None
            await self.iface.send_no_wait(
                probe_req(self.bssid_bytes, self.source_mac, candidate,
                          channel=self.ap.channel)
            )
            self.sent += 1
            revealed = await self._wait_for_ssid(before)
            if revealed is not None:
                logger.info("[DECLOAK] probe hit on %r -> %r", candidate, revealed)
                return revealed
        return None

    async def _wait_for_ssid(self, before: Optional[str]) -> Optional[str]:
        """Poll ap.ssid for PROBE_REPLY_WINDOW_S; the sink sets it from the RX thread."""
        deadline = time.monotonic() + PROBE_REPLY_WINDOW_S
        while time.monotonic() < deadline and not self.stopped:
            if self.ap.ssid and self.ap.ssid != before:
                return self.ap.ssid
            await asyncio.sleep(0.03)
        return None

    # ----- associations ------------------------------------------------------

    async def _associate_candidates(self) -> Optional[str]:
        """Authenticate, check the AP refuses an invented SSID, then claim each candidate."""
        candidates = self.candidates[:ASSOCIATION_CANDIDATE_LIMIT]
        logger.info("[DECLOAK] probes unanswered; associating through %d candidates",
                    len(candidates))
        # Unarmed, the AP's Auth Resp goes unACKed, authentication never completes, and
        # every Assoc Req comes back as a class-2 deauth instead of a verdict.
        arm = self.array.lease(fake_mac=self.source_mac, bssid=self.bssid_bytes,
                               iface=self.iface)
        async with arm:
            our_mac = str_to_mac(arm.mac) if arm.mac else self.source_mac
            association = Association(
                self.iface, self.ap.bssid, "", self.ap.channel, our_mac=our_mac,
                auth_timeout=0.2, assoc_timeout=0.3,
                assoc_trailer_ies=self.ap.rsn_ie or b"",
                privacy=bool(self.ap.rsn_ie), should_stop=lambda: self.stopped,
            )
            association.start()
            try:
                if not await association.authenticate():
                    logger.info("[DECLOAK] %s will not authenticate us", self.ap.bssid)
                    return None
                invented = f"wifit3-control-{os.urandom(8).hex()}"
                if not await self._wait_for_candidate_slot():
                    return None
                if await association.associate_as(invented) == 0:
                    logger.info("[DECLOAK] %s accepts any SSID; association proves nothing",
                                self.ap.bssid)
                    return None
                return await self._claim_each(association, candidates)
            finally:
                association.stop()
                await self._announce_leaving(our_mac)

    async def _claim_each(self, association: Association,
                          candidates: list[str]) -> Optional[str]:
        """Claim each candidate in turn; the one the AP accepts is the AP's own SSID."""
        for candidate in candidates:
            if self.stopped:
                return None
            # A refusal leaves us AUTHENTICATED and a disassoc returns us to it; only a
            # deauth drops us to UNAUTHENTICATED, where we have to authenticate again.
            if association.state is AssocState.UNAUTHENTICATED:
                if not await association.authenticate():
                    logger.info("[DECLOAK] %s deauthenticated us mid-sweep", self.ap.bssid)
                    return None
            if not await self._wait_for_candidate_slot():
                return None
            self.sent += 1
            if await association.associate_as(candidate) == 0:
                self.array.decloak(self.ap, candidate, "assoc")
                logger.info("[DECLOAK] %s accepted %r", self.ap.bssid, candidate)
                return candidate
        return None

    async def _wait_for_candidate_slot(self) -> bool:
        if self._last_candidate_at is not None:
            remaining = self._candidate_interval - (time.monotonic() - self._last_candidate_at)
            while remaining > 0 and not self.stopped:
                await asyncio.sleep(min(remaining, 0.05))
                remaining = self._candidate_interval - (
                    time.monotonic() - self._last_candidate_at
                )
        if self.stopped:
            return False
        self._last_candidate_at = time.monotonic()
        return True

    async def _announce_leaving(self, our_mac: bytes) -> None:
        try:
            await self.iface.send_no_wait(build_client_leaving(self.bssid_bytes, our_mac))
        except Exception:
            logger.debug("[DECLOAK] client-leaving cleanup failed", exc_info=True)
