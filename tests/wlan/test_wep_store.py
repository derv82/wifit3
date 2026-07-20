"""Tests for the passive WEP IV collector + rate/ETA estimation."""
import pytest

from wifit3.wlan.wep_store import RateTracker, WepCaptureStore

BSSID = "11:22:33:44:55:66"


def test_dedup_counts_unique_vs_total():
    c = WepCaptureStore()
    c.record(BSSID, b"\x01\x02\x03")
    c.record(BSSID, b"\x01\x02\x03")   # duplicate IV
    c.record(BSSID, b"\x04\x05\x06")
    stats = c.stats(BSSID)
    assert stats.total_frames == 3
    assert stats.unique_ivs == 2
    assert c.unique_count(BSSID) == 2


def test_per_bssid_isolation():
    c = WepCaptureStore()
    c.record("aa:aa:aa:aa:aa:aa", b"\x01\x01\x01")
    c.record("bb:bb:bb:bb:bb:bb", b"\x01\x01\x01")
    assert c.unique_count("aa:aa:aa:aa:aa:aa") == 1
    assert c.unique_count("bb:bb:bb:bb:bb:bb") == 1


def test_record_returns_same_stats_object():
    """The interface attaches the returned WepStats to the AP once, then
    relies on later records mutating that same instance in place."""
    c = WepCaptureStore()
    s1 = c.record(BSSID, b"\x01\x02\x03")
    s2 = c.record(BSSID, b"\x04\x05\x06")
    assert s1 is s2
    assert s1.unique_ivs == 2


def test_rate_over_known_span():
    c = WepCaptureStore()
    c.record(BSSID, b"\x00\x00\x01", now=0.0)
    c.record(BSSID, b"\x00\x00\x02", now=1.0)
    c.record(BSSID, b"\x00\x00\x03", now=2.0)
    # 3 unique IVs across a 2.0s span → 1.5 IV/s.
    assert c.rate(BSSID, now=2.0) == pytest.approx(1.5)


def test_rate_decays_when_idle():
    c = RateTracker(window_s=10.0)
    c.mark(now=0.0)
    c.mark(now=1.0)
    # Long after the window, all events have aged out → zero.
    assert c.rate(now=100.0) == 0.0


def test_eta_estimates_remaining_time():
    c = WepCaptureStore()
    for i, t in enumerate([0.0, 1.0, 2.0]):
        c.record(BSSID, bytes([0, 0, i]), now=t)
    # 3 IVs at 1.5/s; 7 remaining to reach 10 → ~4.67s.
    eta = c.eta_seconds(BSSID, target=10, now=2.0)
    assert eta == pytest.approx(7 / 1.5)


def test_eta_zero_when_target_reached():
    c = WepCaptureStore()
    c.record(BSSID, b"\x00\x00\x01", now=0.0)
    c.record(BSSID, b"\x00\x00\x02", now=1.0)
    assert c.eta_seconds(BSSID, target=2, now=1.0) == 0.0


def test_eta_none_when_no_rate():
    c = WepCaptureStore()
    # No IVs recorded for this BSSID → no rate → can't estimate.
    assert c.eta_seconds(BSSID, target=10, now=5.0) is None


