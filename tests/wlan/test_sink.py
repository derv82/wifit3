"""Tests for the session-wide 802.11 picture (wlan/sink.py).

WlanSink is pure: it takes parsed Packets plus the receiving card id and builds the AP/client
registry. These are the picture assertions that used to live on WlanInterface, re-driven through
``sink.update(pkt, card_id)``, plus the multicard-specific per-card signal behavior."""

import struct

from wifit3.wlan.sink import WlanSink
from wifit3.wlan.packet_stats import PACKET_CLASSES

from tests.frames import pkt

BSSID = "aa:bb:cc:dd:ee:ff"
W0, W1 = "wlan0", "wlan1"


def _beacon(overrides=None):
    base = {
        "type": "beacon", "bssid": BSSID, "source": BSSID, "dest": "ff:ff:ff:ff:ff:ff",
        "rssi": -40, "ssid": "Test_SSID", "channel": 6, "encryption": "WPA2",
    }
    base.update(overrides or {})
    return pkt(base)


# ----- registry + per-card signal --------------------------------------------

def test_beacon_creates_ap_and_smooths_signal_per_card():
    s = WlanSink()
    s.update(_beacon(), W0)
    ap = s.get_access_points()[0]
    assert ap.bssid == BSSID and ap.ssid == "Test_SSID" and ap.channel == 6
    assert ap.encryption == "WPA2" and ap.beacons == 1
    assert ap.signal == -40 and ap.signal_by_card == {W0: -40}

    s.update(_beacon({"rssi": -50}), W0)         # same card, second sample
    ap = s.get_access_points()[0]
    assert ap.beacons == 2
    assert ap.signal == -45                       # (-40 + -50) // 2, per-card
    assert ap.signal_by_card == {W0: -45}


def test_signal_is_strongest_across_cards():
    s = WlanSink()
    s.update(_beacon({"rssi": -70}), W0)
    s.update(_beacon({"rssi": -55}), W1)          # a second card hears it stronger
    ap = s.get_access_points()[0]
    assert ap.signal_by_card == {W0: -70, W1: -55}
    assert ap.signal == -55                        # max() = strongest antenna


def test_record_signal_updates_per_card_on_duplicate():
    s = WlanSink()
    s.update(_beacon({"rssi": -70}), W0)           # novel: folded into the picture by card 0
    s.record_signal(W1, BSSID, -50)                # card 1's duplicate: only its signal
    ap = s.get_access_points()[0]
    assert ap.signal_by_card == {W0: -70, W1: -50}
    assert ap.signal == -50
    assert ap.beacons == 1                          # a duplicate never re-counts the frame


def test_record_signal_unknown_bssid_is_noop():
    s = WlanSink()
    s.record_signal(W0, "de:ad:be:ef:00:00", -30)
    assert s.get_access_points() == []


def test_channel_hint_used_only_when_beacon_lacks_channel():
    s = WlanSink()
    s.update(pkt({"type": "beacon", "bssid": BSSID, "rssi": -40, "ssid": "X"}), W0, channel_hint=11)
    assert s.access_points[BSSID].channel == 11


# ----- encryption / decloak / clients ----------------------------------------

def test_encryption_keeps_strongest_evidence_not_latest():
    s = WlanSink()
    s.update(_beacon({"encryption": "WPA2-PSK-CCMP", "akms": ["PSK"]}), W0)
    s.update(_beacon({"encryption": "WEP", "akms": []}), W0)     # RSN-less flicker
    assert s.access_points[BSSID].encryption == "WPA2-PSK-CCMP"


def test_decloak_via_probe_resp():
    s = WlanSink()
    s.update(pkt({"type": "beacon", "bssid": BSSID, "rssi": -60, "ssid": "<hidden>"}), W0)
    assert s.access_points[BSSID].ssid is None
    s.update(pkt({"type": "probe_resp", "bssid": BSSID, "rssi": -60, "ssid": "Now_Visible"}), W0)
    ap = s.access_points[BSSID]
    assert ap.ssid == "Now_Visible" and ap.decloak_method == "probe_resp"


def test_assoc_req_stamps_client_akm():
    s = WlanSink()
    client = "12:22:33:44:55:66"
    s.update(pkt({"type": "assoc_req", "bssid": BSSID, "source": client, "dest": BSSID,
                  "rssi": -45, "assoc_akm": 0x02}), W0)
    assert s.clients[client].akm_selected == 0x02


def test_decloak_via_assoc_req():
    """A client's assoc-req SSID IE decloaks the hidden AP it's joining."""
    s = WlanSink()
    s.update(pkt({"type": "beacon", "bssid": BSSID, "rssi": -60, "ssid": "<hidden>"}), W0)
    assert s.access_points[BSSID].ssid is None
    s.update(pkt({"type": "assoc_req", "bssid": BSSID, "source": "12:22:33:44:55:66",
                  "dest": BSSID, "rssi": -45, "ssid": "Real_Name"}), W0)
    ap = s.access_points[BSSID]
    assert ap.ssid == "Real_Name" and ap.decloak_method == "assoc_req"


def test_decloak_via_reassoc_req():
    """Reassoc-req carries the SSID too (PMF doesn't protect it), so it decloaks as well."""
    s = WlanSink()
    s.update(pkt({"type": "beacon", "bssid": BSSID, "rssi": -60, "ssid": "<hidden>"}), W0)
    assert s.access_points[BSSID].ssid is None
    s.update(pkt({"type": "reassoc_req", "bssid": BSSID, "source": "12:22:33:44:55:66",
                  "dest": BSSID, "rssi": -45, "ssid": "Real_Name"}), W0)
    ap = s.access_points[BSSID]
    assert ap.ssid == "Real_Name" and ap.decloak_method == "reassoc_req"


