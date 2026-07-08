"""Tests for EAPOL-Key parsing + 4-way handshake pair detection."""

import struct

import pytest

from wifit3.engine.models import HandshakeMessage, Handshake
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
    assert parsed.type == "eapol"
    assert parsed.msg_num == 1
    assert parsed.nonce == anonce
    assert parsed.mic == mic
    assert parsed.replay_counter == b"\x00" * 7 + b"\x01"
    assert parsed.key_data_len == 0


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
    assert parsed.msg_num == 1
    assert parsed.pmkid == pmkid


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
    assert parsed.pmkid is None


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
    assert parsed.pmkid is None


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
    assert parsed.pmkid == pmkid


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
    assert parsed.pmkid is None


# ---- M2 client-AKM extraction ---------------------------------------------------

def _rsn_keydata(akm_suite: int) -> bytes:
    """A minimal RSN IE (tag 48 + header) as the supplicant ships it in M2's
    cleartext Key Data: CCMP group + pairwise, one AKM suite."""
    body = (
        b"\x01\x00"                              # Version = 1
        + b"\x00\x0f\xac\x04"                    # Group cipher = CCMP
        + b"\x01\x00" + b"\x00\x0f\xac\x04"      # 1 pairwise = CCMP
        + b"\x01\x00" + b"\x00\x0f\xac" + bytes([akm_suite])   # 1 AKM
        + b"\x00\x00"                            # RSN capabilities
    )
    return bytes([48, len(body)]) + body


def _m2(key_data: bytes) -> bytes:
    return _build_eapol_frame(
        bssid_bytes=bytes.fromhex("AABBCCDDEEFF"),
        client_bytes=bytes.fromhex("112233445566"),
        key_info=0x0100 | 0x0008,   # M2: MIC, !ACK, !INSTALL, key data present
        replay_counter=b"\x00" * 7 + b"\x01",
        nonce=bytes(range(32)),
        mic=bytes(range(16)),
        key_data=key_data,
    )


def test_parser_extracts_sae_client_akm_from_m2():
    parsed = WlanFrameParser.parse_80211_frame(_m2(_rsn_keydata(0x08)), -42)
    assert parsed.msg_num == 2
    assert parsed.akm == 0x08          # SAE — the useless case


def test_parser_extracts_psk_client_akm_from_m2():
    parsed = WlanFrameParser.parse_80211_frame(_m2(_rsn_keydata(0x02)), -42)
    assert parsed.akm == 0x02          # PSK — crackable


def test_parser_no_client_akm_on_m1():
    """The AKM rides M2's RSN IE; M1 carries (at most) a PMKID KDE, no RSN IE."""
    frame = _build_eapol_frame(
        bssid_bytes=bytes.fromhex("AABBCCDDEEFF"),
        client_bytes=bytes.fromhex("112233445566"),
        key_info=0x0080 | 0x0008,               # M1
        replay_counter=b"\x00" * 7 + b"\x01",
        key_data=_pmkid_kde(bytes(range(16))),
    )
    parsed = WlanFrameParser.parse_80211_frame(frame, -42)
    assert parsed.msg_num == 1
    assert parsed.akm is None


def test_protected_data_frame_typed_data_and_flagged():
    """A CCMP-protected data frame (FC Protected + ExtIV) parses as type 'data'
    with protected=True — where an encrypted EAPOL M1 lands, so the PMKID harvest
    can tell 'M1 arrived but was PMF-protected' from 'AP silent'."""
    bssid = bytes.fromhex("aabbccddee01")
    sta = bytes.fromhex("021122334455")
    fc0, fc1 = 0x08, 0x40 | 0x02                          # data; Protected + from_ds
    hdr = bytes([fc0, fc1]) + b"\x00\x00" + sta + bssid + bssid + b"\x00\x00"
    ccmp = b"\x00\x00\x00\x20" + b"\x00" * 4 + b"\xde" * 16   # ExtIV (0x20) at KeyID byte
    parsed = WlanFrameParser.parse_80211_frame(hdr + ccmp, -42)
    assert parsed.type == "data"
    assert parsed.protected is True


# ---- Handshake pair-detection tests --------------------------------------------

def _ef(msg_num, replay_int, raw=None, *, ts=0.0, nonce=None, mic=True, complete=True):
    """HandshakeMessage builder for the model-delegation tests. By default produces a
    *usable* frame — a real (non-zero) nonce, a real MIC, and a complete 802.1X
    payload — so M2/M4 qualify as MIC keystones and M1/M3 as ANonce donors, the
    way a clean capture looks. Pass mic=False for an M1-style no-MIC frame or
    complete=False to simulate a clipped payload.

    ts=0.0 (default) means 'unset' → the time-window check is skipped, so these
    tests pin the replay-counter + content rules without depending on timing."""
    return HandshakeMessage(
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
    hs.messages.extend(frames)
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


def test_m2_plus_m3_within_nc_tolerance_is_complete():
    """M3 should be M2.replay+1, but hcxpcapngtool (and now we) tolerate a gap up
    to the NC value (8) and let hashcat fix the small drift — so replay 9 vs the
    expected 6 (gap 3) still pairs."""
    hs = _make_hs(_ef(2, 5), _ef(3, 9))
    assert hs.is_complete


def test_m2_plus_m3_replay_gap_beyond_nc_NOT_complete():
    """Past the NC tolerance (8), the replay mismatch is treated as unrelated."""
    hs = _make_hs(_ef(2, 5), _ef(3, 20))   # expected 6, got 20 → gap 14 > 8
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


def test_m1_m2_valid_despite_unrelated_later_m3():
    """M1+M2 of one association is a valid pair (hashcat MESSAGEPAIR 0x00). An M3
    from a *different*, later association (different ANonce, outside the pairing
    window) does NOT invalidate it — only the M1 the M2 actually answered supplies
    the ANonce. Matches hcxpcapngtool, which has no global 'a conflicting M3
    anywhere vetoes this M1' rule."""
    hs = _make_hs(
        _ef(1, 5, ts=100.0, nonce=b"\xaa" * 32),   # M1 of association A
        _ef(2, 5, ts=100.1),                        # M2 answering it (same window)
        _ef(3, 6, ts=200.0, nonce=b"\xbb" * 32),   # unrelated later M3 (assoc B)
    )
    assert hs.is_complete
    pair = hs.find_valid_pair()
    assert (pair[0].msg_num, pair[1].msg_num) == (1, 2)
    assert pair[0].nonce == b"\xaa" * 32           # ANonce from assoc A's M1


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
    assert hs.total_messages == 3
