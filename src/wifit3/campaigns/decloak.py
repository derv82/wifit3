"""Active decloak: send directed Probe Requests with sibling-derived SSID
candidates and let the existing passive decloak path catch the response."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import List, Optional

from wifit3.models import AccessPoint
from wifit3.dot11 import mac_to_str, str_to_mac
from wifit3.dot11.probe import probe_req

logger = logging.getLogger(__name__)


# Curated suffix list, kept short on purpose so a full run is ~5 seconds.
SIBLING_SUFFIXES: List[str] = [
    "",
    "-Guest", "_Guest", "-guest", " Guest",
    "-5G", "_5G", "-5GHz",
    "-2G", "_2G", "-2.4G", "-2.4GHz",
    "-IoT", "_IoT",
    "-Setup", "_Setup",
    "-EXT",
]


def build_candidates(base: str) -> List[str]:
    """Generate likely sibling SSIDs given a known visible sibling's SSID."""
    if not base:
        return []
    out: List[str] = []
    seen: set[str] = set()
    for suffix in SIBLING_SUFFIXES:
        cand = base + suffix
        cand = cand.rstrip()
        if cand and cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


def _random_client_mac() -> bytes:
    """Locally-administered, unicast MAC. LAA bit set, multicast bit clear."""
    rnd = os.urandom(5)
    return bytes([0x02]) + rnd


class DecloakAttack:
    """Run an active decloak probe sequence against a single hidden AP."""

    def __init__(
        self,
        array,
        target: AccessPoint,
        base_ssid: str,
        source_mac: Optional[bytes] = None,
        candidates_override: Optional[List[str]] = None,
    ):
        self.array = array
        self.target = target
        self.base_ssid = base_ssid
        self.bssid_bytes = str_to_mac(target.bssid)
        self.source_mac = source_mac or _random_client_mac()
        # When non-None, bypass build_candidates() and use this list verbatim
        # (a hook for supplying SSIDs directly; currently exercised only by tests).
        self.candidates_override = candidates_override
        # Register so client/handshake tracking ignores our forged STA.
        self.array.register_forged_mac(self.source_mac)

    # ---- Driver -------------------------------------------------------------

    async def run(
        self,
        per_candidate_timeout: float = 0.3,
    ) -> Optional[str]:
        """Send one Probe Request per candidate SSID, polling for the parser
        to flip ``ap.ssid``. Returns the discovered SSID on success, else
        None when the candidate list is exhausted with no response."""
        iface = self.array.select_iface(self.target.channel)
        if iface is None:
            logger.info("[DECLOAK] no card can reach channel %s for %s",
                        self.target.channel, self.target.bssid)
            return None
        if iface.current_channel != self.target.channel:
            await iface.set_channel(self.target.channel)

        candidates = (
            self.candidates_override
            if self.candidates_override is not None
            else build_candidates(self.base_ssid)
        )
        bssid_lower = self.target.bssid.lower()
        initial_ssid = self.array.access_points.get(bssid_lower, self.target).ssid

        src = ("explicit" if self.candidates_override is not None
               else f"base '{self.base_ssid}'")
        logger.info(
            f"[DECLOAK] {self.target.bssid}: trying {len(candidates)} candidates "
            f"({src}) as STA {mac_to_str(self.source_mac)}"
        )

        for candidate in candidates:
            frame = probe_req(self.bssid_bytes, self.source_mac, candidate)
            await iface.send_no_wait(frame)

            # Poll briefly: the parser flips ap.ssid asynchronously when the AP echoes back a
            # Probe Response the sink's decloak guard sees.
            deadline = time.time() + per_candidate_timeout
            while time.time() < deadline:
                ap_state = self.array.access_points.get(bssid_lower)
                if ap_state and ap_state.ssid and ap_state.ssid != initial_ssid:
                    logger.info(
                        f"[DECLOAK] hit on candidate '{candidate}' → "
                        f"{ap_state.ssid!r}"
                    )
                    return ap_state.ssid
                await asyncio.sleep(0.03)

        logger.info(f"[DECLOAK] exhausted candidates for {self.target.bssid}")
        return None
