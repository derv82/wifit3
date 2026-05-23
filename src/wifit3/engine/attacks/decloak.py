"""Active decloak: send directed Probe Requests with sibling-derived SSID
candidates and let the existing passive decloak path catch the response.

Why this works: a hidden AP only emits a Probe Response when the requesting
SSID matches its real one. By guessing sibling-suffix variants of a known
non-hidden sibling's SSID, we shortcut the "wait for a real client" delay
that pure passive decloak depends on.

Triggered exclusively by the user pressing 'd' in Scanner on a hidden row —
this module never auto-runs. ``WlanInterface._on_frame_parsed`` is the one
that actually flips ``ap.ssid`` when the AP echoes back a Probe Response;
we just poll for that flip per candidate.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import List, Optional

from wifit3.engine.models import AccessPoint

logger = logging.getLogger(__name__)


# Same standard-rates IE used by the PMKID harvester. Real APs only check
# that this is well-formed, not specifically what's in it.
_SUPPORTED_RATES = bytes([
    0x82, 0x84, 0x8B, 0x96,         # 1, 2, 5.5, 11 (basic)
    0x0C, 0x12, 0x18, 0x24,         # 6, 9, 12, 18
])
_EXT_SUPPORTED_RATES = bytes([
    0x30, 0x48, 0x60, 0x6C,         # 24, 36, 48, 54
])


# Curated suffix list — kept short on purpose so a full run is ~5 seconds.
# Empty string first covers the mesh / same-SSID-dual-band case (rare but
# cheap to check). Order is "most-likely-first" heuristic — Guest variants
# are the dominant hidden-sibling flavour we've seen.
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
    """Generate likely sibling SSIDs given a known visible sibling's SSID.

    Returns a deduplicated, order-preserving list. Empty bases yield an
    empty list — caller should validate they have a sibling SSID first.
    """
    if not base:
        return []
    out: List[str] = []
    seen: set[str] = set()
    for suffix in SIBLING_SUFFIXES:
        cand = base + suffix
        # Strip trailing whitespace only — leading whitespace would be
        # suspicious, but " Guest" suffix produces a legitimate space.
        cand = cand.rstrip()
        if cand and cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


def _str_to_mac(mac: str) -> bytes:
    return bytes(int(x, 16) for x in mac.split(":"))


def _random_client_mac() -> bytes:
    """Locally-administered, unicast MAC. LAA bit set, multicast bit clear."""
    rnd = os.urandom(5)
    return bytes([0x02]) + rnd


def _mac_bytes_to_str(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


class DecloakAttack:
    """Run an active decloak probe sequence against a single hidden AP."""

    def __init__(
        self,
        iface,
        target: AccessPoint,
        base_ssid: str,
        source_mac: Optional[bytes] = None,
        candidates_override: Optional[List[str]] = None,
    ):
        self.iface = iface
        self.target = target
        self.base_ssid = base_ssid
        self.bssid_bytes = _str_to_mac(target.bssid)
        self.source_mac = source_mac or _random_client_mac()
        # When non-None, bypass build_candidates() and use this list verbatim.
        # Powers the Shift+D test action where the user supplies SSIDs to try
        # directly (e.g. against a known-config router for pipeline checks).
        self.candidates_override = candidates_override
        # Register so client/handshake tracking ignores our forged STA.
        self.iface.register_forged_mac(self.source_mac)

    # ---- Frame builder ------------------------------------------------------

    def _build_probe_req(self, ssid: str) -> bytes:
        """Directed Probe Request frame addressed to the target BSSID with
        the candidate SSID in the SSID IE. The AP only responds if the SSID
        matches its real one (or it's broadcasting widely)."""
        # FC: type=mgmt(0), subtype=probe_req(4) → 0x40
        mac_hdr = (
            b"\x40\x00"                         # FC
            + b"\x00\x00"                       # Duration
            + self.bssid_bytes                  # Addr1 = BSSID (RA)
            + self.source_mac                   # Addr2 = us (TA)
            + self.bssid_bytes                  # Addr3 = BSSID
            + b"\x00\x00"                       # Seq (hardware fills)
        )
        # Tag 0 (SSID): truncate to 32B max per spec.
        ssid_bytes = ssid.encode("utf-8", errors="ignore")[:32]
        ssid_ie = bytes([0x00, len(ssid_bytes)]) + ssid_bytes
        # Tag 1 + Tag 50: rates (kept short — most APs only spot-check).
        rates_ie = bytes([0x01, len(_SUPPORTED_RATES)]) + _SUPPORTED_RATES
        ext_rates_ie = bytes([0x32, len(_EXT_SUPPORTED_RATES)]) + _EXT_SUPPORTED_RATES
        return mac_hdr + ssid_ie + rates_ie + ext_rates_ie

    # ---- Driver -------------------------------------------------------------

    async def run(
        self,
        per_candidate_timeout: float = 0.3,
    ) -> Optional[str]:
        """Send one Probe Request per candidate SSID, polling for the parser
        to flip ``ap.ssid``. Returns the discovered SSID on success, else
        None when the candidate list is exhausted with no response."""
        if self.iface.current_channel != self.target.channel:
            await self.iface.set_channel(self.target.channel)

        candidates = (
            self.candidates_override
            if self.candidates_override is not None
            else build_candidates(self.base_ssid)
        )
        bssid_lower = self.target.bssid.lower()
        initial_ssid = self.iface.access_points.get(bssid_lower, self.target).ssid

        src = ("explicit" if self.candidates_override is not None
               else f"base '{self.base_ssid}'")
        logger.info(
            f"[DECLOAK] {self.target.bssid} — trying {len(candidates)} candidates "
            f"({src}) as STA {_mac_bytes_to_str(self.source_mac)}"
        )

        for candidate in candidates:
            frame = self._build_probe_req(candidate)
            await self.iface.send_raw(frame, use_no_ack=True)

            # Poll briefly — parser side flips ap.ssid asynchronously when
            # the AP echoes back a Probe Response that the existing decloak
            # guard in WlanInterface._on_frame_parsed sees.
            deadline = time.time() + per_candidate_timeout
            while time.time() < deadline:
                ap_state = self.iface.access_points.get(bssid_lower)
                if ap_state and ap_state.ssid and ap_state.ssid != initial_ssid:
                    logger.info(
                        f"[DECLOAK] hit on candidate '{candidate}' → "
                        f"{ap_state.ssid!r}"
                    )
                    return ap_state.ssid
                await asyncio.sleep(0.03)

        logger.info(f"[DECLOAK] exhausted candidates for {self.target.bssid}")
        return None
