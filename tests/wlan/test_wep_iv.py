"""Tests for the passive WEP IV collector + rate/ETA estimation."""
import pytest

from wifit3.wlan.wep_iv import RateTracker, WepIvCollector

BSSID = "11:22:33:44:55:66"


def test_dedup_counts_unique_vs_total():
    c = WepIvCollector()
    c.record(BSSID, b"\x01\x02\x03")
    c.record(BSSID, b"\x01\x02\x03")   # duplicate IV
    c.record(BSSID, b"\x04\x05\x06")
    stats = c.stats(BSSID)
    assert stats.total_frames == 3
    assert stats.unique_ivs == 2
    assert c.unique_count(BSSID) == 2


def test_per_bssid_isolation():
    c = WepIvCollector()
    c.record("aa:aa:aa:aa:aa:aa", b"\x01\x01\x01")
    c.record("bb:bb:bb:bb:bb:bb", b"\x01\x01\x01")
    assert c.unique_count("aa:aa:aa:aa:aa:aa") == 1
    assert c.unique_count("bb:bb:bb:bb:bb:bb") == 1


def test_record_returns_same_stats_object():
    """The interface attaches the returned WepStats to the AP once, then
    relies on later records mutating that same instance in place."""
    c = WepIvCollector()
    s1 = c.record(BSSID, b"\x01\x02\x03")
    s2 = c.record(BSSID, b"\x04\x05\x06")
    assert s1 is s2
    assert s1.unique_ivs == 2


def test_rate_over_known_span():
    c = WepIvCollector()
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
    c = WepIvCollector()
    for i, t in enumerate([0.0, 1.0, 2.0]):
        c.record(BSSID, bytes([0, 0, i]), now=t)
    # 3 IVs at 1.5/s; 7 remaining to reach 10 → ~4.67s.
    eta = c.eta_seconds(BSSID, target=10, now=2.0)
    assert eta == pytest.approx(7 / 1.5)


def test_eta_zero_when_target_reached():
    c = WepIvCollector()
    c.record(BSSID, b"\x00\x00\x01", now=0.0)
    c.record(BSSID, b"\x00\x00\x02", now=1.0)
    assert c.eta_seconds(BSSID, target=2, now=1.0) == 0.0


def test_eta_none_when_no_rate():
    c = WepIvCollector()
    # No IVs recorded for this BSSID → no rate → can't estimate.
    assert c.eta_seconds(BSSID, target=10, now=5.0) is None
