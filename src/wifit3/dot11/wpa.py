"""WPA/WPA2-PSK 4-way handshake key derivation and EAPOL-M2 frame building.

Pure 802.11i specification functions: no I/O, no hardware.
"""
from __future__ import annotations

import hashlib
import hmac
import os

from wifit3.dot11.aes_cmac import aes_cmac
from wifit3.dot11.eapol import (
    LLC_SNAP_EAPOL,
    data_header,
    eapol_key,
    set_mic,
)

# Key Info bits for Supplicant Message 2 (M2):
# Bit 0-2: Key Descriptor Version = 2 (HMAC-SHA1 MIC / AES-128-CCMP)
# Bit 3: Key Type = 1 (Pairwise)
# Bit 8: MIC bit set (1)
_M2_KEY_INFO = 0x010A
KCK_LEN = 16
_PTK_SHA256_BITS = 384
_FT_R0_KEY_DATA_BITS = 384  # PMK-R0 (256) + PMK-R0Name-Salt (128)
_FT_PMK_R1_BITS = 256


def derive_pmk(passphrase: str, ssid: bytes) -> bytes:
    """802.11i PSK passphrase to PMK: PBKDF2-HMAC-SHA1(passphrase, ssid, 4096, 32)."""
    return hashlib.pbkdf2_hmac("sha1", passphrase.encode(), ssid, 4096, 32)


def prf(key: bytes, label: bytes, data: bytes, nbytes: int) -> bytes:
    """802.11i PRF-n (HMAC-SHA1), truncated to nbytes."""
    out = b""
    i = 0
    while len(out) < nbytes:
        out += hmac.new(key, label + b"\x00" + data + bytes([i]), hashlib.sha1).digest()
        i += 1
    return out[:nbytes]


def derive_ptk(pmk: bytes, ap_mac: bytes, sta_mac: bytes, anonce: bytes, snonce: bytes,
               nbytes: int) -> bytes:
    """First nbytes of the PTK; MAC and nonce pairs enter sorted. KCK is PTK[:KCK_LEN]."""
    mac_block = min(ap_mac, sta_mac) + max(ap_mac, sta_mac)
    nonce_block = min(anonce, snonce) + max(anonce, snonce)
    return prf(pmk, b"Pairwise key expansion", mac_block + nonce_block, nbytes)


def derive_ptk_sha256(pmk: bytes, ap_mac: bytes, sta_mac: bytes, anonce: bytes, snonce: bytes,
                      nbytes: int) -> bytes:
    """First nbytes of the 384-bit KDF-SHA256 PTK (AKM 6, PSK-SHA256); pairs enter sorted."""
    context = (min(ap_mac, sta_mac) + max(ap_mac, sta_mac)
               + min(anonce, snonce) + max(anonce, snonce))
    return kdf_sha256(pmk, b"Pairwise key expansion", context, _PTK_SHA256_BITS)[:nbytes]


def kdf_sha256(key: bytes, label: bytes, context: bytes, length_bits: int) -> bytes:
    """802.11 KDF-Hash-Length over HMAC-SHA256 (12.7.1.7.2), whole bytes only."""
    length = length_bits.to_bytes(2, "little")
    out = b""
    i = 1
    while len(out) * 8 < length_bits:
        out += hmac.new(key, i.to_bytes(2, "little") + label + context + length,
                        hashlib.sha256).digest()
        i += 1
    return out[:length_bits // 8]


def derive_ft_pmk_r0(pmk: bytes, ssid: bytes, mdid: bytes, r0khid: bytes,
                     sta_mac: bytes) -> tuple[bytes, bytes]:
    """FT-PSK (AKM 4) (PMK-R0, PMKR0Name) from the PMK (XXKey)."""
    context = bytes([len(ssid)]) + ssid + mdid + bytes([len(r0khid)]) + r0khid + sta_mac
    r0_key_data = kdf_sha256(pmk, b"FT-R0", context, _FT_R0_KEY_DATA_BITS)
    pmk_r0_name = hashlib.sha256(b"FT-R0N" + r0_key_data[32:48]).digest()[:16]
    return r0_key_data[:32], pmk_r0_name


def derive_ft_pmk_r1(pmk_r0: bytes, r1khid: bytes, sta_mac: bytes) -> bytes:
    return kdf_sha256(pmk_r0, b"FT-R1", r1khid + sta_mac, _FT_PMK_R1_BITS)


def ft_pmk_r1_name(pmk_r0_name: bytes, r1khid: bytes, sta_mac: bytes) -> bytes:
    """PMKR1Name, the PMKID an FT-PSK association carries."""
    return hashlib.sha256(b"FT-R1N" + pmk_r0_name + r1khid + sta_mac).digest()[:16]


def derive_ft_ptk(pmk_r1: bytes, snonce: bytes, anonce: bytes, ap_mac: bytes, sta_mac: bytes,
                  nbytes: int) -> bytes:
    """First nbytes of the FT PTK; unlike the 4-way PTK, nonces and MACs enter in fixed order."""
    context = snonce + anonce + ap_mac + sta_mac
    return kdf_sha256(pmk_r1, b"FT-PTK", context, _PTK_SHA256_BITS)[:nbytes]


def eapol_mic(kck: bytes, eapol_payload_mic_zeroed: bytes) -> bytes:
    """Key descriptor version 2 MIC: HMAC-SHA1-128 over the EAPOL payload with its MIC zeroed."""
    return hmac.new(kck, eapol_payload_mic_zeroed, hashlib.sha1).digest()[:16]


def eapol_mic_hmac_md5(kck: bytes, eapol_payload_mic_zeroed: bytes) -> bytes:
    """Key descriptor version 1 MIC: HMAC-MD5 over the EAPOL payload with its MIC zeroed."""
    return hmac.new(kck, eapol_payload_mic_zeroed, hashlib.md5).digest()


def eapol_mic_aes_cmac(kck: bytes, eapol_payload_mic_zeroed: bytes) -> bytes:
    """Key descriptor version 3 MIC: AES-128-CMAC over the EAPOL payload with its MIC zeroed."""
    return aes_cmac(kck, eapol_payload_mic_zeroed)


def build_eapol_m2(
    bssid: bytes,
    sta_mac: bytes,
    snonce: bytes,
    kck: bytes,
    replay: int = 1,
    rsn_ie: bytes = b"",
) -> bytes:
    """Construct a full 802.11 Data frame carrying EAPOL-Key Message 2 with MIC."""
    hdr = data_header(to_ds=True, bssid=bssid, client=sta_mac)
    raw_payload = eapol_key(
        key_info=_M2_KEY_INFO,
        key_len=0,
        replay=replay,
        nonce=snonce,
        key_data=rsn_ie,
        mic=bytes(16),
    )
    return hdr + LLC_SNAP_EAPOL + set_mic(raw_payload, eapol_mic(kck, raw_payload))


def random_nonce() -> bytes:
    """Generate a random 32-byte SNonce."""
    return os.urandom(32)
