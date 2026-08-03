"""Offline tests for the WSC crypto core.

The low-level primitives are anchored to PUBLISHED known-answer vectors
(FIPS-197 for the AES block, NIST SP800-38A F.2.1 for AES-CBC), so the AES
port is validated independently, not just against itself. The higher-level
WSC derivations are checked for self-consistency (build a hash, recover it);
their on-air correctness is proven by scripts/diag/wps/wps_probe.py against a real AP.
"""

from wifit3.dot11.wsc import crypto as wc


# ---- AES-128 block: FIPS-197 Appendix C.1 ---------------------------------
def test_aes128_block_fips197():
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    ct = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    ks = wc._expand_key(key)
    assert wc._aes128_encrypt_block(ks, pt) == ct
    assert wc._aes128_decrypt_block(ks, ct) == pt


# ---- AES-128-CBC: NIST SP800-38A F.2.1 (first block) ----------------------
def test_aes128_cbc_nist():
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    ct = bytes.fromhex("7649abac8119b246cee98e9b12e9197d")
    assert wc.aes128_cbc_encrypt(key, iv, pt) == ct
    assert wc.aes128_cbc_decrypt(key, iv, ct) == pt


def test_aes128_cbc_multiblock_roundtrip():
    key = b"\x11" * 16
    iv = b"\x22" * 16
    pt = bytes(range(48))
    ct = wc.aes128_cbc_encrypt(key, iv, pt)
    assert len(ct) == 48
    assert wc.aes128_cbc_decrypt(key, iv, ct) == pt


def test_pkcs5_roundtrip():
    for n in range(0, 40):
        data = bytes(range(n))
        padded = wc.pkcs5_pad(data)
        assert len(padded) % 16 == 0 and len(padded) > len(data)
        assert wc.pkcs5_unpad(padded) == data


# ---- Diffie-Hellman MODP group 5 ------------------------------------------
def test_dh_shared_secret_agrees():
    a_priv, a_pub = wc.dh_generate_keypair()
    b_priv, b_pub = wc.dh_generate_keypair()
    assert len(a_pub) == wc.PUBKEY_LEN
    assert wc.dh_shared_secret(b_pub, a_priv) == wc.dh_shared_secret(a_pub, b_priv)


# ---- KDF shape ------------------------------------------------------------
def test_wps_kdf_length_and_determinism():
    key = b"\x01" * 32
    out = wc.wps_kdf(key, 80)
    assert len(out) == 80
    assert wc.wps_kdf(key, 80) == out          # deterministic
    assert wc.wps_kdf(key, 80) != wc.wps_kdf(b"\x02" * 32, 80)


# ---- Full key derivation + split-PIN derivation self-consistency ----------
def test_derive_keys_and_pin_halves():
    # Two synthetic peers complete DH; both must derive the same AuthKey.
    e_priv, pke = wc.dh_generate_keypair()
    r_priv, pkr = wc.dh_generate_keypair()
    nonce_e = b"\xAA" * 16
    nonce_r = b"\xBB" * 16
    mac_e = bytes.fromhex("020102030405")

    shared_r = wc.dh_shared_secret(pke, r_priv)
    shared_e = wc.dh_shared_secret(pkr, e_priv)
    assert shared_r == shared_e

    authkey, kwk, emsk = wc.derive_keys(shared_r, nonce_e, mac_e, nonce_r)
    assert len(authkey) == 32 and len(kwk) == 16 and len(emsk) == 32

    # Enrollee commits E-Hash1/2 over its secret nonces for the real PIN.
    pin = "12345670"                            # valid checksum; not a real cred
    psk1, psk2 = wc.derive_psk(authkey, pin)
    es1, es2 = b"\x11" * 16, b"\x22" * 16
    ehash1 = wc.e_or_r_hash(authkey, es1, psk1, pke, pkr)
    ehash2 = wc.e_or_r_hash(authkey, es2, psk2, pke, pkr)

    # PixieWPS verify primitive: the true halves match, wrong halves don't.
    assert wc.check_pin_half(authkey, es1, ehash1, b"1234", pke, pkr)
    assert wc.check_pin_half(authkey, es2, ehash2, b"5670", pke, pkr)
    assert not wc.check_pin_half(authkey, es1, ehash1, b"9999", pke, pkr)


# ---- Encrypted-settings round-trip (KeyWrapKey + KWA) ---------------------
def test_encrypted_settings_roundtrip():
    authkey = b"\x33" * 32
    kwk = b"\x44" * 16
    inner = b"\x10\x3f\x00\x10" + b"\x55" * 16        # an R-SNonce1 TLV
    kwa = wc.key_wrap_authenticator(authkey, inner)
    plain = inner + b"\x10\x1e\x00\x08" + kwa
    iv = b"\x66" * 16
    ct = wc.aes128_cbc_encrypt(kwk, iv, wc.pkcs5_pad(plain))
    dec = wc.pkcs5_unpad(wc.aes128_cbc_decrypt(kwk, iv, ct))
    assert dec == plain


# ---- PIN checksum ---------------------------------------------------------
def test_pin_checksum():
    assert wc.pin_checksum(1234567) == 0
    assert wc.pin_is_valid("12345670")
    assert not wc.pin_is_valid("12345671")
    assert not wc.pin_is_valid("1234567")          # too short
