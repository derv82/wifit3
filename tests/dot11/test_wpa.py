import hashlib
import hmac

import pytest

from wifit3.dot11.eapol import AES_128_CMAC, HMAC_MD5_RC4, HMAC_SHA1_AES, MIC_OFFSET
from wifit3.dot11.wpa import build_eapol_m2, derive_pmk, derive_ptk, is_valid_psk
from wifit3.dot11.parser import WlanFrameParser
from wifit3.dot11.packet import EapolPacket


def test_derive_ptk_length():
    """Verify PTK length is 64 bytes (512 bits)."""
    pmk = derive_pmk("password", b"IEEE")
    ap_mac = bytes.fromhex("000b86289b00")
    sta_mac = bytes.fromhex("001302d1e550")
    anonce = bytes(range(32))
    snonce = bytes(range(32, 64))
    ptk = derive_ptk(pmk, ap_mac, sta_mac, anonce, snonce, 64)
    assert len(ptk) == 64
    kck = ptk[:16]
    kek = ptk[16:32]
    tk = ptk[32:48]
    assert len(kck) == 16
    assert len(kek) == 16
    assert len(tk) == 16


def test_build_eapol_m2_parsed_correctly():
    """Verify built M2 frame parses as a valid EAPOL-Key frame."""
    bssid = bytes.fromhex("000b86289b00")
    sta_mac = bytes.fromhex("001302d1e550")
    snonce = bytes(range(32, 64))
    kck = b"\xaa" * 16

    frame = build_eapol_m2(bssid, sta_mac, snonce, kck, key_descriptor_version=HMAC_SHA1_AES, replay=1)
    assert len(frame) > 100

    parsed = WlanFrameParser.parse_80211_frame(frame, rssi=0)
    assert isinstance(parsed, EapolPacket)
    assert parsed.type == "eapol"
    assert parsed.to_ds is True
    assert parsed.bssid == "00:0b:86:28:9b:00"
    assert parsed.source == "00:13:02:d1:e5:50"
    assert parsed.dest == "00:0b:86:28:9b:00"
    assert parsed.nonce == snonce
    assert parsed.mic is not None
    assert len(parsed.mic) == 16


@pytest.mark.parametrize("key_descriptor_version, key_info, digest", [
    (HMAC_MD5_RC4, 0x0109, hashlib.md5),
    (HMAC_SHA1_AES, 0x010A, hashlib.sha1),
])
def test_build_eapol_m2_mic_follows_the_key_descriptor_version(key_descriptor_version, key_info, digest):
    kck = b"\xaa" * 16
    frame = build_eapol_m2(bytes.fromhex("000b86289b00"), bytes.fromhex("001302d1e550"), bytes(32), kck,
                           key_descriptor_version=key_descriptor_version)

    parsed = WlanFrameParser.parse_80211_frame(frame, rssi=0)
    payload = frame[24 + 8:]
    zeroed = payload[:MIC_OFFSET] + bytes(16) + payload[MIC_OFFSET + 16:]
    assert parsed.key_info == key_info
    assert parsed.mic == hmac.new(kck, zeroed, digest).digest()[:16]


def test_build_eapol_m2_rejects_an_unsupported_key_descriptor_version():
    with pytest.raises(ValueError):
        build_eapol_m2(bytes(6), bytes(6), bytes(32), b"\xaa" * 16, key_descriptor_version=AES_128_CMAC)


def test_is_valid_psk_length_bounds():
    assert not is_valid_psk("a" * 7)
    assert is_valid_psk("a" * 8)
    assert is_valid_psk("a" * 63)
    assert not is_valid_psk("a" * 64)


def test_is_valid_psk_counts_utf8_bytes():
    assert not is_valid_psk("é" * 32)
    assert is_valid_psk("é" * 31)
