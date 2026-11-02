"""VerifyPskCampaign: associate, answer M1 with an M2 MIC'd from the PSK; an M3 means the AP accepted it."""
from __future__ import annotations

from typing import Optional

from wifit3.campaigns.auth_assoc import Association
from wifit3.dot11 import str_to_mac
from wifit3.dot11.deauth import REASON_4WAY_HANDSHAKE_TIMEOUT
from wifit3.dot11.eapol import KEY_DESCRIPTOR_VERSION_MASK, KeyDescriptorVersion
from wifit3.dot11.ie import GENERIC_RSN_IE, force_psk_akm
from wifit3.dot11.packet import DeauthPacket, EapolPacket
from wifit3.dot11.wpa import KCK_LEN, build_eapol_m2, derive_pmk, derive_ptk, random_nonce
from wifit3.models import CaptureType

from .campaign import VerifyCampaign, VerifyOutcome

_M2_ATTEMPTS = 3             # M1s answered before an AP that keeps re-sending M1 means a wrong PSK
_M2_RESENDS = 3              # M2 resends while the AP hasn't ACKed it
_M1_TIMEOUT_S = 2.0
_EAPOL_REPLY_TIMEOUT_S = 3.0     # hostapd re-sends M1 or M3 about once a second


def _replay_counter(packet: EapolPacket) -> int:
    return int.from_bytes(packet.replay_counter, "big") if packet.replay_counter else 0


class VerifyPskCampaign(VerifyCampaign):
    credential_type = CaptureType.WPA_PSK

    async def _exchange(self, iface, our_mac: bytes, our_mac_str: str) -> tuple[VerifyOutcome, Optional[str]]:
        ap = self.target
        bssid = ap.bssid.lower()
        trailer = force_psk_akm(ap.rsn_ie or GENERIC_RSN_IE) or GENERIC_RSN_IE
        association = Association(iface, bssid, ap.ssid or "", ap.channel, our_mac=our_mac,
                                  assoc_trailer_ies=trailer, should_stop=lambda: self.stopped)
        association.start()
        self.step = "associating"
        try:
            associated = await association.associate(attempts=2)
        finally:
            association.stop()
        if not associated:
            return VerifyOutcome.ERROR, association.fail_reason or "association rejected"
        self._log_step("associated")

        m1 = await self.array.next_frame(
            lambda p: (isinstance(p, EapolPacket) and p.dest == our_mac_str and p.bssid == bssid
                       and p.msg_num == 1 and p.nonce is not None),
            timeout=_M1_TIMEOUT_S)
        if m1 is None:
            return VerifyOutcome.ERROR, "AP never sent M1"

        replay = _replay_counter(m1)

        def answers_m2(p) -> bool:
            if p.dest != our_mac_str or p.bssid != bssid:
                return False
            if isinstance(p, DeauthPacket):
                return True
            if not isinstance(p, EapolPacket):
                return False
            if p.msg_num == 1 and p.nonce is not None and _replay_counter(p) > replay:
                return True
            if p.msg_num == 3:
                return True
            return False

        pmk = derive_pmk(self.value, (ap.ssid or "").encode())
        bssid_mac = str_to_mac(bssid)
        for attempt in range(_M2_ATTEMPTS):
            snonce = random_nonce()
            kck = derive_ptk(pmk, bssid_mac, our_mac, m1.nonce, snonce, KCK_LEN)
            key_descriptor_version = KeyDescriptorVersion(m1.key_info & KEY_DESCRIPTOR_VERSION_MASK)
            m2 = build_eapol_m2(bssid_mac, our_mac, snonce, kck, key_descriptor_version=key_descriptor_version,
                                replay=replay, rsn_ie=trailer)
            if not await iface.send_until_ack(m2, max_retries=_M2_RESENDS):
                return VerifyOutcome.ERROR, "AP never ACKed M2"
            self._log_step("M1 received, M2 sent")
            reply = await self.array.next_frame(answers_m2, timeout=_EAPOL_REPLY_TIMEOUT_S)
            if reply is None:
                return VerifyOutcome.ERROR, "no reply after M2"
            if isinstance(reply, DeauthPacket):
                self._log_step(f"{reply.type.capitalize()} received (reason {reply.reason})")
                # hostapd stops re-sending M1 at its retry limit and deauths instead
                if attempt > 0 and reply.reason == REASON_4WAY_HANDSHAKE_TIMEOUT:
                    return VerifyOutcome.REJECTED, None
                return VerifyOutcome.ERROR, f"AP sent {reply.type} (reason {reply.reason}) after M2"
            if reply.msg_num == 3:
                self._log_step("M3 received")
                return VerifyOutcome.VERIFIED, None
            m1, replay = reply, _replay_counter(reply)
        return VerifyOutcome.REJECTED, None
