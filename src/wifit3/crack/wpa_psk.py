"""WPA/WPA2-PSK checks for key versions 1-3, PMKIDs, and FT-PSK."""
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
_MESSAGE_PAIR_AP_LESS = 0x10
_MESSAGE_PAIR_UNSWAPPED_COUNTER = 0x20
_MESSAGE_PAIR_SWAPPED_COUNTER = 0x40
_MESSAGE_PAIR_NC = 0x80
_NONCE_ERROR_CORRECTIONS = 8


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
    message_pair = _message_pair_bits(line)
    if message_pair & 7 in _FT_MESSAGE_PAIRS_WITH_SNONCE_FIELD:
        snonce, anonce = field_nonce, frame_nonce
    else:
        snonce, anonce = frame_nonce, field_nonce
    pmk_r1 = derive_ft_pmk_r1(pmk_r0, r1khid, mac_sta)
    for corrected_anonce in _keyver3_anonces(anonce, message_pair):
        kck = derive_ft_ptk(pmk_r1, snonce, corrected_anonce, mac_ap, mac_sta, KCK_LEN)
        if hmac.compare_digest(eapol_mic_aes_cmac(kck, eapol), expected):
            return True
    return False


def _swap32(value: int) -> int:
    return int.from_bytes(value.to_bytes(4, "big"), "little")


def _keyver3_anonces(anonce: bytes, message_pair: int) -> list[bytes]:
    if not (message_pair & _MESSAGE_PAIR_NC) or message_pair & _MESSAGE_PAIR_AP_LESS:
        return [anonce]
    counter = anonce[-4:]
    origin = int.from_bytes(counter, "big")
    if message_pair & _MESSAGE_PAIR_UNSWAPPED_COUNTER:
        endian_swap = (False,)
    elif message_pair & _MESSAGE_PAIR_SWAPPED_COUNTER:
        endian_swap = (True,)
    else:
        endian_swap = (False, True)
    prefix = anonce[:-4]
    found = [anonce]
    seen = {counter}
    half = _NONCE_ERROR_CORRECTIONS // 2
    for extra_swap in endian_swap:
        for step in range(_NONCE_ERROR_CORRECTIONS + 1):
            delta = step - half
            if extra_swap:
                adjusted = (_swap32(origin) + delta) & 0xFFFFFFFF
                raw = _swap32(adjusted).to_bytes(4, "big")
            else:
                raw = ((origin + delta) & 0xFFFFFFFF).to_bytes(4, "big")
            if raw not in seen:
                seen.add(raw)
                found.append(prefix + raw)
    return found


def psk_matches(pmk: bytes, line: Hc22000Line) -> bool:
    """True if this PMK cracks the hashline (key version 1, 2 or 3, or FT-PSK).
    Key version 3 with message-pair bit 7 corrects the ANonce the way hashcat 8 does."""
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
        bits = _message_pair_bits(line)
        for corrected in _keyver3_anonces(anonce, bits):
            kck = derive_ptk_sha256(pmk, mac_ap, mac_sta, corrected, snonce, KCK_LEN)
            if hmac.compare_digest(eapol_mic_aes_cmac(kck, eapol), expected):
                return True
        return False
    return False
