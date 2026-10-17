"""Live key verifier: validates stored credentials against a live access point.

Runs on-demand from the Vault UI without using offline VaultTools.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum, auto
import logging
import os
import time
from typing import TYPE_CHECKING, Optional

from wifit3.campaigns.auth_assoc import Association, WlanTransport
from wifit3.campaigns.campaign import Campaign
from wifit3.campaigns.pin import EMPTY_PIN_LABEL
from wifit3.campaigns.wps.registrar import PinResult, WpsRegistrar
from wifit3.crack.wep import rc4_keystream
from wifit3.dot11 import build_deauth, random_client_mac, str_to_mac
from wifit3.dot11.ie import GENERIC_RSN_IE, force_psk_akm
from wifit3.dot11.mac import mac_header
from wifit3.dot11.packet import EapolPacket, WepDataPacket
from wifit3.dot11.wep.crypto import arp_request_plaintext, wep_encrypt
from wifit3.dot11.wpa import KCK_LEN, build_eapol_m2, derive_pmk, derive_ptk, random_nonce
from wifit3.dot11.wsc.assoc_ie import WPS_REQ_REGISTRAR, wps_assoc_ie
from wifit3.models import AccessPoint, PersistedCapture, CaptureType

if TYPE_CHECKING:
    from wifit3.wlan.array import WlanArray
    from wifit3.wlan.interface import WlanInterface

logger = logging.getLogger(__name__)


class VerifyStatus(Enum):
    """Result status of a credential verification attempt."""
    SUCCESS = auto()
    INCORRECT = auto()
    ERROR = auto()


@dataclass(frozen=True)
class VerifyResult:
    """Outcome, title, and body for the verification toast."""
    status: VerifyStatus
    title: str
    body: str

    @property
    def is_success(self) -> bool:
        return self.status == VerifyStatus.SUCCESS


class LiveKeyVerifier:
    """Verifies stored credentials against in-range APs via live radio frames."""

    async def verify_credential(
        self,
        capture: PersistedCapture,
        array: WlanArray,
        target_type: Optional[CaptureType] = None,
    ) -> VerifyResult:
        """Verify the credential in ``capture`` against the live AP using ``array``."""
        ssid = capture.ssid or capture.bssid

        if Campaign.active is not None:
            return VerifyResult(
                VerifyStatus.ERROR,
                f"Unable to verify with {ssid}",
                f"Error: {Campaign.active.key} campaign is active",
            )

        if not array or not array.members:
            return VerifyResult(
                VerifyStatus.ERROR,
                f"Unable to verify with {ssid}",
                "Error: No wireless adapter available",
            )

        ap: Optional[AccessPoint] = array.access_points.get(capture.bssid.lower())
        if ap is None:
            return VerifyResult(
                VerifyStatus.ERROR,
                f"Unable to verify with {ssid}",
                "Error: AP not in range",
            )

        iface = array.select_iface(ap.channel)
        if iface is None:
            return VerifyResult(
                VerifyStatus.ERROR,
                f"Unable to verify with {ssid}",
                f"Error: No wireless card supports channel {ap.channel}",
            )

        async with array.claim(iface):
            await iface.set_channel(ap.channel)
            now = time.time()
            if now - ap.last_seen > 15.0:
                seen = await array.wait_until(
                    lambda: time.time() - array.access_points.get(capture.bssid.lower(), ap).last_seen <= 2.0,
                    timeout=1.5,
                    poll=0.1,
                )
                if not seen:
                    return VerifyResult(
                        VerifyStatus.ERROR,
                        f"Unable to verify with {ssid}",
                        f"Error: AP not in range on channel {ap.channel}",
                    )

            our_mac = random_client_mac()
            our_mac_str = array.register_own_mac(our_mac)
            await iface.set_fake_mac(our_mac, str_to_mac(ap.bssid))
            try:
                mode = target_type or capture.type
                if mode in (CaptureType.WPA_PSK, CaptureType.WPS_PBC):
                    psk = capture.value or ""
                    is_wps_psk = capture.type in (CaptureType.WPS_PIN, CaptureType.WPS_PBC)
                    return await self._verify_wpa_psk(iface, array, ap, psk, our_mac, our_mac_str, is_wps=is_wps_psk)
                if mode == CaptureType.WPS_PIN:
                    if capture.pin is None:
                        return VerifyResult(
                            VerifyStatus.ERROR,
                            f"Unable to verify with {ssid}",
                            "Error: Stored WPS PIN is missing",
                        )
                    return await self._verify_wps_pin(iface, ap, capture.pin, our_mac)
                if mode == CaptureType.WEP:
                    key = capture.value or ""
                    return await self._verify_wep_key(iface, array, ap, key, our_mac)
                return VerifyResult(
                    VerifyStatus.ERROR,
                    f"Unable to verify with {ssid}",
                    f"Error: Unsupported credential type {mode}",
                )
            finally:
                try:
                    deauth = build_deauth(str_to_mac(ap.bssid), our_mac, str_to_mac(ap.bssid), 3)
                    await iface.send_no_wait(deauth)
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
                finally:
                    try:
                        await iface.clear_fake_mac()
                    except Exception:
                        pass
                    array.unregister_own_mac(our_mac_str)

    async def _verify_wpa_psk(
        self,
        iface: WlanInterface,
        array: WlanArray,
        ap: AccessPoint,
        psk: str,
        our_mac: bytes,
        our_mac_str: str,
        is_wps: bool = False,
    ) -> VerifyResult:
        ssid = ap.ssid or "AP"
        noun = "WPS PSK" if is_wps else "PSK"
        val_name = "PSK" if is_wps else "Key"

        trailer = force_psk_akm(ap.rsn_ie or GENERIC_RSN_IE) or GENERIC_RSN_IE
        assoc = Association(
            iface,
            ap.bssid,
            ap.ssid or "",
            ap.channel,
            our_mac=our_mac,
            assoc_trailer_ies=trailer,
        )
        assoc.start()
        try:
            if not await assoc.associate(attempts=2):
                err = assoc.fail_reason or "association rejected"
                logger.warning("[%s] Association failed: %s", ssid, err)
                return VerifyResult(
                    VerifyStatus.ERROR,
                    f"Unable to verify with {ssid}",
                    f"Error: {err}",
                )
        finally:
            assoc.stop()

        logger.info("[%s] Assoc accepted by %s. Waiting for EAPOL M1...", ssid, ap.bssid)
        m1_pkt = await array.next_frame(
            lambda p: (
                isinstance(p, EapolPacket)
                and p.dest == our_mac_str
                and p.bssid == ap.bssid.lower()
                and p.msg_num == 1
                and p.nonce is not None
            ),
            timeout=2.0,
        )
        if m1_pkt is None or m1_pkt.nonce is None:
            logger.warning("[%s] AP %s never sent EAPOL M1", ssid, ap.bssid)
            return VerifyResult(
                VerifyStatus.ERROR,
                f"Unable to verify with {ssid}",
                "Error: AP never sent EAPOL M1",
            )

        replay = int.from_bytes(m1_pkt.replay_counter, "big") if m1_pkt.replay_counter else 1
        logger.info("[%s] <- Received EAPOL M1 (replay %d) from %s", ssid, replay, ap.bssid)

        pmk = derive_pmk(psk, (ap.ssid or "").encode())
        snonce = random_nonce()
        kck = derive_ptk(pmk, str_to_mac(ap.bssid), our_mac, m1_pkt.nonce, snonce, KCK_LEN)
        m2 = build_eapol_m2(
            str_to_mac(ap.bssid), our_mac, snonce, kck, replay=replay, rsn_ie=trailer
        )
        logger.info("[%s] -> Sending EAPOL M2 with derived KCK MIC to %s", ssid, ap.bssid)
        await iface.send_no_wait(m2)

        m3_pkt = await array.next_frame(
            lambda p: (
                isinstance(p, EapolPacket)
                and p.dest == our_mac_str
                and p.bssid == ap.bssid.lower()
                and p.msg_num == 3
            ),
            timeout=2.0,
        )
        if m3_pkt is not None:
            logger.info("[%s] <- Received EAPOL M3 from %s: PSK is correct!", ssid, ap.bssid)
            return VerifyResult(
                VerifyStatus.SUCCESS,
                f"{noun} verified for {ssid}",
                f'{val_name} "{psk}" still works',
            )

        logger.warning("[%s] No EAPOL M3 received from %s (timeout/deauth): PSK is incorrect", ssid, ap.bssid)
        return VerifyResult(
            VerifyStatus.INCORRECT,
            f"{noun} incorrect for {ssid}",
            f'{val_name} "{psk}" is incorrect (no M3)',
        )

    async def _verify_wps_pin(
        self,
        iface: WlanInterface,
        ap: AccessPoint,
        pin: str,
        our_mac: bytes,
    ) -> VerifyResult:
        ssid = ap.ssid or "AP"
        displayed_pin = pin or EMPTY_PIN_LABEL
        assoc = Association(
            iface,
            ap.bssid,
            ap.ssid or "",
            ap.channel,
            our_mac=our_mac,
            assoc_trailer_ies=wps_assoc_ie(WPS_REQ_REGISTRAR, ap.wps_version == "2.0"),
        )
        assoc.start()
        transport = WlanTransport(iface, str_to_mac(ap.bssid), our_mac)
        transport.start()
        try:
            if not await assoc.associate(attempts=2):
                err = assoc.fail_reason or "association rejected"
                logger.warning("[%s] WPS Association failed: %s", ssid, err)
                return VerifyResult(
                    VerifyStatus.ERROR,
                    f"Unable to verify with {ssid}",
                    f"Error: {err}",
                )
            logger.info("[%s] Assoc accepted with WPS IE by %s. Starting WSC session for PIN %s...", ssid, ap.bssid, displayed_pin)
            reg = WpsRegistrar(transport, str_to_mac(ap.bssid), our_mac,
                               channel=ap.channel, wsc_2_0=ap.wps_version == "2.0")
            outcome = await reg.try_pin(pin)
            logger.info("[%s] WPS session outcome: %s (detail: %s)", ssid, outcome.result.value, outcome.detail)
            if outcome.result == PinResult.SUCCESS:
                return VerifyResult(
                    VerifyStatus.SUCCESS,
                    f"WPS PIN verified for {ssid}",
                    f'PIN "{displayed_pin}" still works',
                )
            if outcome.result in (PinResult.FIRST_HALF_WRONG, PinResult.SECOND_HALF_WRONG):
                return VerifyResult(
                    VerifyStatus.INCORRECT,
                    f"WPS PIN incorrect for {ssid}",
                    f'PIN "{displayed_pin}" is incorrect',
                )
            return VerifyResult(
                VerifyStatus.ERROR,
                f"Unable to verify with {ssid}",
                f"Error: {outcome.detail or outcome.result.value}",
            )
        finally:
            assoc.stop()
            transport.stop()

    async def _verify_wep_key(
        self,
        iface: WlanInterface,
        array: WlanArray,
        ap: AccessPoint,
        key_hex: str,
        our_mac: bytes,
    ) -> VerifyResult:
        ssid = ap.ssid or "AP"
        try:
            key_bytes = bytes.fromhex(key_hex)
        except ValueError:
            key_bytes = key_hex.encode("ascii")

        def _is_valid_wep(p) -> bool:
            if not (isinstance(p, WepDataPacket) and p.bssid == ap.bssid.lower() and p.iv and p.cipher):
                return False
            if len(p.cipher) < 6:
                return False
            ks = rc4_keystream(p.iv + key_bytes, 6)
            plain = bytes(c ^ k for c, k in zip(p.cipher[:6], ks))
            return plain == b"\xaa\xaa\x03\x00\x00\x00"

        logger.info("[%s] Checking for passive WEP data packets on %s...", ssid, ap.bssid)
        passive = await array.next_frame(_is_valid_wep, timeout=0.5)
        if passive is not None:
            logger.info("[%s] Passive WEP packet decrypted to an LLC/SNAP header: key verified!", ssid)
            return VerifyResult(
                VerifyStatus.SUCCESS,
                f"WEP Key verified for {ssid}",
                f'Key "{key_hex}" still works',
            )

        assoc = Association(iface, ap.bssid, ap.ssid or "", ap.channel, our_mac=our_mac)
        assoc.start()
        try:
            if not await assoc.associate(attempts=2):
                err = assoc.fail_reason or "association rejected"
                logger.warning("[%s] WEP Association failed: %s", ssid, err)
                return VerifyResult(
                    VerifyStatus.ERROR,
                    f"Unable to verify with {ssid}",
                    f"Error: {err}",
                )
        finally:
            assoc.stop()

        pt = arp_request_plaintext(
            sender_mac=our_mac, sender_ip=b"\xc0\xa8\x01\xfe", target_ip=b"\xc0\xa8\x01\x01"
        )
        iv = os.urandom(3)
        ks = rc4_keystream(iv + key_bytes, len(pt) + 4)
        body = iv + b"\x00" + wep_encrypt(ks, pt)
        # 802.11 ToDS Data header: fc=0x0841 (ToDS=1, Protected=1)
        # Addr1 = AP BSSID (RA), Addr2 = our_mac (TA), Addr3 = broadcast (DA)
        hdr = mac_header(b"\x08\x41", str_to_mac(ap.bssid), our_mac, b"\xff\xff\xff\xff\xff\xff")
        frame = hdr + body

        logger.info(
            "[%s] Assoc accepted by %s. Injecting WEP-encrypted ARP request (key len %d)...",
            ssid,
            ap.bssid,
            len(key_bytes),
        )

        listen_task = asyncio.create_task(array.next_frame(_is_valid_wep, timeout=2.5))

        for _ in range(3):
            await iface.send_no_wait(frame)
            await asyncio.sleep(0.1)

        relayed = await listen_task
        if relayed is not None:
            logger.info("[%s] <- Relayed FromDS WEP packet decrypted to an LLC/SNAP header: key verified!", ssid)
            return VerifyResult(
                VerifyStatus.SUCCESS,
                f"WEP Key verified for {ssid}",
                f'Key "{key_hex}" still works',
            )

        logger.warning("[%s] No relayed WEP frame heard after ARP injection (timeout)", ssid)
        return VerifyResult(
            VerifyStatus.INCORRECT,
            f"WEP Key incorrect for {ssid}",
            f'Key "{key_hex}" is unconfirmed by AP',
        )
