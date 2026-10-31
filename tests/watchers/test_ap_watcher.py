"""ApWatcher and its detectors."""
from __future__ import annotations

from wifit3.models import AccessPoint, Handshake, HandshakeMessage
from wifit3.persist.config import Config
from wifit3.persist.vault import Vault
from wifit3.watchers.ap_watcher import (
    ApDecloaked, ApWatcher, CaptureEventDetector, DecloakDetector, EapolFrameDetector,
    HandshakeCaptured, HandshakeDetector, PmkidCaptured, PmkidDetector, UncrackableHandshakeDetector,
    WepKeyDetector, WepKeyRecovered, WpsPbcPskDetector, WpsPbcPskRecovered, WpsPinDetector,
    WpsPinRecovered, WpsPskDetector, WpsPskRecovered,
)
from wifit3.wlan.array import WlanArray

_CLIENT = "11:22:33:44:55:66"


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


def _hs_with(ap, *, offered=(2,), pmkid=None, beacon_frame=b"B"):
    hs = Handshake(bssid=ap.bssid, client_mac=_CLIENT, beacon_frame=beacon_frame)
    hs.akm_offered = list(offered)
    hs.pmkid = pmkid
    ap.handshakes[hs.client_mac] = hs
    return hs


def _m1_m2(hs):
    hs.messages += [_ef(1, 5, b"\xaa" * 32, 100.0), _ef(2, 5, b"\x11" * 32, 100.1)]


# ----- HandshakeDetector -------------------------------------------------------

def test_handshake_reported_per_instance_and_once_more_when_authorized():
    det = HandshakeDetector()
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="X", channel=6)
    hs = _hs_with(ap)

    _m1_m2(hs)
    events = det.poll(ap, _CLIENT, hs)
    assert [e.pair_label for e in events] == ["M1+M2"]
    assert events[0].channel == 6
    # Its M3 makes the instance authorized: one more event, so the M2+M3 gets saved.
    hs.messages.append(_ef(3, 6, b"\xaa" * 32, 100.2))
    assert [e.pair_label for e in det.poll(ap, _CLIENT, hs)] == ["M2+M3"]
    # An M3 retransmit of the same instance: nothing.
    hs.messages.append(_ef(3, 6, b"\xaa" * 32, 100.3))
    assert det.poll(ap, _CLIENT, hs) == []
    # A fresh ANonce is a new instance.
    hs.messages += [_ef(1, 9, b"\xbb" * 32, 200.0), _ef(2, 9, b"\x22" * 32, 200.1)]
    assert len(det.poll(ap, _CLIENT, hs)) == 1


def test_handshake_reported_when_the_beacon_arrives_after_the_frames():
    det = HandshakeDetector()
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="X")
    hs = _hs_with(ap, beacon_frame=None)
    _m1_m2(hs)
    assert det.poll(ap, _CLIENT, hs) == []

    hs.beacon_frame = b"B"
    assert len(det.poll(ap, _CLIENT, hs)) == 1


def test_no_handshake_for_sae_or_reserved_akm():
    det = HandshakeDetector()
    for bssid, offered in (("aa:bb:cc:dd:ee:f1", [8]), ("aa:bb:cc:dd:ee:f2", [42])):
        ap = AccessPoint(bssid=bssid, ssid="X")
        hs = _hs_with(ap, offered=offered)
        _m1_m2(hs)
        assert det.poll(ap, _CLIENT, hs) == []


# ----- EapolFrameDetector ------------------------------------------------------

def test_every_frame_reported_once_and_pmkid_flag_only_on_m1():
    det = EapolFrameDetector()
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="X")
    hs = _hs_with(ap, offered=[8], pmkid=b"\x01" * 16)   # SAE: frames still reported
    _m1_m2(hs)

    events = det.poll(ap, _CLIENT, hs)
    assert [(e.msg_num, e.has_pmkid) for e in events] == [(1, True), (2, None)]
    assert det.poll(ap, _CLIENT, hs) == []
    hs.messages.append(_ef(1, 5, b"\xaa" * 32, 100.2))   # retransmit: a new frame
    assert len(det.poll(ap, _CLIENT, hs)) == 1


def test_every_frame_reported_across_message_pruning():
    det = EapolFrameDetector()
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="X")
    hs = _hs_with(ap)
    reported = 0
    for rc in range(400):
        hs.add_message(_ef(1, rc + 1, bytes([rc % 256]) * 32, float(rc)))
        reported += len(det.poll(ap, _CLIENT, hs))
    assert len(hs.messages) < 400
    assert reported == 400


