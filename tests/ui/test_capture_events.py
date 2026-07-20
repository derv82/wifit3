"""Decloak-event coverage for the shared CaptureEventDetector.

The handshake/PMKID/EAPOL paths are exercised indirectly through the
WlanInterface tests; here we focus on the AP-scoped "decloak" event the
detector synthesises from the None→SSID transition it witnesses.
"""
from __future__ import annotations

from wifit3.models import AccessPoint, HandshakeMessage, Handshake
from wifit3.ui.capture_events import CaptureEventDetector, CaptureKind


def _ef(msg, rc, nonce, ts):
    """A *usable* EAPOL frame (real MIC + complete 802.1X payload), so an M2 is a
    valid MIC keystone and the detector can fire a real completion banner."""
    return HandshakeMessage(
        raw=bytes([msg]) + rc.to_bytes(8, "big") + nonce[:1],
        msg_num=msg,
        replay_hex=rc.to_bytes(8, "big").hex(),
        nonce=nonce,
        mic=b"\x11" * 16,
        key_data_len=0,
        eapol_payload=bytes(120),
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
    hs.messages += [_ef(1, 5, b"\xaa" * 32, 100.0), _ef(2, 5, b"\x11" * 32, 100.1)]
    n1 = sum(e.kind == "handshake_complete" for e in det.poll(ap))
    assert n1 == 1
    # Re-poll with nothing new (incl. an M3 retransmit of the same instance) →
    # no repeat banner.
    hs.messages.append(_ef(3, 6, b"\xaa" * 32, 100.2))
    assert sum(e.kind == "handshake_complete" for e in det.poll(ap)) == 0
    # Instance 2 (fresh ANonce + replay base) → banner fires again.
    hs.messages += [_ef(1, 9, b"\xbb" * 32, 200.0), _ef(2, 9, b"\x22" * 32, 200.1)]
    assert sum(e.kind == "handshake_complete" for e in det.poll(ap)) == 1


def _hs_with(ap, *, offered, client=None, pmkid=None):
    hs = Handshake(bssid=ap.bssid, client_mac="11:22:33:44:55:66", beacon_frame=b"B")
    hs.akm_offered = list(offered)
    hs.akm_client = client
    hs.pmkid = pmkid
    ap.handshakes[hs.client_mac] = hs
    return hs


def test_sae_handshake_emits_eapol_but_no_completion():
    """SAE 4-way: the per-frame trace still fires (so the user sees their phone
    connect) — but no completion banner, since it's useless."""
    det = CaptureEventDetector(granular_eapol=True)
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="X")
    hs = _hs_with(ap, offered=[8])               # SAE-only AP
    hs.messages += [_ef(1, 5, b"\xaa" * 32, 100.0), _ef(2, 5, b"\x11" * 32, 100.1)]
    events = list(det.poll(ap))

    assert not any(e.kind == CaptureKind.HANDSHAKE for e in events)
    eapols = [e for e in events if e.kind == CaptureKind.EAPOL]
    assert len(eapols) == 2


def test_owe_and_eap_emit_withheld_banner_sae_stays_silent():
    """A withheld 4-way is announced once per AP with its badge (so it's not a silent
    capture failure) for the false-positive AKMs EAP + OWE; SAE/FT stay silent
    (they were never mis-reported as crackable)."""
    det = CaptureEventDetector(granular_eapol=False)

    owe = AccessPoint(bssid="aa:bb:cc:dd:ee:f5", ssid="OpenNet")
    hs = _hs_with(owe, offered=[18])                          # OWE-only AP
    hs.messages += [_ef(1, 5, b"\xaa" * 32, 100.0), _ef(2, 5, b"\x11" * 32, 100.1)]
    ev = [e for e in det.poll(owe) if e.kind == CaptureKind.UNCRACKABLE_HANDSHAKE]
    assert len(ev) == 1 and ev[0].value == "OWE"
    assert not any(e.kind == CaptureKind.UNCRACKABLE_HANDSHAKE for e in det.poll(owe))  # once/AP

    eap = AccessPoint(bssid="aa:bb:cc:dd:ee:f6", ssid="CorpNet")
    hs = _hs_with(eap, offered=[1])                           # EAP-only AP
    hs.messages += [_ef(1, 5, b"\xaa" * 32, 100.0), _ef(2, 5, b"\x11" * 32, 100.1)]
    ev = [e for e in det.poll(eap) if e.kind == CaptureKind.UNCRACKABLE_HANDSHAKE]
    assert len(ev) == 1 and ev[0].value == "EAP/Enterprise"

    sae = AccessPoint(bssid="aa:bb:cc:dd:ee:f7", ssid="Wpa3Net")
    hs = _hs_with(sae, offered=[8])                           # SAE-only AP → silent
    hs.messages += [_ef(1, 5, b"\xaa" * 32, 100.0), _ef(2, 5, b"\x11" * 32, 100.1)]
    assert not any(e.kind == CaptureKind.UNCRACKABLE_HANDSHAKE for e in det.poll(sae))


