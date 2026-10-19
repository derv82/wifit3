"""WPA/WPA2-PSK candidate checks against captured MICs and PMKIDs (no I/O).

EAPOL key descriptor versions 1 (HMAC-MD5), 2 (HMAC-SHA1) and 3 (AES-CMAC, PSK-SHA256); PMKIDs
in HMAC-SHA1 or, when the message-pair field flags it, HMAC-SHA256. No nonce error correction.
"""
import hashlib
import hmac

from wifit3.crack.hc22000_format import PMKID_SHA256_MESSAGE_PAIR_BIT, Hc22000Line
from wifit3.dot11.wpa import (
    KCK_LEN,
    derive_pmk,
    derive_ptk,
    derive_ptk_sha256,
    eapol_mic,
    eapol_mic_aes_cmac,
    eapol_mic_hmac_md5,
)

_KEY_INFO_OFFSET = 5
_SNONCE_OFFSET = 17
_NONCE_LEN = 32


def mic_for(psk: str, ssid: bytes, aa: bytes, spa: bytes, anonce: bytes, snonce: bytes,
            eapol_payload_mic_zeroed: bytes) -> bytes:
    """The M2 MIC a client with this PSK would produce over these EAPOL bytes."""
    kck = derive_ptk(derive_pmk(psk, ssid), aa, spa, anonce, snonce, KCK_LEN)
    return eapol_mic(kck, eapol_payload_mic_zeroed)


def pmkid(pmk: bytes, aa: bytes, spa: bytes) -> bytes:
    return hmac.new(pmk, b"PMK Name" + aa + spa, hashlib.sha1).digest()[:16]


def line_is_verifiable(line: Hc22000Line) -> bool:
    if line.kind == "01":
        return True
    if line.kind != "02":
        return False
    try:
        eapol = bytes.fromhex(line.eapol)
    except ValueError:
        return False
    if len(eapol) < _KEY_INFO_OFFSET + 2:
        return False
    key_info = int.from_bytes(eapol[_KEY_INFO_OFFSET:_KEY_INFO_OFFSET + 2], "big")
    return key_info & 7 == _KEY_DESCRIPTOR_VERSION_HMAC_SHA1
def _message_pair_bits(line: Hc22000Line) -> int:
    try:
        return int(line.message_pair, 16) if len(line.message_pair) == 2 else 0
    except ValueError:
        return 0


def psk_matches(pmk: bytes, line: Hc22000Line) -> bool:
    """True if this PMK cracks the hashline (key descriptor version 1, 2 or 3)."""
    mac_ap, mac_sta = bytes.fromhex(line.mac_ap), bytes.fromhex(line.mac_sta)
    expected = bytes.fromhex(line.pmkid_or_mic)
    if line.kind == "01":
        if _message_pair_bits(line) & PMKID_SHA256_MESSAGE_PAIR_BIT:
            digest = hmac.new(pmk, b"PMK Name" + mac_ap + mac_sta, hashlib.sha256).digest()
            return hmac.compare_digest(digest[:16], expected)
        return hmac.compare_digest(pmkid(pmk, mac_ap, mac_sta), expected)
    if not line_is_verifiable(line):
        return False
    eapol = bytes.fromhex(line.eapol)
    key_info = int.from_bytes(eapol[_KEY_INFO_OFFSET:_KEY_INFO_OFFSET + 2], "big")
    key_descriptor_version = key_info & 3
    snonce = eapol[_SNONCE_OFFSET:_SNONCE_OFFSET + _NONCE_LEN]
    anonce = bytes.fromhex(line.anonce)
    if key_descriptor_version == 1:
        kck = derive_ptk(pmk, mac_ap, mac_sta, anonce, snonce, KCK_LEN)
        return hmac.compare_digest(eapol_mic_hmac_md5(kck, eapol), expected)
    if key_descriptor_version == 2:
        kck = derive_ptk(pmk, mac_ap, mac_sta, anonce, snonce, KCK_LEN)
        return hmac.compare_digest(eapol_mic(kck, eapol), expected)
    if key_descriptor_version == 3:
        kck = derive_ptk_sha256(pmk, mac_ap, mac_sta, anonce, snonce, KCK_LEN)
        return hmac.compare_digest(eapol_mic_aes_cmac(kck, eapol), expected)
    return False
