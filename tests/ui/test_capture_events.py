"""Decloak-event coverage for the shared CaptureEventDetector.

The handshake/PMKID/EAPOL paths are exercised indirectly through the
WlanInterface tests; here we focus on the AP-scoped "decloak" event the
detector synthesises from the None→SSID transition it witnesses.
"""
from __future__ import annotations

from wifit3.engine.models import AccessPoint, EapolFrame, Handshake
from wifit3.ui.capture_events import CaptureEvent, CaptureEventDetector


def _ef(msg, rc, nonce, ts):
    return EapolFrame(
        raw=bytes([msg]) + rc.to_bytes(8, "big") + nonce[:1],
        msg_num=msg,
        replay_hex=rc.to_bytes(8, "big").hex(),
        nonce=nonce,
        mic=b"\x00" * 16,
        key_data_len=0,
        timestamp=ts,
    )


def test_handshake_complete_refires_per_instance():
    """One banner per distinct 4-way (ANonce), deduped within an instance but
    re-fired on a genuine re-handshake."""
    det = CaptureEventDetector(granular_eapol=True)
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="X")
    hs = Handshake(bssid=ap.bssid, client_mac="11:22:33:44:55:66", beacon_frame=b"B")
    ap.handshakes[hs.client_mac] = hs

    # Instance 1 → one completion banner.
    hs.eapol_frames += [_ef(1, 5, b"\xaa" * 32, 100.0), _ef(2, 5, b"\x11" * 32, 100.1)]
    n1 = sum(e.kind == "handshake_complete" for e in det.poll(ap))
    assert n1 == 1
    # Re-poll with nothing new (incl. an M3 retransmit of the same instance) →
    # no repeat banner.
    hs.eapol_frames.append(_ef(3, 6, b"\xaa" * 32, 100.2))
    assert sum(e.kind == "handshake_complete" for e in det.poll(ap)) == 0
    # Instance 2 (fresh ANonce + replay base) → banner fires again.
    hs.eapol_frames += [_ef(1, 9, b"\xbb" * 32, 200.0), _ef(2, 9, b"\x22" * 32, 200.1)]
    assert sum(e.kind == "handshake_complete" for e in det.poll(ap)) == 1


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
