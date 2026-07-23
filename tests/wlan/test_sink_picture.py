"""Picture edge cases for WlanSink, ported from the old WlanInterface tests: sibling clustering,
client attribution (to_ds / group MACs), transition-AP PMKID classification, WPA3/PMF flags,
decloak method stickiness, and the WEP guards."""

from wifit3.wlan.sink import WlanSink

from tests.frames import pkt

W0 = "wlan0"


def _beacon(overrides):
    base = {"type": "beacon", "bssid": "aa:bb:cc:dd:ee:ff", "rssi": -40, "ssid": "X", "channel": 6}
    base.update(overrides)
    return pkt(base)


def _seed_beacon(s, bssid, channel, ssid="Foo"):
    s.update(pkt({"type": "beacon", "bssid": bssid, "rssi": -60, "ssid": ssid,
                  "channel": channel}), W0)


def test_encryption_order_independent_wep_then_wpa2_upgrades_and_sticks():
    s = WlanSink()
    b = "aa:bb:cc:dd:ee:ff"
    s.update(_beacon({"bssid": b, "encryption": "WEP", "akms": []}), W0)
    assert s.access_points[b].encryption == "WEP"          # provisional
    s.update(_beacon({"bssid": b, "encryption": "WPA2-PSK-CCMP", "akms": ["PSK"]}), W0)
    ap = s.access_points[b]
    assert ap.encryption == "WPA2-PSK-CCMP" and ap.akms == ["PSK"]
    s.update(_beacon({"bssid": b, "encryption": "WEP", "akms": []}), W0)   # can't downgrade
    assert s.access_points[b].encryption == "WPA2-PSK-CCMP"


def test_wep_ivs_ignored_for_non_wep_ap():
    s = WlanSink()
    b = "12:22:33:44:55:66"
    s.update(pkt({"type": "beacon", "bssid": b, "rssi": -40, "ssid": "W", "channel": 6,
                  "encryption": "WPA2", "raw": b"\x00" * 36}), W0)
    s.update(pkt({"type": "wep_data", "bssid": b, "source": "aa:bb:cc:dd:ee:01",
                  "dest": "aa:bb:cc:dd:ee:01", "rssi": -45, "wep_iv": b"\x01\x02\x03",
                  "raw": b"\x00" * 40}), W0)
    assert s.access_points[b].wep is None


def test_broadcast_wep_stored_as_arp_candidate_either_direction():
    s = WlanSink()
    b = "12:22:33:44:55:66"
    s.update(pkt({"type": "beacon", "bssid": b, "rssi": -40, "ssid": "W", "channel": 6,
                  "encryption": "WEP", "raw": b"\x00" * 36}), W0)
    for src, to_ds in (("aa:bb:cc:dd:ee:01", True), ("aa:bb:cc:dd:ee:02", False)):
        s.update(pkt({"type": "wep_data", "bssid": b, "source": src, "dest": "ff:ff:ff:ff:ff:ff",
                      "to_ds": to_ds, "rssi": -45, "wep_iv": b"\x09\x08\x07", "raw": b"\x00" * 68}), W0)
    assert s.wep_store.arp_candidate_count(b) == 2
    assert s.wep_store.broadcast_seen_count(b) == 2


def test_real_client_creates_handshake_with_eapol_frames():
    s = WlanSink()
    b = "aa:bb:cc:dd:ee:ff"
    s.update(_beacon({"bssid": b, "channel": 1, "encryption": "WPA2", "raw": b"\x00" * 36}), W0)
    client = "12:22:33:44:55:66"
    s.update(pkt({"type": "eapol", "bssid": b, "source": b, "dest": client, "rssi": -45,
                  "raw": b"\x00" * 100, "eapol_msg_num": 1, "eapol_replay_counter": b"\x00" * 8,
                  "eapol_nonce": b"\x01" * 32, "eapol_mic": b"\x00" * 16, "eapol_key_data_len": 0,
                  "eapol_payload": b"\x00" * 99}), W0)
    assert client in s.clients
    assert len(s.access_points[b].handshakes[client].messages) == 1