def test_from_ds_client_is_receiver_not_addr3_origin():
    s = WlanSink()
    client, upstream = "12:22:33:44:55:66", "de:ad:be:ef:00:01"
    s.update(pkt({"type": "data", "to_ds": False, "from_ds": True,
                  "bssid": BSSID, "dest": client, "source": upstream, "rssi": -50}), W0)
    assert client in s.clients and upstream not in s.clients
    assert s.clients[client].bssid == BSSID


# ----- siblings --------------------------------------------------------------

def test_siblings_last_byte_differs():
    s = WlanSink()
    s.update(pkt({"type": "beacon", "bssid": "aa:bb:cc:dd:ee:00", "rssi": -60,
                  "ssid": "TestSSID", "channel": 44}), W0)
    s.update(pkt({"type": "beacon", "bssid": "aa:bb:cc:dd:ee:02", "rssi": -60,
                  "ssid": "<hidden>", "channel": 44}), W0)
    assert s.access_points["aa:bb:cc:dd:ee:00"].siblings == ["aa:bb:cc:dd:ee:02"]
    assert s.access_points["aa:bb:cc:dd:ee:02"].siblings == ["aa:bb:cc:dd:ee:00"]


# ----- forged / self MAC -----------------------------------------------------

def test_forged_mac_does_not_create_client_or_append_eapol():
    s = WlanSink()
    s.update(_beacon({"channel": 1, "raw": b"\x00" * 36}), W0)
    forged = "02:aa:bb:cc:dd:ee"
    s.register_forged_mac(forged)
    pmkid = bytes.fromhex("ad2fad48da558cdfeb19cea25e2ce5af")
    s.update(pkt({
        "type": "eapol", "bssid": BSSID, "source": BSSID, "dest": forged, "rssi": -45,
        "raw": b"\x00" * 100, "eapol_msg_num": 1, "eapol_replay_counter": b"\x00" * 8,
        "eapol_nonce": b"\x01" * 32, "eapol_mic": b"\x00" * 16, "eapol_key_data_len": 22,
        "eapol_payload": b"\x00" * 121, "eapol_pmkid": pmkid,
    }), W0)
    assert forged not in s.clients
    hs = s.access_points[BSSID].handshakes[forged]
    assert hs.pmkid == pmkid and hs.messages == []


def test_register_and_unregister_own_mac():
    s = WlanSink()
    mac = s.register_own_mac(b"\x02\x00\x00\x00\x00\x01")
    assert mac == "02:00:00:00:00:01"
    assert mac in s.own_macs and mac not in s.clients   # our own MAC is never a client
    s.unregister_own_mac(mac)
    assert mac not in s.own_macs


def test_self_and_forged_aliases_funnel_to_own_macs():
    s = WlanSink()
    s.register_forged_mac("aa:bb:cc:dd:ee:01")
    s.register_self_mac("aa:bb:cc:dd:ee:02", bssid="12:22:33:44:55:66")
    assert {"aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"} <= s.own_macs
    assert s.forged_macs == s.own_macs                  # back-compat alias


def test_wep_ivs_tallied_onto_ap():
    s = WlanSink()
    bssid = "12:22:33:44:55:66"
    s.update(pkt({"type": "beacon", "bssid": bssid, "rssi": -40, "ssid": "WepAP",
                  "channel": 6, "encryption": "WEP", "raw": b"\x00" * 36}), W0)
    for iv in (b"\x01\x02\x03", b"\x01\x02\x03", b"\x04\x05\x06"):
        s.update(pkt({"type": "wep_data", "bssid": bssid, "source": "aa:bb:cc:dd:ee:01",
                      "dest": "aa:bb:cc:dd:ee:01", "rssi": -45, "wep_iv": iv,
                      "wep_keyid": 0, "raw": b"\x00" * 40}), W0)
    ap = s.access_points[bssid]
    assert ap.wep is not None and ap.wep.unique_ivs == 2 and ap.wep.total_frames == 3
    assert s.clients["aa:bb:cc:dd:ee:01"].bssid == bssid


# ----- TX stats --------------------------------------------------------------

def _mac(x):
    return bytes(int(p, 16) for p in x.split(":"))


def test_record_tx_classifies_deauth_vs_inject():
    s = WlanSink()
    bssid, client = "00:11:22:33:44:55", "aa:bb:cc:dd:ee:ff"
    deauth = b"\xc0\x00\x00\x00" + _mac(client) + _mac(bssid) + _mac(bssid) + b"\x00\x00" \
        + struct.pack("<H", 7)
    data = b"\x08\x01\x00\x00" + _mac(bssid) + _mac(client) + _mac(client) + b"\x00\x00"
    s.record_tx(deauth)
    s.record_tx(data)
    snap = s.packet_stats.snapshot(bssid)
    assert snap["deauth"] == 1 and snap["inject"] == 1


def test_record_tx_never_raises_on_garbage():
    s = WlanSink()
    s.record_tx(b"\x00\x01")           # unparseable: best-effort, no exception
    assert s.packet_stats._counts == {} or all(
        v == dict.fromkeys(PACKET_CLASSES, 0) for v in s.packet_stats._counts.values()
    )
