"""AES-128 (FIPS-197 Appendix C.1) and AES-CMAC (RFC 4493 section 4) vectors."""
from wifit3.dot11.aes_cmac import aes128_encrypt, aes_cmac

_CMAC_KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
_CMAC_MESSAGE = bytes.fromhex(
    "6bc1bee22e409f96e93d7e117393172a"
    "ae2d8a571e03ac9c9eb76fac45af8e51"
    "30c81c46a35ce411e5fbc1191a0a52ef"
    "f69f2445df4f9b17ad2b417be66c3710")


def test_aes128_fips197_appendix_c1():
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    plaintext = bytes.fromhex("00112233445566778899aabbccddeeff")
    assert aes128_encrypt(key, plaintext) == bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")


def test_cmac_rfc4493_empty_message():
    assert aes_cmac(_CMAC_KEY, b"") == bytes.fromhex("bb1d6929e95937287fa37d129b756746")


def test_cmac_rfc4493_16_bytes():
    assert aes_cmac(_CMAC_KEY, _CMAC_MESSAGE[:16]) == bytes.fromhex("070a16b46b4d4144f79bdd9dd04a287c")


def test_cmac_rfc4493_40_bytes():
    assert aes_cmac(_CMAC_KEY, _CMAC_MESSAGE[:40]) == bytes.fromhex("dfa66747de9ae63030ca32611497c827")


def test_cmac_rfc4493_64_bytes():
    assert aes_cmac(_CMAC_KEY, _CMAC_MESSAGE) == bytes.fromhex("51f0bebf7e3b9d92fc49741779363cfe")
