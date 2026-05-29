"""Unit tests for engine/wpa/handshake.py — the single source of truth for
WPA 4-way crackability + hc22000 emission."""
from wifit3.engine.models import EapolFrame, Handshake
from wifit3.engine.wpa import handshake as wpa

ANONCE = b"\xaa" * 32
SNONCE = b"\x02" * 32


def _rc(n: int) -> str:
    return n.to_bytes(8, "big").hex()


def _frame(msg, replay, *, nonce=None, mic=True, complete=True, ts=0.0):
    """An EapolFrame with the fields crackability cares about. `complete=False`
    simulates a clipped 802.1X payload; `mic=False` an M1-style no-MIC frame."""
    return EapolFrame(
        raw=b"",
        msg_num=msg,
        replay_hex=_rc(replay),
        nonce=(nonce if nonce is not None else bytes([msg]) * 32),
        mic=(b"\x11" * 16 if mic else b"\x00" * 16),
        key_data_len=0,
        eapol_payload=(bytes(120) if complete else bytes(50)),
        timestamp=ts,
    )


def _hs(*frames):
    hs = Handshake(bssid="aa:bb:cc:dd:ee:ff", client_mac="11:22:33:44:55:66",
                   beacon_frame=b"B")
    hs.eapol_frames.extend(frames)
    return hs


def test_m1_m2_crackable():
    hs = _hs(_frame(1, 5, nonce=ANONCE, mic=False), _frame(2, 5, nonce=SNONCE))
    pairs = wpa.crackable_pairs(hs)
    assert len(pairs) == 1
    assert pairs[0].pair_byte == 0x00
    assert pairs[0].mic_frame.msg_num == 2
    assert pairs[0].anonce_frame.nonce == ANONCE


def test_m2_m3_crackable_uses_m2_keystone():
    hs = _hs(_frame(2, 5, nonce=SNONCE), _frame(3, 6, nonce=ANONCE))
    pairs = wpa.crackable_pairs(hs)
    assert len(pairs) == 1
    assert pairs[0].pair_byte == 0x02
    assert pairs[0].mic_frame.msg_num == 2          # keystone is M2, never M3


def test_clipped_m2_is_not_crackable():
    # The exact failure that lost a capture: M2's 802.1X payload was truncated,
    # so it can't be the MIC frame -> no crackable pair -> nothing to save.
    hs = _hs(_frame(2, 5, nonce=SNONCE, complete=False), _frame(3, 6, nonce=ANONCE))
    assert wpa.crackable_pairs(hs) == []


def test_m3_m4_only_with_echoed_snonce():
    zeroed = _hs(_frame(3, 6, nonce=ANONCE), _frame(4, 6, nonce=b"\x00" * 32))
    assert wpa.crackable_pairs(zeroed) == []         # zeroed M4 nonce -> no SNonce
    echoed = _hs(_frame(3, 6, nonce=ANONCE), _frame(4, 6, nonce=SNONCE))
    pairs = wpa.crackable_pairs(echoed)
    assert len(pairs) == 1 and pairs[0].pair_byte == 0x05


def test_cross_session_not_paired():
    # M2 + an M3 from a different association (replay not +1) must not pair.
    hs = _hs(_frame(2, 5, nonce=SNONCE), _frame(3, 9, nonce=ANONCE))
    assert wpa.crackable_pairs(hs) == []


def test_one_instance_per_anonce():
    # M1+M2 and M2+M3 of the SAME association collapse to one instance.
    hs = _hs(
        _frame(1, 5, nonce=ANONCE, mic=False),
        _frame(2, 5, nonce=SNONCE),
        _frame(3, 6, nonce=ANONCE),
    )
    assert len(wpa.crackable_pairs(hs)) == 1


def test_rehandshake_is_two_instances():
    hs = _hs(
        _frame(2, 5, nonce=SNONCE), _frame(3, 6, nonce=ANONCE),
        _frame(2, 9, nonce=b"\x07" * 32), _frame(3, 10, nonce=b"\x55" * 32),
    )
    assert len(wpa.crackable_pairs(hs)) == 2


def test_describe_flags():
    good = wpa.describe(_frame(2, 5, nonce=SNONCE))
    assert good.has_nonce and good.has_mic and good.eapol_complete and good.useful
    clipped = wpa.describe(_frame(2, 5, nonce=SNONCE, complete=False))
    assert clipped.has_nonce and clipped.has_mic and not clipped.eapol_complete
    assert not clipped.useful
    m1 = wpa.describe(_frame(1, 5, nonce=ANONCE, mic=False))
    assert m1.has_nonce and not m1.has_mic and m1.useful   # M1 needs no MIC


def test_hc22000_line_shape():
    hs = _hs(_frame(1, 5, nonce=ANONCE, mic=False), _frame(2, 5, nonce=SNONCE))
    line = wpa.hc22000_line("TestNet", hs, wpa.crackable_pairs(hs)[0])
    parts = line.split("*")
    assert parts[0] == "WPA" and parts[1] == "02"
    assert parts[2] == "11" * 16                 # MIC (from M2)
    assert parts[3] == "aabbccddeeff"            # AP MAC
    assert parts[6] == ANONCE.hex()              # ANonce
    assert parts[8] == "00"                      # MESSAGEPAIR (M1+M2, EAPOL from M2)