# ----- UncrackableHandshakeDetector --------------------------------------------

def test_uncrackable_handshake_badged_once_per_ap():
    det = UncrackableHandshakeDetector()
    for bssid, offered, badge in (
        ("aa:bb:cc:dd:ee:f5", [18], "OWE"),
        ("aa:bb:cc:dd:ee:f6", [1], "EAP/Enterprise"),
        ("aa:bb:cc:dd:ee:f7", [8], "SAE"),
        ("aa:bb:cc:dd:ee:f8", [4], "FT"),
        ("aa:bb:cc:dd:ee:f9", [42], "Unsupported AKM"),
    ):
        ap = AccessPoint(bssid=bssid, ssid="Net")
        hs = _hs_with(ap, offered=offered)
        _m1_m2(hs)
        assert [e.akm_label for e in det.poll(ap, _CLIENT, hs)] == [badge]
        hs.messages.append(_ef(3, 6, b"\xaa" * 32, 100.2))
        assert det.poll(ap, _CLIENT, hs) == []


def test_crackable_handshake_is_not_badged():
    det = UncrackableHandshakeDetector()
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="X")
    hs = _hs_with(ap, offered=[2])
    _m1_m2(hs)
    assert det.poll(ap, _CLIENT, hs) == []


# ----- PmkidDetector -----------------------------------------------------------

def test_pmkid_reported_once_for_wpa2_and_never_for_sae_or_mixed_enterprise():
    det = PmkidDetector()
    wpa2_ap = AccessPoint(bssid="aa:bb:cc:dd:ee:f2", ssid="Y")
    hs = _hs_with(wpa2_ap, offered=[2], pmkid=b"\x02" * 16)
    assert len(det.poll(wpa2_ap, _CLIENT, hs)) == 1
    assert det.poll(wpa2_ap, _CLIENT, hs) == []

    for bssid, offered in (("aa:bb:cc:dd:ee:f1", [8]), ("aa:bb:cc:dd:ee:f4", [1, 2])):
        ap = AccessPoint(bssid=bssid, ssid="X")
        hs = _hs_with(ap, offered=offered, pmkid=b"\x01" * 16)
        assert det.poll(ap, _CLIENT, hs) == []


def test_transition_pmkid_waits_for_m2_to_confirm_psk():
    det = PmkidDetector()
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:f3", ssid="X")
    hs = _hs_with(ap, offered=[2, 8], pmkid=b"\x03" * 16)   # transition, client AKM unknown
    assert det.poll(ap, _CLIENT, hs) == []
    hs.pmkid_akm = 2                                        # M2 says PSK
    assert len(det.poll(ap, _CLIENT, hs)) == 1


# ----- DecloakDetector ---------------------------------------------------------

def test_decloak_reported_once_with_its_method():
    det = DecloakDetector()
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid=None)
    assert det.poll(ap) == []

    ap.ssid = "TestSSID"
    ap.decloak_method = "probe_resp"
    assert det.poll(ap) == [ApDecloaked(bssid=ap.bssid, ssid="TestSSID", channel=ap.channel,
                                        method="probe_resp")]
    assert det.poll(ap) == []


def test_hidden_marker_counts_as_hidden():
    det = DecloakDetector()
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="<hidden>")
    assert det.poll(ap) == []
    ap.ssid = "Foo"
    assert len(det.poll(ap)) == 1


def test_no_decloak_for_an_ap_never_seen_hidden():
    det = DecloakDetector()
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="Public_WiFi", decloak_method="probe_resp")
    assert det.poll(ap) == []


# ----- Credential detectors ----------------------------------------------------

def test_wep_key_reported_once_as_hex():
    det = WepKeyDetector()
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="Net")
    assert det.poll(ap) == []
    ap.wep_key = bytes.fromhex("6162636465")
    assert det.poll(ap) == [WepKeyRecovered(bssid=ap.bssid, ssid="Net", channel=ap.channel,
                                            key_hex="6162636465")]
    assert det.poll(ap) == []


def test_wps_pin_reported_once_including_an_empty_pin():
    det = WpsPinDetector()
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="Net")
    assert det.poll(ap) == []
    ap.wps_pin = ""
    assert [e.pin for e in det.poll(ap)] == [""]
    assert det.poll(ap) == []