def test_to_ds_client_is_sender_not_addr3_da():
    s = WlanSink()
    ap, client, far_da = "aa:bb:cc:dd:ee:ff", "12:22:33:44:55:66", "de:ad:be:ef:00:02"
    s.update(pkt({"type": "data", "to_ds": True, "from_ds": False, "bssid": ap,
                  "source": client, "dest": far_da, "rssi": -50}), W0)
    assert client in s.clients and far_da not in s.clients
    assert s.clients[client].bssid == ap


def test_group_mac_destination_is_not_a_client():
    s = WlanSink()
    ap, upstream = "aa:bb:cc:dd:ee:ff", "de:ad:be:ef:00:01"
    for group in ("33:33:00:00:00:02", "01:00:5e:7f:ff:fa", "ff:ff:ff:ff:ff:ff"):
        s.update(pkt({"type": "data", "to_ds": False, "from_ds": True, "bssid": ap,
                      "dest": group, "source": upstream, "rssi": -50}), W0)
    assert s.clients == {}


def test_transition_pmkid_only_classified_via_assoc():
    from wifit3.crack import handshake as wpa
    for client_akm, expect_crackable in ((0x02, True), (0x08, False)):
        s = WlanSink()
        b, client = "aa:bb:cc:dd:ee:ff", "12:22:33:44:55:66"
        s.update(pkt({"type": "beacon", "bssid": b, "source": b, "dest": "ff:ff:ff:ff:ff:ff",
                      "rssi": -40, "ssid": "T", "channel": 1, "encryption": "WPA2/WPA3",
                      "akm_suites": [0x02, 0x08], "raw": b"\x00" * 36}), W0)
        s.update(pkt({"type": "assoc_req", "bssid": b, "source": client, "dest": b,
                      "rssi": -45, "assoc_akm": client_akm}), W0)
        s.update(pkt({"type": "eapol", "bssid": b, "source": b, "dest": client, "rssi": -45,
                      "raw": b"\x00" * 100, "eapol_msg_num": 1, "eapol_replay_counter": b"\x00" * 8,
                      "eapol_nonce": b"\x01" * 32, "eapol_mic": b"\x00" * 16, "eapol_key_data_len": 0,
                      "eapol_payload": b"\x00" * 99, "eapol_pmkid": b"\x07" * 16}), W0)
        hs = s.access_points[b].handshakes[client]
        assert hs.pmkid == b"\x07" * 16 and hs.akm_client == client_akm
        assert wpa.pmkid_crackable(hs) is expect_crackable


def test_wpa3_and_pmf_flags_propagate_and_refresh():
    s = WlanSink()
    b = "aa:bb:cc:dd:ee:ff"
    s.update(pkt({"type": "beacon", "bssid": b, "rssi": -40, "ssid": "SAE", "channel": 1,
                  "encryption": "WPA3", "wpa3": True, "transition_mode": False,
                  "pmf_capable": True, "pmf_required": True, "raw": b"\x00" * 36}), W0)
    ap = s.access_points[b]
    assert ap.wpa3 and not ap.transition_mode and ap.pmf_capable and ap.pmf_required
    s.update(pkt({"type": "beacon", "bssid": b, "rssi": -42, "ssid": "SAE", "channel": 1,
                  "encryption": "WPA3", "wpa3": True, "transition_mode": True,
                  "pmf_capable": True, "pmf_required": False, "raw": b"\x00" * 36}), W0)
    assert ap.transition_mode and not ap.pmf_required


