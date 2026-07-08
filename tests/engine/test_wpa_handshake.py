"""Unit tests for engine/wpa/handshake.py — the single source of truth for
WPA 4-way crackability + hc22000 emission."""
from wifit3.engine.models import HandshakeMessage, Handshake
from wifit3.engine.wpa import handshake as wpa

ANONCE = b"\xaa" * 32
SNONCE = b"\x02" * 32


def _rc(n: int) -> str:
    return n.to_bytes(8, "big").hex()


def _frame(msg, replay, *, nonce=None, mic=True, complete=True, ts=0.0):
    """An HandshakeMessage with the fields crackability cares about. `complete=False`
    simulates a clipped 802.1X payload; `mic=False` an M1-style no-MIC frame."""
    return HandshakeMessage(
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
    hs.messages.extend(frames)
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


def test_replay_gap_within_nc_pairs():
    """hcxpcapngtool tolerates a replay-counter gap up to the NC value (8) and
    sets the NC bit so hashcat fixes the small nonce drift. An M3 a few counts off
    from M2.replay+1 still pairs (it's the only candidate)."""
    hs = _hs(_frame(2, 5, nonce=SNONCE), _frame(3, 9, nonce=ANONCE))  # want 6, got 9 → gap 3
    pairs = wpa.crackable_pairs(hs)
    assert len(pairs) == 1
    assert pairs[0].anonce_frame.nonce == ANONCE


def test_replay_gap_beyond_nc_not_paired():
    """Past the NC tolerance (8) a replay mismatch is treated as unrelated."""
    hs = _hs(_frame(2, 5, nonce=SNONCE), _frame(3, 20, nonce=ANONCE))  # gap 14 > 8
    assert wpa.crackable_pairs(hs) == []


def test_lone_m2_binds_to_nearest_preceding_m1():
    """NETGEAR2G shape: the AP spams several M1s (all replay 0, a fresh ANonce each)
    with stale M3s ahead, then the client sends ONE M2 answering the last M1. The
    M2 binds to that nearest preceding M1 (its real association) — never a stale
    earlier M3 — so exactly one pair, carrying the newest ANonce."""
    a1, a2, a3, snonce = b"\x11" * 32, b"\x22" * 32, b"\x33" * 32, b"\x02" * 32
    hs = _hs(
        _frame(1, 0, nonce=a1, mic=False), _frame(3, 1, nonce=a1),
        _frame(1, 0, nonce=a2, mic=False), _frame(3, 1, nonce=a2),
        _frame(1, 0, nonce=a3, mic=False),     # newest M1, just before the M2
        _frame(2, 0, nonce=snonce),            # M2 answers a3
    )
    pairs = wpa.crackable_pairs(hs)
    assert len(pairs) == 1
    assert pairs[0].anonce_frame.nonce == a3
    assert (pairs[0].anonce_frame.msg_num, pairs[0].mic_frame.msg_num) == (1, 2)


def test_reconnect_spam_does_not_cross_pair():
    """Reconnect spam (the real NETGEAR2G HW capture): the AP resets its key-replay
    counter each association, so a STALE M3 from a prior attempt (a_old, replay 6)
    and a FRESH M2 from the next attempt (replay 5) collide on the M2+M3 replay+1
    rule. The fresh M2 must bind only within its OWN association — to the M1 it
    answered (a_new) — never to the stale a_old M3 ahead of it.

    No timestamps are set, so the binding runs purely off ARRIVAL ORDER: this is
    exactly the round-tripped-pcap condition (per-frame timestamps not preserved),
    where the earlier timestamp-based guard was fooled into 5 cross-session pairs."""
    a_old, a_new, s_new = b"\xa1" * 32, b"\xb2" * 32, b"\x02" * 32
    hs = _hs(
        _frame(1, 5, nonce=a_old, mic=False),   # old assoc M1
        _frame(3, 6, nonce=a_old),              # old assoc M3 (stale, ahead of M2)
        _frame(4, 6, nonce=b"\x00" * 32),       # old assoc M4 (zeroed)
        _frame(1, 5, nonce=a_new, mic=False),   # NEW assoc M1 (replay collides)
        _frame(2, 5, nonce=s_new),              # NEW assoc M2 answering the NEW M1
    )
    pairs = wpa.crackable_pairs(hs)
    # Exactly one crackable association, and it's the NEW one: the fresh M2 binds
    # to the M1 it answered (a_new), never to the stale a_old M3 sitting ahead.
    assert len(pairs) == 1
    assert pairs[0].anonce_frame.nonce == a_new
    assert pairs[0].mic_frame.nonce == s_new
    assert a_old not in {p.anonce_frame.nonce for p in pairs}


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


# ----- AKM crackability gating (SAE / FT / SHA256-PMKID) ---------------------

def _m1m2(*, offered, client=None):
    """A structurally perfect M1+M2 (always a crackable pair if the AKM allows),
    tagged with the AP's offered AKM suites and the client's chosen one."""
    hs = _hs(_frame(1, 5, nonce=ANONCE, mic=False), _frame(2, 5, nonce=SNONCE))
    hs.akm_offered = list(offered)
    hs.akm_client = client
    return hs


def _hs_pmkid(*, offered, client=None):
    hs = Handshake(bssid="aa:bb:cc:dd:ee:ff", client_mac="11:22:33:44:55:66",
                   beacon_frame=b"B", pmkid=b"\x01" * 16)
    hs.akm_offered = list(offered)
    hs.akm_client = client
    return hs


# --- EAPOL (WPA*02) gate: suppress SAE + FT-PSK ------------------------------

def test_sae_only_ap_suppresses_handshake():
    """The user's Beryl AX: beacon offers SAE only → every 4-way is uncrackable,
    so no pair is emitted (and thus no banner / save / hashline)."""
    hs = _m1m2(offered=[8])
    assert wpa.eapol_verdict(hs) == "uncrackable"
    assert not wpa.eapol_crackable(hs)
    assert wpa.crackable_pairs(hs) == []


def test_sae_ext_key_only_ap_suppresses_handshake():
    """WPA3-H2E uses SAE-EXT-KEY (24), still SAE-family → uncrackable."""
    assert wpa.crackable_pairs(_m1m2(offered=[24])) == []


def test_transition_ap_psk_client_is_crackable():
    """Transition AP, but THIS client negotiated PSK (M2 RSN IE) → crackable."""
    hs = _m1m2(offered=[2, 8], client=2)
    assert wpa.eapol_verdict(hs) == "crackable"
    assert len(wpa.crackable_pairs(hs)) == 1


def test_transition_ap_sae_client_suppressed():
    """Same transition AP, but this client chose SAE → uncrackable."""
    hs = _m1m2(offered=[2, 8], client=8)
    assert wpa.eapol_verdict(hs) == "uncrackable"
    assert wpa.crackable_pairs(hs) == []


def test_transition_ap_unknown_client_not_emitted():
    """Transition AP with no M2 AKM yet: can't confirm which side → withhold."""
    hs = _m1m2(offered=[2, 8], client=None)
    assert wpa.eapol_verdict(hs) == "unknown"
    assert wpa.crackable_pairs(hs) == []


def test_ft_psk_handshake_suppressed():
    """FT-PSK (4) is PSK-derived, but hashcat -m 22000 has no FT mode → the
    handshake is NOT emitted, and the trace badges 'FT'."""
    hs = _m1m2(offered=[4], client=4)
    assert wpa.eapol_verdict(hs) == "uncrackable"
    assert wpa.crackable_pairs(hs) == []
    assert wpa.uncrackable_label(hs) == "FT"


def test_ft_psk_only_ap_suppressed_without_client_akm():
    assert wpa.crackable_pairs(_m1m2(offered=[4, 19])) == []


def test_wpa2_plus_ft_unknown_client_withheld():
    """WPA2+FT AP (PSK + FT-PSK), client AKM unknown → can't tell → withhold."""
    assert wpa.eapol_verdict(_m1m2(offered=[2, 4])) == "unknown"


def test_no_sae_or_ft_offered_is_unchanged():
    """Plain WPA2-PSK (no SAE/FT): behaves exactly as before — the gate only ever
    removes SAE/FT false-positives, never plain-PSK captures."""
    hs = _m1m2(offered=[2])
    assert wpa.eapol_verdict(hs) == "crackable"
    assert len(wpa.crackable_pairs(hs)) == 1
    # No AKM info at all (fixtures / pre-AKM captures): still crackable.
    assert len(wpa.crackable_pairs(_m1m2(offered=[]))) == 1


# --- EAPOL gate: suppress EAP / Enterprise (PMK from the MSK, no passphrase) --

def test_eap_only_ap_suppresses_handshake():
    """Enterprise AP (EAP AKM): a full 4-way is captured but the PMK comes from
    the 802.1X MSK, not a passphrase → hashcat -m 22000 can't touch it."""
    hs = _m1m2(offered=[1])
    assert wpa.eapol_verdict(hs) == "uncrackable"
    assert wpa.crackable_pairs(hs) == []
    assert wpa.uncrackable_label(hs) == "EAP/Enterprise"
    assert wpa.eap_capture_label(hs) == "EAP/Enterprise"


def test_eap_variants_all_suppressed():
    """FT-EAP (3), EAP-SHA256 (5), EAP-Suite-B[-192] (11/12), FT-EAP-SHA384 (13)."""
    for suite in (3, 5, 11, 12, 13):
        assert wpa.crackable_pairs(_m1m2(offered=[suite])) == [], suite


def test_transition_ap_eap_client_suppressed():
    """WPA2-Enterprise/PSK transition, THIS client negotiated EAP → withheld + badged."""
    hs = _m1m2(offered=[1, 2], client=1)
    assert wpa.eapol_verdict(hs) == "uncrackable"
    assert wpa.eap_capture_label(hs) == "EAP/Enterprise"


def test_transition_ap_psk_client_over_eap_is_crackable():
    """Same transition AP, but this client negotiated PSK → crackable, no EAP badge."""
    hs = _m1m2(offered=[1, 2], client=2)
    assert wpa.eapol_verdict(hs) == "crackable"
    assert len(wpa.crackable_pairs(hs)) == 1
    assert wpa.eap_capture_label(hs) is None


def test_transition_ap_eap_unknown_client_not_badged_eap():
    """EAP+PSK offered, client unknown: withhold (could be PSK), but don't badge it
    EAP — we can't confirm which side, so no misleading 'EAP/Enterprise' line."""
    hs = _m1m2(offered=[1, 2], client=None)
    assert wpa.eapol_verdict(hs) == "unknown"
    assert wpa.crackable_pairs(hs) == []
    assert wpa.eap_capture_label(hs) is None


def test_eap_capture_label_needs_a_usable_pairing():
    """eap_capture_label fires only on a real captured 4-way, not a stray M1 — so
    the log line means 'you got a handshake but it's enterprise', not noise."""
    m1_only = _hs(_frame(1, 5, nonce=ANONCE, mic=False))
    m1_only.akm_offered = [1]
    assert wpa.uncrackable_label(m1_only) == "EAP/Enterprise"   # AKM says EAP
    assert wpa.eap_capture_label(m1_only) is None               # but no usable pair


def test_eap_capture_label_none_for_sae_and_psk():
    """The EAP badge is EAP-specific: SAE withholds too but isn't badged EAP, and a
    plain-PSK capture isn't badged at all."""
    assert wpa.eap_capture_label(_m1m2(offered=[8])) is None    # SAE → not EAP
    assert wpa.eap_capture_label(_m1m2(offered=[2])) is None    # crackable PSK


# --- PMKID (WPA*01) gate: only plain-PSK (AKM 2) HMAC-SHA1 -------------------

def test_pmkid_plain_psk_crackable():
    assert wpa.pmkid_crackable(_hs_pmkid(offered=[2], client=2))
    assert wpa.pmkid_crackable(_hs_pmkid(offered=[2]))      # unknown client, PSK-only AP


def test_pmkid_sha256_client_suppressed():
    """AKM-6 PMKID is HMAC-SHA256 — hashcat's PMKID path is SHA1-only."""
    assert not wpa.pmkid_crackable(_hs_pmkid(offered=[2, 6], client=6))


def test_pmkid_sha256_only_ap_suppressed():
    """SHA256-PSK offered with no plain PSK → the PMKID can't be SHA1."""
    assert not wpa.pmkid_crackable(_hs_pmkid(offered=[6]))


def test_pmkid_unknown_client_assumes_sha1():
    """Asymmetry vs SAE: AKM 6 is rare, so a PMKID of unknown AKM on a PSK+PSK256
    AP is assumed to be the common SHA1 one (emit) — we don't withhold."""
    assert wpa.pmkid_crackable(_hs_pmkid(offered=[2, 6], client=None))


def test_pmkid_sae_or_ft_unknown_withheld():
    """SAE/FT in the mix with unknown client → strict withhold (could be the
    uncrackable one)."""
    assert not wpa.pmkid_crackable(_hs_pmkid(offered=[2, 8]))   # PSK + SAE
    assert not wpa.pmkid_crackable(_hs_pmkid(offered=[2, 4]))   # PSK + FT-PSK


def test_ft_pmkid_suppressed():
    assert not wpa.pmkid_crackable(_hs_pmkid(offered=[4], client=4))


def test_akm6_eapol_crackable_but_pmkid_not():
    """The split that motivates two gates: an AKM-6 (PSK-SHA256) network's EAPOL
    cracks (keyver → AES-CMAC), but its PMKID (HMAC-SHA256) does not."""
    eapol = _m1m2(offered=[6], client=6)
    assert wpa.eapol_crackable(eapol)
    assert len(wpa.crackable_pairs(eapol)) == 1
    assert not wpa.pmkid_crackable(_hs_pmkid(offered=[6], client=6))


def test_hc22000_line_shape():
    hs = _hs(_frame(1, 5, nonce=ANONCE, mic=False), _frame(2, 5, nonce=SNONCE))
    line = wpa.hc22000_line("TestNet", hs, wpa.crackable_pairs(hs)[0])
    parts = line.split("*")
    assert parts[0] == "WPA" and parts[1] == "02"
    assert parts[2] == "11" * 16                 # MIC (from M2)
    assert parts[3] == "aabbccddeeff"            # AP MAC
    assert parts[6] == ANONCE.hex()              # ANonce
    assert parts[8] == "80"                      # M1+M2 (0x00) | NC bit (0x80)
