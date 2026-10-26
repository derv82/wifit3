"""WPA/WPA2-PSK candidate checks against captured MICs and PMKIDs (no I/O).

EAPOL key descriptor versions 1 (HMAC-MD5), 2 (HMAC-SHA1) and 3 (AES-CMAC, PSK-SHA256 and FT-PSK);
PMKIDs in HMAC-SHA1, HMAC-SHA256 when message-pair bit 0x02 is set (hcxpcapngtool writes ``03``),
or FT PMKR1Name. No nonce error correction.
"""
import hashlib
import hmac

from wifit3.crack.hc22000_format import PMKID_SHA256_MESSAGE_PAIR_BIT, Hc22000Line
from wifit3.dot11.wpa import (
    KCK_LEN,
    derive_ft_pmk_r0,
    derive_ft_pmk_r1,
    derive_ft_ptk,
    derive_pmk,
    derive_ptk,
    derive_ptk_sha256,
    eapol_mic,
    eapol_mic_aes_cmac,
    eapol_mic_hmac_md5,
    ft_pmk_r1_name,
)

_KEY_INFO_OFFSET = 5
_SNONCE_OFFSET = 17
_NONCE_LEN = 32
# hcxpcapngtool's FT message pairs 3 and 4 hold the SNonce in the ANONCE field (module_22000.c).
_FT_MESSAGE_PAIRS_WITH_SNONCE_FIELD = frozenset({3, 4})


def mic_for(psk: str, ssid: bytes, aa: bytes, spa: bytes, anonce: bytes, snonce: bytes,
            eapol_payload_mic_zeroed: bytes) -> bytes:
    """The M2 MIC a client with this PSK would produce over these EAPOL bytes."""
    kck = derive_ptk(derive_pmk(psk, ssid), aa, spa, anonce, snonce, KCK_LEN)
    return eapol_mic(kck, eapol_payload_mic_zeroed)


def pmkid(pmk: bytes, aa: bytes, spa: bytes) -> bytes:
    """AKM 2 PMKID: HMAC-SHA1-128(PMK, ``PMK Name`` || AP MAC || STA MAC)."""
    return hmac.new(pmk, b"PMK Name" + aa + spa, hashlib.sha1).digest()[:16]


def sha256_pmkid(pmk: bytes, aa: bytes, spa: bytes) -> bytes:
    """AKM 6 PMKID: HMAC-SHA256-128 of the same input. Hashcat 8 reads it from message pair ``03``."""
    return hmac.new(pmk, b"PMK Name" + aa + spa, hashlib.sha256).digest()[:16]


def _message_pair_bits(line: Hc22000Line) -> int:
    try:
        return int(line.message_pair, 16) if len(line.message_pair) == 2 else 0
    except ValueError:
        return 0


def _key_descriptor_version(eapol: bytes) -> int:
    return int.from_bytes(eapol[_KEY_INFO_OFFSET:_KEY_INFO_OFFSET + 2], "big") & 3


def _ft_psk_matches(pmk: bytes, line: Hc22000Line) -> bool:
    """FT-PSK (``WPA*03`` PMKR1Name / ``WPA*04`` MIC) check, as module_22000.c derives it."""
    mac_ap, mac_sta = bytes.fromhex(line.mac_ap), bytes.fromhex(line.mac_sta)
    expected = bytes.fromhex(line.pmkid_or_mic)
    r1khid = bytes.fromhex(line.r1khid)
    pmk_r0, pmk_r0_name = derive_ft_pmk_r0(pmk, bytes.fromhex(line.essid), bytes.fromhex(line.mdid),
                                           bytes.fromhex(line.r0khid), mac_sta)
    if line.kind == "03":
        return hmac.compare_digest(ft_pmk_r1_name(pmk_r0_name, r1khid, mac_sta), expected)
    eapol = bytes.fromhex(line.eapol)
    if _key_descriptor_version(eapol) != 3:
        return False
    frame_nonce = eapol[_SNONCE_OFFSET:_SNONCE_OFFSET + _NONCE_LEN]
    field_nonce = bytes.fromhex(line.anonce)
    if _message_pair_bits(line) & 7 in _FT_MESSAGE_PAIRS_WITH_SNONCE_FIELD:
        snonce, anonce = field_nonce, frame_nonce
    else:
        snonce, anonce = frame_nonce, field_nonce
    pmk_r1 = derive_ft_pmk_r1(pmk_r0, r1khid, mac_sta)
    kck = derive_ft_ptk(pmk_r1, snonce, anonce, mac_ap, mac_sta, KCK_LEN)
    return hmac.compare_digest(eapol_mic_aes_cmac(kck, eapol), expected)


def psk_matches(pmk: bytes, line: Hc22000Line) -> bool:
    """True if this PMK cracks the hashline (key descriptor version 1, 2 or 3, or FT-PSK)."""
    if line.kind in ("03", "04"):
        return _ft_psk_matches(pmk, line)
    mac_ap, mac_sta = bytes.fromhex(line.mac_ap), bytes.fromhex(line.mac_sta)
    expected = bytes.fromhex(line.pmkid_or_mic)
    if line.kind == "01":
        if _message_pair_bits(line) & PMKID_SHA256_MESSAGE_PAIR_BIT:
            return hmac.compare_digest(sha256_pmkid(pmk, mac_ap, mac_sta), expected)
        return hmac.compare_digest(pmkid(pmk, mac_ap, mac_sta), expected)
    if line.kind != "02":
        return False
    eapol = bytes.fromhex(line.eapol)
    key_descriptor_version = _key_descriptor_version(eapol)
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
