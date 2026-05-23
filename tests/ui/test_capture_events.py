"""Decloak-event coverage for the shared CaptureEventDetector.

The handshake/PMKID/EAPOL paths are exercised indirectly through the
WlanInterface tests; here we focus on the AP-scoped "decloak" event the
detector synthesises from the None→SSID transition it witnesses.
"""
from __future__ import annotations

from wifit3.engine.models import AccessPoint
from wifit3.ui.capture_events import CaptureEvent, CaptureEventDetector


def _ap(bssid: str = "aa:bb:cc:dd:ee:ff", ssid=None, method=None) -> AccessPoint:
    return AccessPoint(bssid=bssid, ssid=ssid, decloak_method=method)


def test_decloak_event_fires_once_on_transition():
    det = CaptureEventDetector(granular_eapol=False)
    ap = _ap()

    # First poll: AP still hidden — no event, but detector remembers it.
    assert list(det.poll(ap)) == []

    # Decloak happens (interface.py flips ssid + decloak_method) → next poll
    # yields exactly one "decloak" event with the method propagated.
    ap.ssid = "TestSSID"
    ap.decloak_method = "probe_resp"
    events = list(det.poll(ap))
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "decloak"
    assert ev.bssid == ap.bssid
    assert ev.ssid == "TestSSID"
    assert ev.method == "probe_resp"


def test_decloak_event_not_repeated():
    det = CaptureEventDetector(granular_eapol=False)
    ap = _ap()
    list(det.poll(ap))                        # observe hidden
    ap.ssid = "Foo"
    ap.decloak_method = "probe_resp"
    first = list(det.poll(ap))
    second = list(det.poll(ap))               # further beacons / probe resps
    third = list(det.poll(ap))
    assert len(first) == 1
    assert second == []
    assert third == []


def test_decloak_skipped_for_ap_never_seen_hidden():
    """An AP that already has an SSID on first observation isn't a decloak —
    the detector never witnessed it hidden during its lifetime. Matches the
    Focus-entered-on-decloaked-AP semantics."""
    det = CaptureEventDetector(granular_eapol=False)
    ap = _ap(ssid="Public_WiFi", method=None)
    assert list(det.poll(ap)) == []
    # Even if decloak_method gets retroactively set (shouldn't happen, but
    # defensively): no event, because we never marked the BSSID hidden.
    ap.decloak_method = "probe_resp"
    assert list(det.poll(ap)) == []


def test_decloak_method_assoc_req_propagates():
    det = CaptureEventDetector(granular_eapol=False)
    ap = _ap()
    list(det.poll(ap))
    ap.ssid = "OtherTestSSID"
    ap.decloak_method = "assoc_req"
    events = list(det.poll(ap))
    assert len(events) == 1
    assert events[0].method == "assoc_req"


def test_decloak_treats_hidden_marker_as_hidden():
    """Belt-and-suspenders: some upstream paths set ssid='<hidden>' instead
    of None. The detector treats both the same for transition detection."""
    det = CaptureEventDetector(granular_eapol=False)
    ap = _ap(ssid="<hidden>")
    assert list(det.poll(ap)) == []
    ap.ssid = "Foo"
    ap.decloak_method = "probe_resp"
    events = list(det.poll(ap))
    assert len(events) == 1
    assert events[0].ssid == "Foo"


def test_reset_clears_decloak_state():
    det = CaptureEventDetector(granular_eapol=False)
    ap = _ap()
    list(det.poll(ap))
    ap.ssid = "Foo"
    ap.decloak_method = "probe_resp"
    assert len(list(det.poll(ap))) == 1

    det.reset()
    # After reset, the detector has forgotten everything — but the AP is no
    # longer hidden, so it won't be marked seen_hidden again, and we won't
    # re-emit. This is correct: reset is a fresh start, not a replay.
    assert list(det.poll(ap)) == []
