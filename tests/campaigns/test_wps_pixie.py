"""Tests for native PixieWPS offline recovery."""

from wifit3.campaigns.wps import pins, pixie_prng
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


def test_static_secret_generator_survives_earlier_modes():
    pin = "01030365"
    e_s1 = bytes.fromhex("12" * wc.SECRET_NONCE_LEN)
    e_s2 = bytes.fromhex("34" * wc.SECRET_NONCE_LEN)
    bundle = _bundle(pin, e_s1, e_s2)
    candidates = ((e_s1, e_s2) for _ in range(1))

    result = recover_pin(
        bundle,
        modes=(PixieMode.NULL_SECRET, PixieMode.STATIC_SECRET),
        static_secrets=candidates,
    )

    assert result.found is True
    assert result.pin == pin
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

    result = recover_pin(bundle, modes=(PixieMode.NULL_SECRET,))

    assert result.pin == pin
    assert result.pin[4:] == "5670"


def test_second_half_recovery_accepts_a_wrong_checksum_pin():
    # "12345678"'s 8th digit should be 0, not 8; sweeping all 10000 second halves (not only the
    # 1000 checksum-valid ones) recovers factory PINs that ship with a wrong checksum digit.
    e_s1 = e_s2 = b"\x00" * wc.SECRET_NONCE_LEN
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

    assert result.found is True
    assert result.pin == "12345678"
    assert not wc.pin_is_valid(result.pin)


# ----- PRNG-seed modes + Phase 2 (end-to-end: synthesise a vulnerable M3, then recover) -----

def _bundle_for(pin, es1, es2, e_nonce):
    psk1, psk2 = wc.derive_psk(AUTHKEY, pin)
    return PixieBundle(
        pke=PKE, pkr=PKR,
        e_hash1=wc.e_or_r_hash(AUTHKEY, es1, psk1, PKE, PKR),
        e_hash2=wc.e_or_r_hash(AUTHKEY, es2, psk2, PKE, PKR),
        e_nonce=e_nonce, authkey=AUTHKEY, enrollee_mac=MAC,
    )


def test_ralink_mode_recovers_pin():
    pin = pins.full_pin("1357", "246")
    stream = pixie_prng.ralink_forward_stream(0x12345678, 48)
    bundle = _bundle_for(pin, stream[0:16], stream[16:32], stream[32:48])
    result = recover_pin(bundle, modes=(PixieMode.RALINK,))
    assert result.found and result.pin == pin and result.mode is PixieMode.RALINK


def test_ralink_mode_skips_non_ralink_nonce():
    pin = pins.full_pin("1357", "246")
    stream = pixie_prng.ralink_forward_stream(0x12345678, 48)
    bundle = _bundle_for(pin, stream[0:16], stream[16:32], b"\x01" * 16)
    assert recover_pin(bundle, modes=(PixieMode.RALINK,)).found is False


def test_rtl819x_mode_recovers_pin():
    pin = pins.full_pin("9753", "864")
    seed = 1700000000
    nonce = pixie_prng.glibc_nonce(seed)             # dist-0 enrollee: nonce == E-S1 == E-S2
    bundle = _bundle_for(pin, nonce, nonce, nonce)
    result = recover_pin(bundle, modes=(PixieMode.RTL819X,), rtl_window=(seed + 3, seed - 3))
    assert result.found and result.pin == pin and result.mode is PixieMode.RTL819X


def test_rtl819x_mode_recovers_pin_from_neighbouring_seeds():
    # The enrollee draws each secret a second or two after the nonce, so E-S1/E-S2 come from
    # seeds the search has to walk out to. A low first half keeps the 10^4 half-sweep cheap.
    pin = pins.full_pin("0001", "000")
    seed = 1700000000
    bundle = _bundle_for(pin, pixie_prng.glibc_nonce(seed + 1), pixie_prng.glibc_nonce(seed + 2),
                         pixie_prng.glibc_nonce(seed))
    result = recover_pin(bundle, modes=(PixieMode.RTL819X,), rtl_window=(seed + 2, seed - 2))
    assert result.found and result.pin == pin and result.mode is PixieMode.RTL819X


