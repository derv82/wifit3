"""Offline tests for the WEP forging core (no hardware)."""
import struct
import zlib

import pytest

import os
import random

from wifit3.engine.attacks.wep.wep_crypto import (
    CRC32_RESIDUE,
    arp_request_plaintext,
    build_fragments,
    chop_last_byte_and_fixup,
    forge_arp_request,
    icv,
    seed_keystream_from_arp,
    wep_encrypt,
    _crc,
    _CRC_TABLE,
    _patch_zeros,
    _reverse_crc_byte,
)


def test_icv_is_le_crc32():
    assert icv(b"abc") == struct.pack("<I", zlib.crc32(b"abc") & 0xFFFFFFFF)


def test_crc32_residue_constant():
    # Foundation for the (future) ChopChop fix-up: data ++ icv(data) always
    # has this CRC32 residue, which is how the attack cancels the unknown ICV.
    for d in (b"hello", b"test123", bytes(range(36))):
        assert zlib.crc32(d + icv(d)) & 0xFFFFFFFF == CRC32_RESIDUE


def test_wep_encrypt_roundtrips_to_plaintext_plus_icv():
    ks = bytes(range(64))
    pt = b"hello world"
    body = wep_encrypt(ks, pt)
    dec = bytes(b ^ k for b, k in zip(body, ks))
    assert dec[: len(pt)] == pt
    assert dec[len(pt):] == icv(pt)        # trailing ICV is valid over pt


def test_wep_encrypt_rejects_short_keystream():
    with pytest.raises(ValueError):
        wep_encrypt(b"\x00" * 4, b"too long for this keystream")


def test_forge_arp_request_decrypts_to_valid_arp():
    ks = bytes((i * 7 + 3) & 0xFF for i in range(64))   # >= 40 needed
    sender_mac = bytes.fromhex("020000000001")
    sender_ip = bytes([192, 168, 1, 123])
    target_ip = bytes([192, 168, 1, 1])
    body = forge_arp_request(
        ks, sender_mac=sender_mac, sender_ip=sender_ip, target_ip=target_ip
    )
    pt = bytes(b ^ k for b, k in zip(body, ks))
    msg, trailer = pt[:-4], pt[-4:]
    # Well-formed: LLC/SNAP + ARP request, our sender, broadcast target MAC.
    assert msg[:8] == bytes([0xAA, 0xAA, 0x03, 0x00, 0x00, 0x00, 0x08, 0x06])
    assert msg[8:16] == bytes([0x00, 0x01, 0x08, 0x00, 0x06, 0x04, 0x00, 0x01])
    assert msg[16:22] == sender_mac
    assert msg[22:26] == sender_ip
    assert msg[26:32] == b"\x00" * 6      # target MAC unknown in a request
    assert msg[32:36] == target_ip
    # And the AP's ICV check would pass.
    assert trailer == icv(msg)


def test_forge_arp_request_needs_enough_keystream():
    with pytest.raises(ValueError):
        forge_arp_request(
            b"\x00" * 20,                  # < 40
            sender_mac=b"\x00" * 6,
            sender_ip=b"\x00" * 4,
            target_ip=b"\x00" * 4,
        )


# ---- ChopChop CRC machinery (each piece verified against zlib) -------------

def test_internal_crc_matches_zlib():
    for d in (b"", b"x", b"hello world", bytes(range(50))):
        assert _crc(d) == zlib.crc32(d) & 0xFFFFFFFF


def test_reverse_crc_byte_undoes_a_step():
    rng = random.Random(1)
    for _ in range(200):
        reg = rng.randrange(1 << 32)
        b = rng.randrange(256)
        after = (reg >> 8) ^ _CRC_TABLE[(reg ^ b) & 0xFF]
        assert _reverse_crc_byte(after, b) == reg


def test_patch_zeros_hits_target_crc():
    rng = random.Random(2)
    for _ in range(50):
        zlen = rng.randrange(0, 40)
        corr = bytes(rng.randrange(256) for _ in range(4))
        target = _crc(b"\x00" * zlen + corr)
        # The unique 4 bytes producing that CRC must be exactly corr.
        assert _patch_zeros(zlen, target) == corr


# ---- ChopChop oracle: chop+fixup is valid IFF the guess is right -----------

def _wep_body(keystream: bytes, data: bytes) -> bytes:
    return wep_encrypt(keystream, data)


def test_chopchop_fixup_valid_for_correct_guess_only():
    rng = random.Random(3)
    for _ in range(40):
        ks = bytes(rng.randrange(256) for _ in range(80))
        data = bytes(rng.randrange(256) for _ in range(rng.randrange(8, 40)))
        body = _wep_body(ks, data)               # valid frame (data ++ icv)
        plaintext = data + icv(data)
        true_last = plaintext[-1]

        # Correct guess → shortened frame decrypts to a valid ICV (residue).
        short = chop_last_byte_and_fixup(body, true_last)
        p_short = bytes(b ^ k for b, k in zip(short, ks))
        assert zlib.crc32(p_short) & 0xFFFFFFFF == CRC32_RESIDUE

        # Any wrong guess → not valid.
        wrong = chop_last_byte_and_fixup(body, true_last ^ 0x5A)
        p_wrong = bytes(b ^ k for b, k in zip(wrong, ks))
        assert zlib.crc32(p_wrong) & 0xFFFFFFFF != CRC32_RESIDUE