def test_wps_psk_and_pbc_psk_are_separate_events():
    psk_det, pbc_det = WpsPskDetector(), WpsPbcPskDetector()
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="Net")
    ap.wps_pin_psk = "hunter2"
    assert [type(e) for e in psk_det.poll(ap)] == [WpsPskRecovered]
    assert pbc_det.poll(ap) == []

    ap.wps_pbc_psk = "latte123"
    assert [(type(e), e.psk) for e in pbc_det.poll(ap)] == [(WpsPbcPskRecovered, "latte123")]
    assert psk_det.poll(ap) == [] and pbc_det.poll(ap) == []


def test_capture_event_detector_reports_every_credential():
    det = CaptureEventDetector()
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="Net")
    ap.wep_key = b"abcde"
    ap.wps_pin = "12345670"
    ap.wps_pin_psk = "hunter2"
    ap.wps_pbc_psk = "latte123"
    assert [type(e) for e in det.poll(ap, frozenset())] == [
        WepKeyRecovered, WpsPinRecovered, WpsPskRecovered, WpsPbcPskRecovered,
    ]


# ----- CaptureEventDetector: forged MACs ---------------------------------------

def test_forged_mac_reports_nothing_and_its_pmkid_stays_reported():
    det = CaptureEventDetector()
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="X")
    hs = _hs_with(ap, pmkid=b"\x02" * 16)
    _m1_m2(hs)
    assert det.poll(ap, frozenset({_CLIENT})) == []

    types = [type(e) for e in det.poll(ap, frozenset())]
    assert HandshakeCaptured in types
    assert PmkidCaptured not in types


# ----- ApWatcher ---------------------------------------------------------------

def _array_with_handshake() -> tuple[WlanArray, AccessPoint]:
    array = WlanArray()
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="X", channel=6)
    _m1_m2(_hs_with(ap))
    array.access_points[ap.bssid] = ap
    return array, ap


def _handshakes(events) -> list[HandshakeCaptured]:
    return [e for e in events if isinstance(e, HandshakeCaptured)]


def test_watcher_saves_a_handshake_and_reports_it_once(monkeypatch):
    monkeypatch.setattr(Config, "auto_crack", None)
    array, _ = _array_with_handshake()
    watcher = ApWatcher(Vault())

    handshakes = _handshakes(watcher.poll(array))
    assert len(handshakes) == 1
    assert handshakes[0].save_result is not None and handshakes[0].save_result.was_new
    assert _handshakes(watcher.poll(array)) == []


def test_watcher_reports_an_unsaved_handshake_while_the_ap_is_hidden(monkeypatch):
    monkeypatch.setattr(Config, "auto_crack", None)
    array, ap = _array_with_handshake()
    ap.ssid = None

    handshakes = _handshakes(ApWatcher(Vault()).poll(array))
    assert len(handshakes) == 1 and handshakes[0].save_result is None


def test_watcher_skips_twins(monkeypatch):
    monkeypatch.setattr(Config, "auto_crack", None)
    array, ap = _array_with_handshake()
    array.mark_evil_twin(ap.bssid)

    assert ApWatcher(Vault()).poll(array) == []


def test_watcher_reports_a_silenced_ap_once_unsilenced(monkeypatch):
    monkeypatch.setattr(Config, "auto_crack", None)
    array, ap = _array_with_handshake()
    watcher = ApWatcher(Vault())

    monkeypatch.setattr(Config, "silenced_bssids", [ap.bssid])
    assert watcher.poll(array) == []
    monkeypatch.setattr(Config, "silenced_bssids", [])
    assert len(_handshakes(watcher.poll(array))) == 1


def test_watcher_forgets_events_when_the_array_changes(monkeypatch):
    monkeypatch.setattr(Config, "auto_crack", None)
    watcher = ApWatcher(Vault())
    for _ in range(2):
        array, _ = _array_with_handshake()
        assert len(_handshakes(watcher.poll(array))) == 1


def test_watcher_saves_a_hidden_aps_handshake_once_its_ssid_is_known(monkeypatch):
    monkeypatch.setattr(Config, "auto_crack", None)
    array, ap = _array_with_handshake()
    ap.ssid = None
    watcher = ApWatcher(Vault())
    assert [e.save_result for e in _handshakes(watcher.poll(array))] == [None]
    assert watcher.poll(array) == []

    array.decloak(ap, "X", "probe_resp")
    events = watcher.poll(array)
    assert isinstance(events[0], ApDecloaked)
    handshakes = _handshakes(events)
    assert len(handshakes) == 1
    assert handshakes[0].ssid == "X"
    assert handshakes[0].save_result is not None and handshakes[0].save_result.was_new
    assert watcher.poll(array) == []
