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


# ---- PMKID KDE extraction -------------------------------------------------------

def _pmkid_kde(pmkid: bytes) -> bytes:
    """Build a valid PMKID KDE: 0xDD <len=0x14> 00 0F AC 04 <pmkid>."""
    assert len(pmkid) == 16
    return b"\xdd\x14\x00\x0f\xac\x04" + pmkid


def test_parser_extracts_pmkid_from_m1_key_data():
    bssid = bytes.fromhex("AABBCCDDEEFF")
    client = bytes.fromhex("112233445566")
    pmkid = bytes.fromhex("ad2fad48da558cdfeb19cea25e2ce5af")

    frame = _build_eapol_frame(
        bssid_bytes=bssid,
        client_bytes=client,
        key_info=0x0080 | 0x0008,  # M1
        replay_counter=b"\x00" * 7 + b"\x01",
        key_data=_pmkid_kde(pmkid),
    )
    parsed = WlanFrameParser.parse_80211_frame(frame, -42)
    assert parsed is not None
    assert parsed["eapol_msg_num"] == 1
    assert parsed.get("eapol_pmkid") == pmkid


def test_parser_no_pmkid_when_key_data_empty():
    frame = _build_eapol_frame(
        bssid_bytes=bytes.fromhex("AABBCCDDEEFF"),
        client_bytes=bytes.fromhex("112233445566"),
        key_info=0x0080 | 0x0008,
        replay_counter=b"\x00" * 7 + b"\x01",
        key_data=b"",
    )
    parsed = WlanFrameParser.parse_80211_frame(frame, -42)
    assert parsed is not None
    assert "eapol_pmkid" not in parsed


def test_parser_ignores_all_zero_pmkid():
    """Some APs ship a zero-filled placeholder KDE — not crackable; skip it."""
    frame = _build_eapol_frame(
        bssid_bytes=bytes.fromhex("AABBCCDDEEFF"),
        client_bytes=bytes.fromhex("112233445566"),
        key_info=0x0080 | 0x0008,
        replay_counter=b"\x00" * 7 + b"\x01",
        key_data=_pmkid_kde(b"\x00" * 16),
    )
    parsed = WlanFrameParser.parse_80211_frame(frame, -42)
    assert parsed is not None
    assert "eapol_pmkid" not in parsed


def test_parser_finds_pmkid_among_multiple_kdes():
    """Some APs concatenate multiple KDEs. PMKID may not be first."""
    pmkid = bytes.fromhex("11223344556677889900aabbccddeeff")
    # Bogus vendor KDE first, then the real PMKID KDE
    other_kde = b"\xdd\x06\x00\x0f\xac\x07\x00\x00"  # arbitrary 8-byte KDE
    frame = _build_eapol_frame(
        bssid_bytes=bytes.fromhex("AABBCCDDEEFF"),
        client_bytes=bytes.fromhex("112233445566"),
        key_info=0x0080 | 0x0008,
        replay_counter=b"\x00" * 7 + b"\x01",
        key_data=other_kde + _pmkid_kde(pmkid),
    )
    parsed = WlanFrameParser.parse_80211_frame(frame, -42)
    assert parsed.get("eapol_pmkid") == pmkid


def test_parser_rejects_truncated_kde():
    """A KDE whose declared length runs past the buffer must not raise."""
    # Length byte says 0x14 (20 B follow) but we only provide 5.
    truncated = b"\xdd\x14\x00\x0f\xac\x04"
    frame = _build_eapol_frame(
        bssid_bytes=bytes.fromhex("AABBCCDDEEFF"),
        client_bytes=bytes.fromhex("112233445566"),
        key_info=0x0080 | 0x0008,
        replay_counter=b"\x00" * 7 + b"\x01",
        key_data=truncated,
    )
    parsed = WlanFrameParser.parse_80211_frame(frame, -42)
    assert parsed is not None
    assert "eapol_pmkid" not in parsed


# ---- Handshake pair-detection tests --------------------------------------------

