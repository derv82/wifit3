"""Offline tests for the WEP forging core (no hardware)."""
import struct
import zlib

import pytest

import os
import random

from wifit3.engine.attacks.wep.wep_crypto import (
    CRC32_RESIDUE,
    chop_last_byte_and_fixup,
    forge_arp_request,
    icv,
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
