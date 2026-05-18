"""Tests for EAPOL-Key parsing + 4-way handshake pair detection."""

import struct

import pytest

from wifit3.engine.models import EapolFrame, Handshake
from wifit3.wlan.packet import WlanFrameParser


def _build_eapol_frame(
    bssid_bytes: bytes,
    client_bytes: bytes,
    key_info: int,
    replay_counter: bytes,
    nonce: bytes = b"\x00" * 32,
    mic: bytes = b"\x00" * 16,
    key_data: bytes = b"",
) -> bytes:
    """Build a minimal valid 802.11 data frame carrying an EAPOL-Key payload.

    Direction is client -> AP (to_ds=1, from_ds=0) which matches M2/M4.
    Flip to_ds/from_ds for M1/M3 if it matters (it doesn't for our parser —
    we read the BSSID from addr1 when to_ds=0, addr2 when to_ds=1, but the
    classification path doesn't care about direction).
    """
    # FC0 = data frame (type=2), subtype=0
    fc0 = 0x08
    # FC1: to_ds=1, from_ds=0
    fc1 = 0x01
    dur = b"\x00\x00"
    # to_ds=1: addr1=BSSID, addr2=SRC(client), addr3=DST
    seq = b"\x00\x00"
    mac_hdr = bytes([fc0, fc1]) + dur + bssid_bytes + client_bytes + bssid_bytes + seq

    # LLC/SNAP + EAPOL ethertype
    llc_snap = b"\xaa\xaa\x03\x00\x00\x00\x88\x8e"

    # 802.1X header: version(1), type(1)=3 EAPOL-Key, length(2)
    key_descriptor = (
        b"\x02"                                          # Key Desc Type = 2 (RSN)
        + struct.pack(">H", key_info)                    # Key Info
        + b"\x00\x10"                                    # Key Length
        + replay_counter                                 # 8 B
        + nonce                                          # 32 B
        + b"\x00" * 16                                   # Key IV
        + b"\x00" * 8                                    # Key RSC
        + b"\x00" * 8                                    # Reserved
        + mic                                            # 16 B
        + struct.pack(">H", len(key_data))               # Key Data Length
        + key_data
    )
    body_len = len(key_descriptor)
    x802_1x = b"\x01\x03" + struct.pack(">H", body_len)

    return mac_hdr + llc_snap + x802_1x + key_descriptor


# ---- Classifier unit tests ------------------------------------------------------

@pytest.mark.parametrize(
    "key_info, key_data_len, expected",
    [
        (0x0080 | 0x0008, 0, 1),                 # M1: ACK, !MIC, !INSTALL, pairwise
        (0x0100 | 0x0008, 22, 2),                # M2: MIC, !ACK, !INSTALL, key data present
        (0x01C0 | 0x0008, 56, 3),                # M3: ACK+MIC+INSTALL
        (0x0100 | 0x0008, 0, 4),                 # M4: MIC, !ACK, !INSTALL, no key data
        (0x0000, 0, 0),                          # All flags clear → not a 4-way msg
        (0x0080 | 0x0040, 0, 0),                 # ACK + INSTALL only — group rekey-ish
    ],
)
def test_classify_eapol_msg(key_info, key_data_len, expected):
    assert WlanFrameParser._classify_eapol_msg(key_info, key_data_len) == expected


def test_parser_extracts_full_eapol_fields():
    bssid = bytes.fromhex("AABBCCDDEEFF")
    client = bytes.fromhex("112233445566")
    anonce = bytes(range(32))
    mic = bytes(range(16))

    # M1 frame
    frame = _build_eapol_frame(
        bssid_bytes=bssid,
        client_bytes=client,
        key_info=0x0080 | 0x0008,
        replay_counter=b"\x00" * 7 + b"\x01",
        nonce=anonce,
        mic=mic,
    )
    parsed = WlanFrameParser.parse_80211_frame(frame, -42)
    assert parsed is not None
    assert parsed["type"] == "eapol"
    assert parsed["eapol_msg_num"] == 1
    assert parsed["eapol_nonce"] == anonce
    assert parsed["eapol_mic"] == mic
    assert parsed["eapol_replay_counter"] == b"\x00" * 7 + b"\x01"
    assert parsed["eapol_key_data_len"] == 0


# ---- Handshake pair-detection tests --------------------------------------------

def _ef(msg_num, replay_int, raw=None):
    """Quick EapolFrame builder for pair tests."""
    return EapolFrame(
        raw=raw if raw is not None else bytes([msg_num, replay_int % 256]),
        msg_num=msg_num,
        replay_hex=(replay_int).to_bytes(8, "big").hex(),
        nonce=b"\x00" * 32,
        mic=b"\x00" * 16,
        key_data_len=0,
    )


def _make_hs(*frames, with_beacon=True):
    hs = Handshake(
        bssid="aa:bb:cc:dd:ee:ff",
        client_mac="11:22:33:44:55:66",
        beacon_frame=b"BEACON" if with_beacon else None,
    )
    hs.eapol_frames.extend(frames)
    return hs


def test_no_beacon_means_incomplete():
    hs = _make_hs(_ef(1, 5), _ef(2, 5), with_beacon=False)
    assert not hs.is_complete


def test_m1_plus_m2_same_replay_is_complete():
    hs = _make_hs(_ef(1, 5), _ef(2, 5))
    assert hs.is_complete
    pair = hs.find_valid_pair()
    assert (pair[0].msg_num, pair[1].msg_num) == (1, 2)


def test_two_m1_retries_NOT_complete():
    """The user-reported bug: two M1s with identical replay counter were
    incorrectly being treated as a valid M1+M2 pair."""
    hs = _make_hs(_ef(1, 5), _ef(1, 5))
    assert not hs.is_complete
    assert hs.captured_messages == {1}


def test_m1_plus_m1_plus_m3_NOT_complete():
    """The exact scenario from Wireshark: two M1s, one M3, no M2 or M4."""
    hs = _make_hs(_ef(1, 5), _ef(1, 5), _ef(3, 6))
    assert not hs.is_complete


def test_m2_plus_m3_pair_replay_plus_one_is_complete():
    hs = _make_hs(_ef(2, 5), _ef(3, 6))
    assert hs.is_complete
    assert (hs.find_valid_pair()[0].msg_num, hs.find_valid_pair()[1].msg_num) == (2, 3)


def test_m2_plus_m3_with_wrong_replay_NOT_complete():
    """M3 must be M2.replay + 1, not any random other value."""
    hs = _make_hs(_ef(2, 5), _ef(3, 9))
    assert not hs.is_complete


def test_m3_plus_m4_same_replay_is_complete():
    hs = _make_hs(_ef(3, 7), _ef(4, 7))
    assert hs.is_complete


def test_m1_plus_m4_replay_plus_one_is_complete():
    hs = _make_hs(_ef(1, 5), _ef(4, 6))
    assert hs.is_complete


def test_replay_counter_wide_int_handles_8_byte_field():
    """Replay counters can be up to 64-bit. Ensure we compare them as
    integers, not as raw hex-string lexicographic ordering."""
    hs = _make_hs(_ef(2, 0xFF), _ef(3, 0x100))
    assert hs.is_complete


def test_captured_messages_set():
    hs = _make_hs(_ef(1, 5), _ef(1, 5), _ef(3, 6))
    assert hs.captured_messages == {1, 3}


def test_total_eapol_frames():
    hs = _make_hs(_ef(1, 5), _ef(1, 5), _ef(3, 6))
    assert hs.total_eapol_frames == 3
