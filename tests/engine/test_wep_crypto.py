"""Offline tests for the WEP forging core (no hardware)."""
import struct
import zlib

import pytest

from wifit3.engine.attacks.wep.wep_crypto import (
    forge_arp_request,
    icv,
    wep_encrypt,
)


def test_icv_is_le_crc32():
    assert icv(b"abc") == struct.pack("<I", zlib.crc32(b"abc") & 0xFFFFFFFF)


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