def test_crack_eta_tracks_samples_not_unique_ivs():
    """The crack ETA must use the SAMPLE rate, not the unique-IV rate: samples
    (usable IVs) gate cracking and lag unique IVs (the gap = organic traffic).
    Regression: the UI used to gate on unique IVs, so cracking "should start"
    at 10k IVs but didn't begin until ~10k samples (often ~2x the IVs)."""
    c = WepCaptureStore()
    # Lots of unique IVs (fast)...
    for i in range(20):
        c.record(BSSID, bytes([0, i // 256, i % 256]), now=float(i) * 0.1)
    # ...but only a few crack samples (slow): 2 samples across a 2.0s span = 1/s.
    c.record_crack_sample(BSSID, b"\x01\x00\x00", b"\x00" * 16, now=0.0)
    c.record_crack_sample(BSSID, b"\x01\x00\x01", b"\x00" * 16, now=2.0)
    assert c.crack_rate(BSSID, now=2.0) == pytest.approx(1.0)
    # 2 samples at 1/s, 8 remaining to reach 10 → 8s (NOT driven by the 20 IVs).
    assert c.crack_eta_seconds(BSSID, target=10, now=2.0) == pytest.approx(8.0)


def test_crack_eta_none_when_no_samples():
    c = WepCaptureStore()
    c.record(BSSID, b"\x00\x00\x01", now=0.0)   # unique IV but no crack sample
    assert c.crack_eta_seconds(BSSID, target=10, now=1.0) is None


# ---- ARP replay candidates -------------------------------------------------

def test_arp_candidate_retained_when_size_matches():
    c = WepCaptureStore()
    frame = b"\x00" * 68          # canonical WEP ARP-request length
    assert c.record_broadcast_frame(BSSID, frame) is True
    assert c.arp_candidate_count(BSSID) == 1
    assert c.arp_candidates(BSSID) == [frame]


def test_arp_candidate_rejected_on_wrong_size():
    c = WepCaptureStore()
    assert c.record_broadcast_frame(BSSID, b"\x00" * 100) is False
    assert c.arp_candidate_count(BSSID) == 0


def test_arp_candidates_stored_both_directions():
    """We keep ALL ARP-sized broadcast frames regardless of direction: the
    replay engine re-addresses them and prunes non-yielding ones later."""
    c = WepCaptureStore()
    for i in range(5):
        c.record_broadcast_frame(BSSID, bytes([i]) + b"\x00" * 67)
    assert c.arp_candidate_count(BSSID) == 5


def test_arp_ring_capped():
    from wifit3.wlan.wep_store import ARP_RING_MAXLEN
    c = WepCaptureStore()
    for i in range(ARP_RING_MAXLEN + 50):
        c.record_broadcast_frame(BSSID, i.to_bytes(2, "big") + b"\x00" * 66)
    assert c.arp_candidate_count(BSSID) == ARP_RING_MAXLEN


def test_broadcast_seen_counts_all_sizes():
    c = WepCaptureStore()
    c.record_broadcast_frame(BSSID, b"\x00" * 68)     # stored
    c.record_broadcast_frame(BSSID, b"\x00" * 368)    # wrong size, still 'seen'
    assert c.broadcast_seen_count(BSSID) == 2
    assert c.arp_candidate_count(BSSID) == 1


def test_chop_candidates_include_non_arp_broadcast_frames():
    """ChopChop's seed pool is broader than the ARP-replay one: any broadcast
    WEP data frame of usable size (incl. IP broadcasts) is kept, even though
    only the ARP-sized one lands in the (replay) ARP ring."""
    c = WepCaptureStore()
    c.record_broadcast_frame(BSSID, b"\x00" * 68)     # ARP-sized → both rings
    c.record_broadcast_frame(BSSID, b"\x00" * 90)     # non-ARP IP-ish → chop only
    assert c.arp_candidate_count(BSSID) == 1        # ARP ring: ARP-sized only
    assert len(c.chop_candidates(BSSID)) == 2       # chop ring: both


def test_chop_candidates_skip_runts():
    from wifit3.wlan.wep_store import CHOP_MIN_LEN
    c = WepCaptureStore()
    c.record_broadcast_frame(BSSID, b"\x00" * (CHOP_MIN_LEN - 1))   # too short
    assert c.chop_candidates(BSSID) == []


def test_crack_samples_dedup_by_iv():
    c = WepCaptureStore()
    assert c.record_crack_sample(BSSID, b"\x01\x02\x03", b"\xaa" * 16) is True
    assert c.record_crack_sample(BSSID, b"\x01\x02\x03", b"\xbb" * 16) is False  # dup IV
    assert c.record_crack_sample(BSSID, b"\x04\x05\x06", b"\xcc" * 16) is True
    assert c.crack_sample_count(BSSID) == 2
    assert c.crack_samples(BSSID)[0] == (b"\x01\x02\x03", b"\xaa" * 16)