def test_chopchop_iterates_keeping_frames_valid():
    """Iterated chopping: guess the CURRENT frame's last plaintext byte each
    round (a fixup rewrites the trailing bytes, so it's not the original's) →
    every shortened frame stays valid. (Reconstructing the original data from
    the guess sequence is the attack daemon's job, not this function's.)"""
    ks = bytes((i * 3 + 1) & 0xFF for i in range(80))
    body = _wep_body(ks, os.urandom(20))
    for _ in range(5):
        p = bytes(b ^ k for b, k in zip(body, ks))
        assert zlib.crc32(p) & 0xFFFFFFFF == CRC32_RESIDUE   # current frame valid
        body = chop_last_byte_and_fixup(body, p[-1])         # guess its last byte


# ---- Fragmentation (M5) ----------------------------------------------------
#
# These prove the SEND side is self-consistent: a simulated AP that decrypts +
# reassembles our fragments recovers exactly what we fragmented. That's a
# consistency check, NOT proof the real dd-wrt box reassembles + relays — only
# the on-air probe (scripts/wep/frag_probe.py) can establish that.

_SNAP_ARP_PREFIX = bytes([0xAA, 0xAA, 0x03, 0x00, 0x00, 0x00, 0x08, 0x06])


def _arp_body(keystream: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """Build a captured-looking WEP ARP body: IV ++ KeyID(0) ++ RC4(pt++icv)."""
    return iv + b"\x00" + wep_encrypt(keystream, plaintext)


def test_seed_keystream_recovers_known_prga():
    ks = bytes((i * 5 + 9) & 0xFF for i in range(40))
    iv = bytes([0x11, 0x22, 0x33])
    pt = arp_request_plaintext(
        sender_mac=bytes.fromhex("020000000099"),
        sender_ip=bytes([10, 0, 0, 5]),
        target_ip=bytes([10, 0, 0, 1]),
    )
    body = _arp_body(ks, iv, pt)
    # The 8-byte seed must equal the real first 8 keystream bytes...
    assert seed_keystream_from_arp(body, want=8) == ks[:8]
    # ...and shorter requests are honored (6 protocol-agnostic SNAP bytes).
    assert seed_keystream_from_arp(body, want=6) == ks[:6]


def test_seed_keystream_rejects_oversize_request():
    body = _arp_body(bytes(range(40)), bytes(3), b"\x00" * 36)
    with pytest.raises(ValueError):
        seed_keystream_from_arp(body, want=9)   # >8 is not known plaintext


def _reassemble(fragments: list, keystream: bytes) -> bytes:
    """Stand-in for the AP: validate each fragment's WEP framing + ICV, check
    the More-Fragments / fragment-number bookkeeping, and concatenate the
    decrypted payload chunks back into the original MSDU."""
    out = bytearray()
    for i, frame in enumerate(fragments):
        fc1 = frame[1]
        assert fc1 & 0x01, "ToDS must be set"
        assert fc1 & 0x40, "Protected must be set"
        more = bool(fc1 & 0x04)
        assert more == (i < len(fragments) - 1), "More-Fragments bit wrong"
        seq_ctl = struct.unpack("<H", frame[22:24])[0]
        assert (seq_ctl & 0x0F) == i, "fragment number wrong"
        body = frame[24:]
        cipher = body[4:]                       # skip IV(3)+KeyID(1)
        plain = bytes(b ^ k for b, k in zip(cipher, keystream))
        data, trailer = plain[:-4], plain[-4:]
        assert trailer == icv(data), "fragment ICV invalid"
        out += data
    return bytes(out)


def test_build_fragments_roundtrips_through_simulated_reassembly():
    ks = bytes((i * 7 + 1) & 0xFF for i in range(8))   # 8-byte seed → 4 B/frag
    iv = bytes([0xAB, 0xCD, 0xEF])
    payload = arp_request_plaintext(
        sender_mac=bytes.fromhex("020000000042"),
        sender_ip=bytes([192, 168, 6, 66]),
        target_ip=bytes([192, 168, 6, 1]),
    )                                                  # 36 bytes → 9 fragments
    frags = build_fragments(
        ks, iv, payload,
        bssid=bytes.fromhex("001122334455"),
        source_mac=bytes.fromhex("020000000042"),
        dest_mac=b"\xff" * 6,
    )
    assert len(frags) == 9                             # ceil(36 / 4)
    assert _reassemble(frags, ks) == payload
    # The seed extracted from the reassembled+re-encrypted relay would let us
    # forge ARPs directly: the relayed plaintext starts with the SNAP prefix.
    assert payload[:8] == _SNAP_ARP_PREFIX


def test_build_fragments_rejects_payload_too_big_for_seed():
    ks = bytes(range(8))                               # 4 data bytes/fragment
    with pytest.raises(ValueError):
        build_fragments(
            ks, bytes(3), b"\x00" * 100,               # 100 → 25 frags > 16
            bssid=b"\x00" * 6, source_mac=b"\x00" * 6, dest_mac=b"\xff" * 6,
        )


def test_build_fragments_single_fragment_has_no_more_frag_bit():
    ks = bytes(range(40))                              # 36 data bytes/fragment
    frags = build_fragments(
        ks, bytes(3), b"\x01\x02\x03",
        bssid=b"\x00" * 6, source_mac=b"\x00" * 6, dest_mac=b"\xff" * 6,
    )
    assert len(frags) == 1
    assert not (frags[0][1] & 0x04)                    # lone fragment: no MoreFrag
