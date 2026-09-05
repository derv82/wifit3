"""Tests for native PixieWPS offline recovery."""

from wifit3.campaigns.wps import pins
from wifit3.campaigns.wps.pixie import PixieBundle, PixieMode, recover_pin
from wifit3.dot11.wsc import crypto as wc


AUTHKEY = bytes.fromhex("11" * wc.AUTHKEY_LEN)
PKE = bytes.fromhex("22" * wc.PUBKEY_LEN)
PKR = bytes.fromhex("33" * wc.PUBKEY_LEN)
E_NONCE = bytes.fromhex("44" * wc.NONCE_LEN)
MAC = bytes.fromhex("aabbccddeeff")


def _bundle(pin: str, e_s1: bytes, e_s2: bytes) -> PixieBundle:
    psk1, psk2 = wc.derive_psk(AUTHKEY, pin)
    return PixieBundle(
        pke=PKE,
        pkr=PKR,
        e_hash1=wc.e_or_r_hash(AUTHKEY, e_s1, psk1, PKE, PKR),
        e_hash2=wc.e_or_r_hash(AUTHKEY, e_s2, psk2, PKE, PKR),
        e_nonce=E_NONCE,
        authkey=AUTHKEY,
        enrollee_mac=MAC,
    )


def test_null_secret_mode_recovers_pin():
    bundle = _bundle("12345670", b"\x00" * wc.SECRET_NONCE_LEN, b"\x00" * wc.SECRET_NONCE_LEN)

    result = recover_pin(bundle, modes=(PixieMode.NULL_SECRET,))

    assert result.found is True
    assert result.pin == "12345670"
    assert result.mode is PixieMode.NULL_SECRET


def test_static_secret_mode_recovers_pin_from_candidate_pair():
    e_s1 = bytes.fromhex("12" * wc.SECRET_NONCE_LEN)
    e_s2 = bytes.fromhex("34" * wc.SECRET_NONCE_LEN)
    bundle = _bundle("01030365", e_s1, e_s2)

    result = recover_pin(
        bundle,
        modes=(PixieMode.STATIC_SECRET,),
        static_secrets=[(bytes.fromhex("56" * wc.SECRET_NONCE_LEN), e_s2), (e_s1, e_s2)],
    )

    assert result.found is True
    assert result.pin == "01030365"
    assert result.mode is PixieMode.STATIC_SECRET


def test_recover_pin_returns_not_found_when_secret_does_not_match():
    bundle = _bundle("12345670", bytes.fromhex("12" * wc.SECRET_NONCE_LEN), bytes.fromhex("34" * wc.SECRET_NONCE_LEN))

    result = recover_pin(bundle, modes=(PixieMode.NULL_SECRET,))

    assert result.found is False
    assert result.pin is None
    assert result.mode is None


def test_second_half_recovery_uses_checksum_digit():
    first4 = "1234"
    pin = pins.full_pin(first4, "567")
    bundle = _bundle(pin, b"\x00" * wc.SECRET_NONCE_LEN, b"\x00" * wc.SECRET_NONCE_LEN)

    result = recover_pin(bundle, modes=("null-secret",))

    assert result.pin == pin
    assert result.pin[4:] == "5670"


def test_second_half_recovery_rejects_wrong_checksum_hash():
    e_s1 = b"\x00" * wc.SECRET_NONCE_LEN
    e_s2 = b"\x00" * wc.SECRET_NONCE_LEN
    psk1 = wc.hmac_sha256(AUTHKEY, b"1234")[:wc.PSK_LEN]
    psk2 = wc.hmac_sha256(AUTHKEY, b"5678")[:wc.PSK_LEN]
    bundle = PixieBundle(
        pke=PKE,
        pkr=PKR,
        e_hash1=wc.e_or_r_hash(AUTHKEY, e_s1, psk1, PKE, PKR),
        e_hash2=wc.e_or_r_hash(AUTHKEY, e_s2, psk2, PKE, PKR),
        e_nonce=E_NONCE,
        authkey=AUTHKEY,
    )

    result = recover_pin(bundle, modes=(PixieMode.NULL_SECRET,))

    assert result.found is False