def test_wpa2_handshake_emits_completion_banner():
    det = CaptureEventDetector(granular_eapol=True)
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="X")
    hs = _hs_with(ap, offered=[2])               # WPA2-PSK
    hs.messages += [_ef(1, 5, b"\xaa" * 32, 100.0), _ef(2, 5, b"\x11" * 32, 100.1)]
    events = list(det.poll(ap))
    assert sum(e.kind == CaptureKind.HANDSHAKE for e in events) == 1


def test_sae_pmkid_suppressed_wpa2_pmkid_emitted():
    det = CaptureEventDetector(granular_eapol=False)
    sae_ap = AccessPoint(bssid="aa:bb:cc:dd:ee:f1", ssid="X")
    _hs_with(sae_ap, offered=[8], pmkid=b"\x01" * 16)
    assert not any(e.kind == CaptureKind.PMKID for e in det.poll(sae_ap))

    wpa2_ap = AccessPoint(bssid="aa:bb:cc:dd:ee:f2", ssid="Y")
    _hs_with(wpa2_ap, offered=[2], pmkid=b"\x02" * 16)
    assert sum(e.kind == CaptureKind.PMKID for e in det.poll(wpa2_ap)) == 1


def test_transition_pmkid_withheld_until_m2_confirms_psk():
    """A transition-AP PMKID is withheld while the client AKM is unknown, then
    surfaces once an M2 confirms PSK — proving we don't burn the dedup slot on a
    suppressed capture."""
    det = CaptureEventDetector(granular_eapol=False)
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:f3", ssid="X")
    hs = _hs_with(ap, offered=[2, 8], pmkid=b"\x03" * 16)   # transition, client unknown
    assert not any(e.kind == CaptureKind.PMKID for e in det.poll(ap))
    hs.akm_client = 2                                       # M2 says PSK
    assert sum(e.kind == CaptureKind.PMKID for e in det.poll(ap)) == 1


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


# ----- Recovered-credential events (WEP key, WPS PIN/PSK/PBC) ----------------

def _ap_named(bssid: str = "aa:bb:cc:dd:ee:ff", ssid: str = "Net") -> AccessPoint:
    """A non-hidden AP, so cred-event polls aren't entangled with decloak."""
    return AccessPoint(bssid=bssid, ssid=ssid)


def test_wep_key_event_fires_once():
    det = CaptureEventDetector(granular_eapol=False)
    ap = _ap_named()
    assert list(det.poll(ap)) == []                  # nothing recovered yet

    ap.wep_key = bytes.fromhex("6162636465")         # b"abcde"
    events = [e for e in det.poll(ap) if e.kind == CaptureKind.WEP_KEY]
    assert len(events) == 1
    assert events[0].value == "6162636465"           # rendered from .hex()
    assert events[0].bssid == ap.bssid
    # Subsequent polls don't re-announce.
    assert [e for e in det.poll(ap) if e.kind == CaptureKind.WEP_KEY] == []


def test_wps_pin_emits_pin_and_psk():
    """A PIN win is two atomic facts → two events (PIN + PSK), one log line each."""
    det = CaptureEventDetector(granular_eapol=False)
    ap = _ap_named()
    ap.wps_pin = "12345670"
    ap.wps_pin_psk = "hunter2"
    got = {(e.kind, e.value) for e in det.poll(ap)}
    assert (CaptureKind.WPS_PIN, "12345670") in got
    assert (CaptureKind.WPS_PSK, "hunter2") in got
    # PBC field untouched → no PBC event ever.
    assert not any(e.kind == CaptureKind.WPS_PBC for e in det.poll(ap))


def test_wps_pbc_emits_only_psk_distinct_kind():
    """PBC recovers a passphrase but no PIN → a single, distinctly-kinded event
    so the log can label it 'via PushButton'."""
    det = CaptureEventDetector(granular_eapol=False)
    ap = _ap_named()
    ap.wps_pbc_psk = "latte123"
    assert [(e.kind, e.value) for e in det.poll(ap)] == [
        (CaptureKind.WPS_PBC, "latte123")
    ]
    assert list(det.poll(ap)) == []                  # fire-once


def test_credential_event_re_emits_after_reset():
    """reset() forgets announce-state; a still-set credential re-announces — a
    fresh start, consistent with the decloak reset semantics."""
    det = CaptureEventDetector(granular_eapol=False)
    ap = _ap_named()
    ap.wep_key = b"\x01\x02\x03\x04\x05"
    assert len([e for e in det.poll(ap) if e.kind == CaptureKind.WEP_KEY]) == 1
    det.reset()
    assert len([e for e in det.poll(ap) if e.kind == CaptureKind.WEP_KEY]) == 1
