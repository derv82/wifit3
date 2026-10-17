"""WPA/WPA2-PSK candidate checks against captured MICs and PMKIDs (no I/O).

Key descriptor version 2 (plain PSK, HMAC-SHA1) only. Version 3 (PSK-SHA256, AES-CMAC) is not
implemented: the FakeAP twin advertises plain PSK.
"""
import hashlib
import hmac

from wifit3.crack.hc22000_format import Hc22000Line
from wifit3.dot11.wpa import KCK_LEN, derive_pmk, derive_ptk, eapol_mic

_KEY_INFO_OFFSET = 5
_SNONCE_OFFSET = 17
_NONCE_LEN = 32
_KEY_DESCRIPTOR_VERSION_HMAC_SHA1 = 2


def mic_for(psk: str, ssid: bytes, aa: bytes, spa: bytes, anonce: bytes, snonce: bytes,
            eapol_payload_mic_zeroed: bytes) -> bytes:
    """The M2 MIC a client with this PSK would produce over these EAPOL bytes."""
    kck = derive_ptk(derive_pmk(psk, ssid), aa, spa, anonce, snonce, KCK_LEN)
    return eapol_mic(kck, eapol_payload_mic_zeroed)


def pmkid(pmk: bytes, aa: bytes, spa: bytes) -> bytes:
    return hmac.new(pmk, b"PMK Name" + aa + spa, hashlib.sha1).digest()[:16]


def psk_matches(pmk: bytes, line: Hc22000Line) -> bool:
    """True if this PMK cracks the hashline. EAPOL lines: key descriptor version 2 only, no nonce
    error correction."""
    mac_ap, mac_sta = bytes.fromhex(line.mac_ap), bytes.fromhex(line.mac_sta)
    expected = bytes.fromhex(line.pmkid_or_mic)
    if line.kind == "01":
        return hmac.compare_digest(pmkid(pmk, mac_ap, mac_sta), expected)
    if line.kind != "02":
        return False
    eapol = bytes.fromhex(line.eapol)
    key_info = int.from_bytes(eapol[_KEY_INFO_OFFSET:_KEY_INFO_OFFSET + 2], "big")
    if key_info & 7 != _KEY_DESCRIPTOR_VERSION_HMAC_SHA1:
        return False
    snonce = eapol[_SNONCE_OFFSET:_SNONCE_OFFSET + _NONCE_LEN]
    kck = derive_ptk(pmk, mac_ap, mac_sta, bytes.fromhex(line.anonce), snonce, KCK_LEN)
    return hmac.compare_digest(eapol_mic(kck, eapol), expected)