def _ef(msg_num, replay_int, raw=None, *, ts=0.0, nonce=None, mic=True, complete=True):
    """EapolFrame builder for the model-delegation tests. By default produces a
    *usable* frame — a real (non-zero) nonce, a real MIC, and a complete 802.1X
    payload — so M2/M4 qualify as MIC keystones and M1/M3 as ANonce donors, the
    way a clean capture looks. Pass mic=False for an M1-style no-MIC frame or
    complete=False to simulate a clipped payload.

    ts=0.0 (default) means 'unset' → the time-window check is skipped, so these
    tests pin the replay-counter + content rules without depending on timing."""
    return EapolFrame(
        raw=raw if raw is not None else bytes([msg_num, replay_int % 256]),
        msg_num=msg_num,
        replay_hex=(replay_int).to_bytes(8, "big").hex(),
        nonce=nonce if nonce is not None else bytes([msg_num]) * 32,
        mic=(b"\x11" * 16 if mic else b"\x00" * 16),
        key_data_len=0,
        eapol_payload=(bytes(120) if complete else b""),
        timestamp=ts,
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


# ---- Same-instance binding (timestamp + ANonce) -------------------------------

def test_pair_rejected_when_frames_far_apart_in_time():
    """The reported bug: M1, then an M2 with a coincidentally-matching replay
    counter a minute later, are different associations → must NOT pair (the
    ANonce/SNonce would be from different PTKs → uncrackable)."""
    hs = _make_hs(_ef(1, 5, ts=1000.0), _ef(2, 5, ts=1060.0))
    assert not hs.is_complete


def test_pair_accepted_when_frames_within_window():
    hs = _make_hs(_ef(1, 5, ts=1000.0), _ef(2, 5, ts=1000.05))
    assert hs.is_complete
    pair = hs.find_valid_pair()
    assert (pair[0].msg_num, pair[1].msg_num) == (1, 2)


def test_m1_anonce_guard_rejects_wrong_instance():
    """M1+M2 are tight in time, but a captured M3 at replay+1 carries a
    DIFFERENT ANonce — proof this M1 belongs to another association, so its
    ANonce can't be trusted. With the M3 far away (no M2+M3 pair), nothing
    valid remains → incomplete (better than emitting an uncrackable line)."""
    hs = _make_hs(
        _ef(1, 5, ts=100.0, nonce=b"\xaa" * 32),
        _ef(2, 5, ts=100.1),
        _ef(3, 6, ts=200.0, nonce=b"\xbb" * 32),
    )
    assert not hs.is_complete


def test_timestampless_frames_still_pair_on_replay():
    """Fixtures / pre-timestamp captures (ts unset) fall back to replay rules."""
    hs = _make_hs(_ef(1, 5), _ef(2, 5))
    assert hs.is_complete


def test_complete_instances_counts_distinct_anonces():
    """A client that completes the 4-way twice (re-association → fresh ANonce,
    new replay base) counts as two captured handshakes, not one."""
    hs = _make_hs(
        _ef(1, 5, ts=100.0, nonce=b"\xaa" * 32),
        _ef(2, 5, ts=100.1, nonce=b"\x11" * 32),
        _ef(1, 9, ts=200.0, nonce=b"\xbb" * 32),
        _ef(2, 9, ts=200.1, nonce=b"\x22" * 32),
    )
    assert hs.is_complete
    assert hs.complete_instances == 2


def test_complete_instances_one_4way_is_single_instance():
    """All four messages of ONE handshake share the AP's ANonce → one instance,
    even though several M-frame combos validate."""
    anonce = b"\xaa" * 32
    hs = _make_hs(
        _ef(1, 5, ts=100.0, nonce=anonce),
        _ef(2, 5, ts=100.1, nonce=b"\x11" * 32),
        _ef(3, 6, ts=100.2, nonce=anonce),
        _ef(4, 6, ts=100.3, nonce=b"\x11" * 32),
    )
    assert hs.complete_instances == 1


def test_captured_messages_set():
    hs = _make_hs(_ef(1, 5), _ef(1, 5), _ef(3, 6))
    assert hs.captured_messages == {1, 3}


def test_total_eapol_frames():
    hs = _make_hs(_ef(1, 5), _ef(1, 5), _ef(3, 6))
    assert hs.total_eapol_frames == 3
