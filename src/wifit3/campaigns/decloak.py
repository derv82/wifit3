"""Guess a hidden AP's SSID by naming candidates at it, one method at a time.

Each enabled method sweeps every candidate before the next begins, cheapest first:
directed Probe Requests (hardware-retried, so a miss is a real miss), then broadcast
Probe Requests (one frame, never ACKed, but some radios answer only these), then
Association Requests (no SSID in the Auth exchange, so it reaches a radio that ignores
probes entirely). All of it runs inside one device claim, so the card HW-ACKs the AP's
replies throughout instead of making it retransmit them.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from wifit3.campaigns.auth_assoc import Association, AssocState, build_client_leaving
from wifit3.campaigns.campaign import Campaign
from wifit3.dot11 import SSID_MAX_OCTETS, mac_to_str, str_to_mac
from wifit3.dot11.mac import BROADCAST_MAC, random_client_mac
from wifit3.dot11.probe import probe_req
from wifit3.models import AccessPoint
from wifit3.wlan.array import WlanArray

logger = logging.getLogger(__name__)

PROBE_RESPONSE_WAIT_S = 0.15
AUTH_TIMEOUT_S = 0.2
ASSOC_TIMEOUT_S = 0.3

# In-house list: keep growing it, do not import anyone else's.
SIBLING_SUFFIXES: list[str] = [
    "",

    "-Guest",   "_Guest",   " Guest",
    "-guest",   "_guest",   " guest",
    "-GUEST",   "_GUEST",   " GUEST",
    "-Visitor", "_Visitor", " Visitor",

    # IoT / Smart Home (Segmented 2.4GHz networks)
    "-IoT",   "_IoT",   " IoT",
    "-iot",   "_iot",   " iot",
    "-IOT",   "_IOT",   " IOT",
    "-Smart", "_Smart", " Smart",

    "-2.4G",   "_2.4G",   " 2.4G",
    "-2.4GHz", "_2.4GHz", " 2.4GHz",
    "-2G",     "_2G",     " 2G",
    "-2",      "_2",      " 2", "2",

    "-5G",   "_5G",   " 5G",
    "-5GHz", "_5GHz", " 5GHz",
    "-5",    "_5",

    # Guest <-> Band
    "-Guest-2.4G", "_Guest_2G", "-Guest 2G", "-Guest-2.4G",
    "-Guest-5G",   "_Guest_5G", "-Guest 5G", "-Guest-5G",
    "-5G-GUEST", "_5G-GUEST", "-2.4G-GUEST", "_2.4G-GUEST",
    "-5G_Guest", "_5G_Guest", "-2.4G_Guest", "_2.4G_Guest",

    # Extenders & Repeaters
    "-EXT", "_EXT", " EXT",
    "-Ext", "_Ext", " Ext",
    "-ext", "_ext", " ext",
    "-EXT2G", "_EXT2G", "-EXT5G", "_EXT5G",
    "-RE", "_RE",
]


@dataclass
class DecloakInput:
    """The modal's output: the exact SSIDs to send and which methods send them. Methods run
    in this order, each sweeping every SSID before the next starts."""
    candidate_ssids: list[str] = field(default_factory=list)
    use_directed_probe: bool = True
    use_broadcast_probe: bool = False
    use_authassoc_probe: bool = False


def candidates_from_sibling(sibling_ssid: str) -> list[str]:
    """Every SIBLING_SUFFIXES entry appended to sibling_ssid, minus duplicates and names
    over 32 octets. Empty when sibling_ssid is empty."""
    if not sibling_ssid:
        return []
    candidates: list[str] = []
    for suffix in SIBLING_SUFFIXES:
        candidate = (sibling_ssid + suffix).rstrip()
        if (candidate and candidate not in candidates
                and len(candidate.encode("utf-8")) <= SSID_MAX_OCTETS):
            candidates.append(candidate)
    return candidates


class DecloakCampaign(Campaign):
    """Names every candidate SSID at one hidden AP until the AP names itself back."""

    button_id = "btn-decloak"
    key = "decloak"
    hotkey = ("h", "Decloak")
    idle_label = "Decloak"
    run_label = "Stop Decloak"

    @classmethod
    def visible(cls, ap: AccessPoint) -> bool:
        return ap.is_hidden

    def __init__(self, array: WlanArray, target: AccessPoint,
                 decloak_input: Optional[DecloakInput] = None, *,
                 log: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(ap=target, array=array)
        self.sibling_ssid = array.guessid(target)
        chosen = decloak_input or DecloakInput(candidates_from_sibling(self.sibling_ssid))
        self.candidate_ssids = list(chosen.candidate_ssids)
        self.use_directed_probe = chosen.use_directed_probe
        self.use_broadcast_probe = chosen.use_broadcast_probe
        self.use_authassoc_probe = chosen.use_authassoc_probe
        self.bssid_bytes = str_to_mac(target.bssid)
        self.source_mac = random_client_mac()
        self.revealed: Optional[str] = None
        self.sent = 0
        self._log = log or (lambda _message: None)

    @property
    def planned(self) -> int:
        """Candidates times enabled methods: every method sweeps the whole list."""
        methods = sum((self.use_directed_probe, self.use_broadcast_probe,
                       self.use_authassoc_probe))
        return len(self.candidate_ssids) * methods

    def status_under_card(self) -> str:
        return "● Decloak"

    def status_headlines(self, vault) -> list[str]:
        return ["[bold cyan]● Decloak[/bold cyan] guessing SSIDs",
                f"[dim]{self.sent}/{self.planned} sent[/dim]"]

    async def _loop(self) -> None:
        if not self.candidate_ssids:
            self._log("no named sibling to guess from")
            return
        if self.iface is None:
            self._log(f"no card can reach channel {self.ap.channel}")
            return
        self._log(f"guessing from '{self.sibling_ssid}'")
        async with self.array.claim(self.iface):
            if self.iface.current_channel != self.ap.channel:
                await self.iface.set_channel(self.ap.channel)
            await self._adopt_acking_mac()
            try:
                for enabled, method in ((self.use_directed_probe, self._probe_directed),
                                        (self.use_broadcast_probe, self._probe_broadcast),
                                        (self.use_authassoc_probe, self._claim_candidates)):
                    if self.stopped or self.revealed is not None:
                        break
                    if enabled:
                        self.revealed = await method()
            finally:
                await self.iface.clear_fake_mac()
        if self.revealed is None and not self.stopped:
            logger.info("[DECLOAK] %s: no candidate matched", self.ap.bssid)

    async def _adopt_acking_mac(self) -> None:
        """Take the MAC the card will HW-ACK as our STA identity. Without it a MediaTek
        card retransmits each probe ~12 times and the AP retransmits every reply."""
        assumed = await self.iface.set_fake_mac(self.source_mac, self.ap.bssid)
        if assumed is None:
            logger.info("[DECLOAK] %s has no active monitor; replies go unACKed",
                        self.ap.bssid)
        else:
            self.source_mac = str_to_mac(assumed)
        self.array.register_forged_mac(self.source_mac)
        logger.info("[DECLOAK] %s: sweeping %d candidate(s) as STA %s", self.ap.bssid,
                    len(self.candidate_ssids), mac_to_str(self.source_mac))

    async def teardown(self) -> None:
        self.array.unregister_own_mac(self.source_mac)

    # ----- probes ------------------------------------------------------------

    async def _probe_directed(self) -> Optional[str]:
        """Addr1 and Addr3 both the BSSID: the AP ACKs it, so the card stops at one copy
        and an unanswered probe is the AP declining rather than a lost frame."""
        return await self._sweep_probes(self.bssid_bytes, "directed")

    async def _probe_broadcast(self) -> Optional[str]:
        """Addr1 and Addr3 both broadcast. Group-addressed, so never ACKed and never
        retried; some radios answer only this shape."""
        return await self._sweep_probes(BROADCAST_MAC, "broadcast")

    async def _sweep_probes(self, addressee: bytes, method: str) -> Optional[str]:
        before = self.ap.ssid
        for ssid in self.candidate_ssids:
            if self.stopped:
                return None
            await self.iface.send_no_wait(
                probe_req(addressee, self.source_mac, ssid, channel=self.ap.channel)
            )
            self.sent += 1
            revealed = await self._revealed_ssid(before)
            if revealed is not None:
                logger.info("[DECLOAK] %s probe hit on %r -> %r", method, ssid, revealed)
                return revealed
        return None

    async def _revealed_ssid(self, before: Optional[str]) -> Optional[str]:
        """The AP's name once the sink has it, else None after PROBE_RESPONSE_WAIT_S. The
        sink sets it off our own Probe Response or off one the AP sent another client."""
        deadline = time.monotonic() + PROBE_RESPONSE_WAIT_S
        while time.monotonic() < deadline and not self.stopped:
            if self.ap.ssid and self.ap.ssid != before:
                return self.ap.ssid
            await asyncio.sleep(0.03)
        return None

    # ----- associations ------------------------------------------------------

    async def _claim_candidates(self) -> Optional[str]:
        """Authenticate, check the AP refuses an invented SSID, then claim each candidate."""
        logger.info("[DECLOAK] associating through %d candidate(s)",
                    len(self.candidate_ssids))
        association = Association(
            self.iface, self.ap.bssid, "", self.ap.channel, our_mac=self.source_mac,
            auth_timeout=AUTH_TIMEOUT_S, assoc_timeout=ASSOC_TIMEOUT_S,
            assoc_trailer_ies=self.ap.rsn_ie or b"",
            privacy=bool(self.ap.rsn_ie), should_stop=lambda: self.stopped,
        )
        association.start()
        try:
            if not await association.authenticate():
                logger.info("[DECLOAK] %s will not authenticate us", self.ap.bssid)
                return None
            invented = f"wifit3-control-{os.urandom(8).hex()}"
            if await association.associate_as(invented) == 0:
                logger.info("[DECLOAK] %s accepts any SSID; association proves nothing",
                            self.ap.bssid)
                return None
            return await self._claim_each(association)
        finally:
            association.stop()
            await self._announce_leaving()

    async def _claim_each(self, association: Association) -> Optional[str]:
        """Claim each candidate in turn; the one the AP accepts is the AP's own SSID."""
        for ssid in self.candidate_ssids:
            if self.stopped:
                return None
            # A refusal leaves us AUTHENTICATED and a disassoc returns us to it; only a
            # deauth drops us to UNAUTHENTICATED, where we have to authenticate again.
            if association.state is AssocState.UNAUTHENTICATED:
                if not await association.authenticate():
                    logger.info("[DECLOAK] %s deauthenticated us mid-sweep", self.ap.bssid)
                    return None
            self.sent += 1
            if await association.associate_as(ssid) == 0:
                self.array.decloak(self.ap, ssid, "assoc")
                logger.info("[DECLOAK] %s accepted %r", self.ap.bssid, ssid)
                return ssid
        return None

    async def _announce_leaving(self) -> None:
        try:
            await self.iface.send_no_wait(
                build_client_leaving(self.bssid_bytes, self.source_mac)
            )
        except Exception:
            logger.debug("[DECLOAK] client-leaving cleanup failed", exc_info=True)
