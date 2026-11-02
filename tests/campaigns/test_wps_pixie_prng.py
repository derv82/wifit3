"""Known-answer tests for the PixieDust weak-PRNG module (campaigns/wps/pixie_prng.py).

Vectors in data/pixie_prng_vectors.json come from literal reference implementations of glibc's
TYPE_3 additive generator, the Ralink Galois LFSR and the eCos LCG (see the fixture's
_provenance), anchored to the canonical glibc srandom(1) sequence 1804289383, 846930886,
1681692777, 1714636915.
"""

import json
import struct
from pathlib import Path

import pytest

from wifit3.campaigns.wps import pixie_prng as P

_VEC = json.loads((Path(__file__).parent / "data" / "pixie_prng_vectors.json").read_text())


@pytest.mark.parametrize("seed, want", _VEC["glibc_nonce"].items())
def test_glibc_nonce_matches_reference(seed, want):
    assert P.glibc_nonce(int(seed)).hex() == want


@pytest.mark.parametrize("seed, want", _VEC["glibc_first_word"].items())
def test_glibc_first_word_matches_reference(seed, want):
    assert P.glibc_first_word(int(seed)) == want


def test_glibc_nonce_reproduces_canonical_srandom1():
    # glibc `srandom(1); random()` → 1804289383, 846930886, 1681692777, 1714636915.
    words = struct.unpack(">IIII", P.glibc_nonce(1))
    assert words == (1804289383, 846930886, 1681692777, 1714636915)


@pytest.mark.parametrize("sreg, want", _VEC["ralink_forward"].items())
def test_ralink_forward_matches_reference(sreg, want):
    assert P.ralink_forward_stream(int(sreg), 48).hex() == want


@pytest.mark.parametrize("sreg, rec", _VEC["ralink_recover"].items())
def test_ralink_recover_matches_reference(sreg, rec):
    forward = bytes.fromhex(_VEC["ralink_forward"][sreg])
    e_nonce = forward[32:48]
    out = P.ralink_recover(e_nonce)
    assert out is not None
    es1, es2 = out
    assert es1.hex() == rec["es1"]
    assert es2.hex() == rec["es2"]
    # The two secrets precede the nonce in the forward stream.
    assert es1 == forward[0:16]
    assert es2 == forward[16:32]


def test_ralink_recover_rejects_impossible_nonce():
    # Corrupting an early byte of a genuine LFSR run breaks the forward re-check (the restored
    # 32-bit state is fixed by the final bytes, so byte 0 no longer regenerates).
    valid = P.ralink_forward_stream(0xCAFEBABE, 48)[32:48]
    corrupt = bytes([valid[0] ^ 0xFF]) + valid[1:]
    assert P.ralink_recover(corrupt) is None


def test_find_glibc_nonce_seed_roundtrips_in_window():
    seed = 1700000000
    nonce = P.glibc_nonce(seed)
    assert P.find_glibc_nonce_seed(nonce, seed + 5, seed - 5) == seed


def test_find_glibc_nonce_seed_absent_outside_window():
    seed = 1700000000
    nonce = P.glibc_nonce(seed)
    assert P.find_glibc_nonce_seed(nonce, seed + 100, seed + 50) is None


@pytest.mark.parametrize("seed, want", _VEC["ecos_rand_stream"].items())
def test_ecos_rand_stream_matches_reference(seed, want):
    s = int(seed)
    out = bytearray()
    for _ in range(48):
        v, s = P.ecos_rand(s)
        out.append(v & 0xFF)
    assert out.hex() == want


def test_ecos_simple_model_recover_roundtrip():
    seed = (0x05 << 25) | 137          # small low 25 bits so the sweep finishes fast
    nonce, es1, es2 = P.ecos_simple_model(seed)
    assert P.ecos_simple_recover(nonce, max_counter=1000) == (es1, es2)


def test_glibc_nonce_matches_glibc_past_2038():
    # Seeds >= 2^31 (Unix time from Jan 2038) are negative in glibc's int32_t state. Ground truth
    # from the system glibc via ctypes srandom()/random(); before the fix 2**31 gave 2b101346...
    assert P.glibc_nonce(2**31).hex() == "4fad0d5d48255ae056402720141c32ff"
    assert P.glibc_nonce(2**31 + 1).hex() == "407b3221004fa414406d7d184094741d"
    assert P.glibc_nonce(2**32 - 1).hex() == "0f31db3b47d5b6cc15d2aa085387bee1"


def test_glibc_nonce_treats_seed_as_srandom_unsigned_int():
    # srandom() takes an unsigned int, so a Python-negative seed wraps mod 2^32 and 0 becomes 1.
    assert P.glibc_nonce(-1) == P.glibc_nonce(2**32 - 1)
    assert P.glibc_nonce(0) == P.glibc_nonce(1)