def test_decloak_via_assoc_req_and_method_not_overwritten():
    s = WlanSink()
    b = "12:22:33:44:55:66"
    s.update(pkt({"type": "beacon", "bssid": b, "rssi": -60, "ssid": "<hidden>"}), W0)
    s.update(pkt({"type": "assoc_req", "bssid": b, "source": "aa:aa:aa:aa:aa:aa", "dest": b,
                  "rssi": -65, "ssid": "TestSSID"}), W0)
    ap = s.access_points[b]
    assert ap.ssid == "TestSSID" and ap.decloak_method == "assoc_req"
    s.update(pkt({"type": "probe_resp", "bssid": b, "rssi": -60, "ssid": "TestSSID"}), W0)
    assert s.access_points[b].decloak_method == "assoc_req"   # first method wins


def test_siblings_first_byte_differs():
    s = WlanSink()
    _seed_beacon(s, "00:bb:cc:dd:ee:ff", 10, "TestSSID")
    _seed_beacon(s, "07:bb:cc:dd:ee:ff", 10, "<hidden>")
    assert s.access_points["00:bb:cc:dd:ee:ff"].siblings == ["07:bb:cc:dd:ee:ff"]
    assert s.access_points["07:bb:cc:dd:ee:ff"].siblings == ["00:bb:cc:dd:ee:ff"]


def test_siblings_different_channel_no_match():
    s = WlanSink()
    _seed_beacon(s, "aa:bb:cc:dd:ee:00", 1)
    _seed_beacon(s, "aa:bb:cc:dd:ee:02", 6)
    assert s.access_points["aa:bb:cc:dd:ee:00"].siblings == []
    assert s.access_points["aa:bb:cc:dd:ee:02"].siblings == []


def test_siblings_two_bytes_two_bits_matches():
    s = WlanSink()
    _seed_beacon(s, "02:bb:cc:00:ee:ff", 11, "TestSSID")
    _seed_beacon(s, "00:bb:cc:01:ee:ff", 11, "<hidden>")
    assert s.access_points["02:bb:cc:00:ee:ff"].siblings == ["00:bb:cc:01:ee:ff"]


def test_siblings_one_byte_many_bits_matches():
    s = WlanSink()
    _seed_beacon(s, "00:bb:cc:dd:ee:ff", 2, "TestSSID")
    _seed_beacon(s, "3f:bb:cc:dd:ee:ff", 2, "<hidden>")
    assert s.access_points["00:bb:cc:dd:ee:ff"].siblings == ["3f:bb:cc:dd:ee:ff"]


def test_siblings_too_divergent_no_match():
    s = WlanSink()
    _seed_beacon(s, "aa:bb:cc:dd:ee:00", 44)
    _seed_beacon(s, "aa:bb:cc:dd:e9:03", 44)      # 5 bits, 2 bytes → not siblings
    assert s.access_points["aa:bb:cc:dd:ee:00"].siblings == []


def test_siblings_self_is_never_a_sibling():
    s = WlanSink()
    _seed_beacon(s, "aa:bb:cc:dd:ee:00", 44)
    s._recompute_siblings_for("aa:bb:cc:dd:ee:00")
    assert s.access_points["aa:bb:cc:dd:ee:00"].siblings == []


def test_siblings_three_way_cluster():
    s = WlanSink()
    for b in ("aa:bb:cc:dd:ee:02", "aa:bb:cc:dd:ee:03", "aa:bb:cc:dd:ee:05"):
        _seed_beacon(s, b, 6)
    a = s.access_points["aa:bb:cc:dd:ee:02"]
    assert sorted(a.siblings) == ["aa:bb:cc:dd:ee:03", "aa:bb:cc:dd:ee:05"]


def test_siblings_channel_change_drops_stale_link():
    s = WlanSink()
    _seed_beacon(s, "aa:bb:cc:dd:ee:00", 44)
    _seed_beacon(s, "aa:bb:cc:dd:ee:02", 44)
    assert s.access_points["aa:bb:cc:dd:ee:00"].siblings
    _seed_beacon(s, "aa:bb:cc:dd:ee:02", 149)      # roams away
    assert s.access_points["aa:bb:cc:dd:ee:00"].siblings == []
    assert s.access_points["aa:bb:cc:dd:ee:02"].siblings == []
