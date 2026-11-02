"""VerifyWepCampaign: the key decrypts the AP's data to an LLC/SNAP header, heard passively or
after the AP relays an ARP request we encrypted with it."""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from wifit3.campaigns.auth_assoc import Association
from wifit3.crack.wep import rc4_keystream
from wifit3.dot11 import str_to_mac
from wifit3.dot11.mac import mac_header
from wifit3.dot11.packet import WepDataPacket
from wifit3.dot11.wep.crypto import arp_request_plaintext, wep_encrypt

from .campaign import VerifyCampaign, VerifyOutcome

_LLC_SNAP_HEADER = b"\xaa\xaa\x03\x00\x00\x00"
_PASSIVE_LISTEN_S = 0.5
_RELAY_LISTEN_S = 2.5
_ARP_SENDS = 3


class VerifyWepCampaign(VerifyCampaign):
    credential_name = "WEP key"

    async def _exchange(self, iface, our_mac: bytes, our_mac_str: str) -> tuple[VerifyOutcome, Optional[str]]:
        ap = self.target
        bssid = ap.bssid.lower()
        try:
            key = bytes.fromhex(self.value)
        except ValueError:
            key = self.value.encode("ascii")

        def decrypts(p) -> bool:
            if not (isinstance(p, WepDataPacket) and p.bssid == bssid and p.iv and p.cipher):
                return False
            if len(p.cipher) < len(_LLC_SNAP_HEADER):
                return False
            keystream = rc4_keystream(p.iv + key, len(_LLC_SNAP_HEADER))
            return bytes(c ^ k for c, k in zip(p.cipher, keystream)) == _LLC_SNAP_HEADER

        self._log_step("listening for WEP data")
        if await self.array.next_frame(decrypts, timeout=_PASSIVE_LISTEN_S) is not None:
            self._log_step("WEP data decrypted")
            return VerifyOutcome.VERIFIED, None

        association = Association(iface, bssid, ap.ssid or "", ap.channel, our_mac=our_mac,
                                  should_stop=lambda: self.stopped)
        association.start()
        self._log_step("auth/assoc sent")
        try:
            associated = await association.associate(attempts=2)
        finally:
            association.stop()
        if not associated:
            return VerifyOutcome.ERROR, association.fail_reason or "association rejected"

        plaintext = arp_request_plaintext(sender_mac=our_mac,
                                          sender_ip=b"\xc0\xa8\x01\xfe",
                                          target_ip=b"\xc0\xa8\x01\x01")
        iv = os.urandom(3)
        keystream = rc4_keystream(iv + key, len(plaintext) + 4)
        # ToDS + Protected data to broadcast, through the AP
        header = mac_header(b"\x08\x41", str_to_mac(bssid), our_mac, b"\xff\xff\xff\xff\xff\xff")
        frame = header + iv + b"\x00" + wep_encrypt(keystream, plaintext)
        relayed = asyncio.create_task(self.array.next_frame(decrypts, timeout=_RELAY_LISTEN_S))
        for _ in range(_ARP_SENDS):
            await iface.send_no_wait(frame)
            await asyncio.sleep(0.1)
        self._log_step("encrypted ARP request sent")
        if await relayed is not None:
            self._log_step("relayed ARP decrypted")
            return VerifyOutcome.VERIFIED, None
        return VerifyOutcome.REJECTED, None