def test_rtl819x_mode_rejects_non_glibc_nonce():
    pin = pins.full_pin("9753", "864")
    bundle = _bundle_for(pin, b"\x00" * 16, b"\x00" * 16, b"\x80" + b"\x00" * 15)
    assert recover_pin(bundle, modes=(PixieMode.RTL819X,), rtl_window=(10, 0)).found is False


def test_rtl819x_mode_absent_when_seed_outside_window():
    pin = pins.full_pin("9753", "864")
    seed = 1700000000
    nonce = pixie_prng.glibc_nonce(seed)
    bundle = _bundle_for(pin, nonce, nonce, nonce)
    result = recover_pin(bundle, modes=(PixieMode.RTL819X,), rtl_window=(seed + 1000, seed + 900))
    assert result.found is False


def test_static_secret_nonce_reuse_recovers_pin():
    pin = pins.full_pin("2468", "135")
    nonce = bytes.fromhex("a1" * wc.SECRET_NONCE_LEN)
    bundle = _bundle_for(pin, nonce, nonce, nonce)
    result = recover_pin(bundle, modes=(PixieMode.STATIC_SECRET,))
    assert result.found and result.pin == pin and result.mode is PixieMode.STATIC_SECRET


def test_default_modes_recover_ralink_bundle():
    pin = pins.full_pin("1357", "246")
    stream = pixie_prng.ralink_forward_stream(0xABCDEF01, 48)
    bundle = _bundle_for(pin, stream[0:16], stream[16:32], stream[32:48])
    result = recover_pin(bundle)   # DEFAULT_MODES
    assert result.found and result.pin == pin and result.mode is PixieMode.RALINK


def test_ecos_simple_mode_recovers_pin():
    pin = pins.full_pin("8642", "097")
    seed = (0x05 << 25) | 137          # small low bits so the 25-bit sweep is fast in a test
    nonce, es1, es2 = pixie_prng.ecos_simple_model(seed)
    bundle = _bundle_for(pin, es1, es2, nonce)
    result = recover_pin(bundle, modes=(PixieMode.ECOS_SIMPLE,), ecos_max_counter=1000)
    assert result.found and result.pin == pin and result.mode is PixieMode.ECOS_SIMPLE


def test_empty_device_password_recovered():
    # A zero-length device password hashes the empty string into both PSK halves; neither the
    # 0000-9999 nor the 000-999 sweep would ever reach it, so it's checked ahead of each loop.
    null = b"\x00" * wc.SECRET_NONCE_LEN
    result = recover_pin(_bundle("", null, null), modes=(PixieMode.NULL_SECRET,))
    assert result.found is True
    assert result.pin == ""
    assert result.mode is PixieMode.NULL_SECRET


def test_rtl819x_carries_partial_first_half_when_second_half_seed_missed():
    # First half proved against E-Hash1, but the E-S2 seed lands past the +0..+9s second-half sweep:
    # the proved half must escape (found=False, first_half set) instead of being thrown away.
    seed = 1700000000
    pin = pins.full_pin("0001", "000")
    bundle = _bundle_for(
        pin,
        pixie_prng.glibc_nonce(seed + 1),        # E-S1: a second after the nonce (found by the sweep)
        pixie_prng.glibc_nonce(seed + 1 + 15),   # E-S2: beyond the 10s forward sweep from E-S1
        pixie_prng.glibc_nonce(seed),
    )
    result = recover_pin(bundle, modes=(PixieMode.RTL819X,), rtl_window=(seed + 2, seed - 2))
    assert result.found is False
    assert result.pin is None
    assert result.first_half == "0001"
    assert result.mode is PixieMode.RTL819X


def test_rtl819x_mode_recovers_pin_past_2038():
    # Seeds >= 2^31 (Unix time from Jan 2038) are negative in glibc's int32 state; recovery must
    # still reconstruct the nonce there, not just before the rollover.
    pin = pins.full_pin("9753", "864")
    seed = 2**31 + 100000
    nonce = pixie_prng.glibc_nonce(seed)
    bundle = _bundle_for(pin, nonce, nonce, nonce)
    result = recover_pin(bundle, modes=(PixieMode.RTL819X,), rtl_window=(seed + 3, seed - 3))
    assert result.found and result.pin == pin and result.mode is PixieMode.RTL819X
