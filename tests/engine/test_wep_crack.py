"""WEP cracker tests — pure software, no hardware.

Proves the native PTW cracker recovers a known key from synthetic WEP packets:
generate packets under a chosen key (random IVs, known ARP plaintext), feed the
(IV, keystream) pairs, assert the cracker returns the key.
"""
import os
import random

import pytest

from wifit3.engine.attacks.wep.crack import (
    ARP_REQUEST_PLAINTEXT,
    PlaceholderCracker,
    PtwCracker,
    WepCracker,
    keystream_from_arp_cipher,
    rc4_keystream,
)


# ---- RC4 known-answer -------------------------------------------------------

def test_rc4_known_answer():
    # Classic test vector: RC4("Key") ⊕ "Plaintext" = BBF316E8D940AF0AD3.
    ks = rc4_keystream(b"Key", len(b"Plaintext"))
    ct = bytes(a ^ b for a, b in zip(ks, b"Plaintext"))
    assert ct == bytes.fromhex("BBF316E8D940AF0AD3")


def test_keystream_from_arp_cipher_roundtrips():
    ks = bytes(range(16))
    cipher = bytes(ks[i] ^ ARP_REQUEST_PLAINTEXT[i] for i in range(16))
    assert keystream_from_arp_cipher(cipher) == ks


# ---- Placeholder ------------------------------------------------------------

def test_placeholder_reports_ready_at_threshold():
    c = PlaceholderCracker(threshold=100)
    assert isinstance(c, WepCracker)
    for _ in range(99):
        c.feed(b"\x00\x00\x00", b"\x00" * 16)
    assert not c.ready
    c.feed(b"\x00\x00\x00", b"\x00" * 16)
    assert c.ready
    assert c.recover() is None
    assert c.sample_count == 100


# ---- Native PTW end-to-end --------------------------------------------------

def _synth_samples(key: bytes, n: int, seed: int = 1):
    """n synthetic WEP ARP packets under `key`: random IV + the keystream a
    cracker would derive from the known ARP plaintext."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        iv = bytes(rng.randrange(256) for _ in range(3))
        ks = rc4_keystream(iv + key, 16)   # what cipher⊕known-plaintext yields
        out.append((iv, ks))
    return out


@pytest.mark.slow
def test_ptw_recovers_40bit_key():
    key = bytes.fromhex("6162636465")  # "abcde" — the user's dd-wrt test key
    c = PtwCracker()
    for iv, ks in _synth_samples(key, 40_000):
        c.feed(iv, ks)
    assert c.recover() == key


@pytest.mark.slow
def test_ptw_recovers_random_40bit_key():
    # Deterministic key so the test can't flake; random.Random keeps it varied.
    key = bytes(random.Random(11).randrange(256) for _ in range(5))
    c = PtwCracker()
    for iv, ks in _synth_samples(key, 60_000, seed=7):
        c.feed(iv, ks)
    assert c.recover() == key


@pytest.mark.slow
def test_ptw_recovers_104bit_key():
    key = bytes(random.Random(99).randrange(256) for _ in range(13))
    c = PtwCracker()
    for iv, ks in _synth_samples(key, 90_000, seed=3):
        c.feed(iv, ks)
    assert c.recover() == key


@pytest.mark.slow
def test_cracker_picklable_and_recovers_after_roundtrip():
    """The campaign runs recover() in a ProcessPoolExecutor, which pickles the
    cracker to the worker. Prove that round-trip preserves recovery."""
    import pickle
    key = bytes.fromhex("6162636465")
    c = PtwCracker()
    for iv, ks in _synth_samples(key, 40_000):
        c.feed(iv, ks)
    c2 = pickle.loads(pickle.dumps(c))    # exactly what the process pool does
    assert c2.recover() == key


@pytest.mark.slow
def test_ptw_tolerates_one_odd_packet_in_verify():
    """A single bad verify sample (e.g. an ARP-sized broadcast that wasn't
    actually an ARP → wrong 'known plaintext') must not reject the correct
    key — majority verification shrugs it off."""
    key = bytes.fromhex("6162636465")
    c = PtwCracker()
    for iv, ks in _synth_samples(key, 40_000):
        c.feed(iv, ks)
    iv0, ks0 = c._verify[0]
    c._verify[0] = (iv0, bytes(b ^ 0xFF for b in ks0))   # poison one sample
    assert c.recover() == key


@pytest.mark.slow
def test_ptw_no_false_key_with_few_samples():
    key = os.urandom(5)
    c = PtwCracker()
    c._MAX_TRIALS = 2000   # bound the doomed search — proving no false positive
    for iv, ks in _synth_samples(key, 50):
        c.feed(iv, ks)
    # Far too few IVs — must not return a bogus key (verification guards it).
    assert c.recover() is None
