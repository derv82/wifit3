"""Tests for the session-wide 802.11 picture (wlan/sink.py).

WlanSink is pure: it takes parsed Packets plus the receiving card id and builds the AP/client
registry. These are the picture assertions that used to live on WlanInterface, re-driven through
``sink.update(pkt, card_id)``, plus the multicard-specific per-card signal behavior."""

import struct

import pytest

from wifit3.dot11.mac import str_to_mac
from wifit3.dot11.wsc import messages as WSC
from wifit3.models import IdKey, IdSource
from wifit3.wlan.sink import WlanSink
from wifit3.wlan.packet_stats import PACKET_CLASSES
from wifit3.dot11.parser import WlanFrameParser

from tests.frames import pkt

BSSID = "aa:bb:cc:dd:ee:ff"
W0, W1 = "wlan0", "wlan1"
_LLC_SNAP_EAPOL = b"\xaa\xaa\x03\x00\x00\x00\x88\x8e"


def _beacon(overrides=None):
    base = {
        "type": "beacon", "bssid": BSSID, "source": BSSID, "dest": "ff:ff:ff:ff:ff:ff",
        "rssi": -40, "ssid": "Test_SSID", "channel": 6, "encryption": "WPA2",
    }
    base.update(overrides or {})
    return pkt(base)


def _wps_m1_frame(bssid: bytes, client: bytes) -> bytes:
    attrs = (
        WSC.tlv_u8(WSC.ATTR_VERSION, 0x10)
        + WSC.tlv_u8(WSC.ATTR_MSG_TYPE, WSC.WPS_M1)
        + WSC.tlv(WSC.ATTR_MANUFACTURER, b"TP-Link")
        + WSC.tlv(WSC.ATTR_MODEL_NAME, b"Archer AX10")
        + WSC.tlv(WSC.ATTR_MODEL_NUMBER, b"AX10")
        + WSC.tlv(WSC.ATTR_DEV_NAME, b"Office AP\x00")
    )
    expanded = (
        bytes([WSC.EAP_TYPE_EXPANDED]) + WSC.WFA_VENDOR_ID
        + WSC.WFA_VENDOR_TYPE_SIMPLECONFIG + bytes([WSC.WSC_MSG, 0x00]) + attrs
    )
    eap_len = 4 + len(expanded)
    eap = struct.pack(">BBH", WSC.EAP_REQUEST, 1, eap_len) + expanded
    eapol = struct.pack(">BBH", WSC.DOT1X_VERSION, WSC.DOT1X_TYPE_EAP_PACKET, len(eap)) + eap
    return b"\x08\x02\x00\x00" + client + bssid + bssid + b"\x00\x00" + _LLC_SNAP_EAPOL + eapol


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
    assert ap.signal == -45                       # (-40 + -50) / 2, per-card
    assert ap.signal_by_card == {W0: -45}


def test_signal_sliding_window_absorbs_spikes_and_evicts_old_samples():
    s = WlanSink()
    for _ in range(8):
        s.update(_beacon({"rssi": -70}), W0)
    ap = s.get_access_points()[0]
    assert ap.signal == -70
    assert len(ap.signal_history[W0]) == 8

    # A 16 dBm noisy drop is dampened to only 2 dBm across the 8-sample window
    s.update(_beacon({"rssi": -86}), W0)
    assert ap.signal == -72
    assert len(ap.signal_history[W0]) == 8


def test_signal_sliding_window_ignores_dummy_minus_100():
    s = WlanSink()
    s.update(_beacon({"rssi": -65}), W0)
    # A driver emitting -100 for a frame with missing RSSI report must not drag down signal
    s.update(_beacon({"rssi": -100}), W0)
    ap = s.get_access_points()[0]
    assert ap.signal == -65
    assert len(ap.signal_history[W0]) == 1


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


def test_wps_identity_fields_persist_on_ap():
    s = WlanSink()
    s.update(_beacon({
        "wps": True,
        "wsc_manufacturer": "MikroTik",
        "wsc_model_name": "RouterBOARD",
        "wsc_device_name": "Office AP",
    }), W0)
    ap = s.access_points[BSSID]
    assert ap.identity.manufacturer == "MikroTik"
    assert ap.identity.model_name == "RouterBOARD"
    assert ap.identity.device_name == "Office AP"
    assert ap.identity.summary == "MikroTik RouterBOARD"


def test_wps_m1_identity_fields_are_applied_by_sink():
    s = WlanSink()
    s.update(_beacon(), W0)
    frame = _wps_m1_frame(str_to_mac(BSSID), str_to_mac("02:00:00:00:00:01"))
    s.update(pkt({
        "type": "eapol", "to_ds": False, "from_ds": True, "bssid": BSSID,
        "source": BSSID, "dest": "02:00:00:00:00:01", "rssi": -45, "raw": frame,
    }), W0)
    ap = s.access_points[BSSID]
    assert ap.wps is True
    assert ap.identity.get_source_value(IdKey.MANUFACTURER, IdSource.WSC_M1) == "TP-Link"
    assert ap.identity.get_source_value(IdKey.MODEL_NAME, IdSource.WSC_M1) == "Archer AX10"
    assert ap.identity.get_source_value(IdKey.MODEL_NUMBER, IdSource.WSC_M1) == "AX10"
    assert ap.identity.get_source_value(IdKey.DEVICE_NAME, IdSource.WSC_M1) == "Office AP"
    assert ap.identity.manufacturer == "TP-Link"
    assert ap.identity.model_name == "Archer AX10"
    assert ap.identity.model_number == "AX10"
    assert ap.identity.device_name == "Office AP"
    assert ap.identity.summary == "TP-Link Archer AX10"


def _eap_identity_frame(bssid: bytes, client: bytes, username: bytes) -> bytes:
    """Client->AP EAP-Response/Identity (802.1X Type 0), with a cleartext username."""
    eap = struct.pack(">BBH", 2, 1, 4 + 1 + len(username)) + bytes([1]) + username
    eapol = struct.pack(">BBH", 1, 0, len(eap)) + eap
    return b"\x08\x01\x00\x00" + bssid + client + bssid + b"\x00\x00" + _LLC_SNAP_EAPOL + eapol


def test_eap_method_recorded_on_ap_without_identity():
    s = WlanSink()
    s.update(_beacon(), W0)                                   # the AP must exist first
    frame = _eap_identity_frame(str_to_mac(BSSID), str_to_mac("de:ad:be:ef:00:01"),
                                b"alice@corp.example")
    s.update(WlanFrameParser.parse_80211_frame(frame, -40), W0)

    ap = s.access_points[BSSID]
    assert ap.observed_eap_methods == {1}                     # EAP-Identity (Type 1)
    # Privacy: no handshake is minted from EAP, and the username reaches no model.
    assert ap.handshakes == {}
    assert "alice" not in repr(ap) and "alice" not in repr(s.clients["de:ad:be:ef:00:01"])


def test_wps_m1_via_parser_still_sets_wps_and_records_no_eap_method():
    s = WlanSink()
    s.update(_beacon(), W0)
    frame = _wps_m1_frame(str_to_mac(BSSID), str_to_mac("02:00:00:00:00:01"))
    s.update(WlanFrameParser.parse_80211_frame(frame, -45), W0)   # through the parser
    ap = s.access_points[BSSID]
    assert ap.wps is True                                     # WPS M1 path intact (EapolPacket)
    assert ap.observed_eap_methods == set()                   # Expanded is not an EAP method



# ----- encryption / decloak / clients ----------------------------------------

def test_encryption_keeps_strongest_evidence_not_latest():
    s = WlanSink()
    s.update(_beacon({"encryption": "WPA2-PSK-CCMP", "akms": ["PSK"]}), W0)
    s.update(_beacon({"encryption": "WEP", "akms": []}), W0)     # RSN-less flicker
    assert s.access_points[BSSID].encryption == "WPA2-PSK-CCMP"


def test_invalid_rsn_followup_keeps_valid_security_profile():
    s = WlanSink()
    valid_rsn = b"\x30\x02\x01\x00"
    s.update(_beacon({
        "encryption": "WPA3-EAP-CCMP",
        "akms": ["EAP-SHA256"],
        "akm_suites": [5],
        "rsn_valid": True,
        "rsn_ie_raw": valid_rsn,
        "pairwise_cipher": "CCMP",
        "wpa3": True,
        "pmf_capable": True,
        "pmf_required": True,
    }), W0)
    s.update(pkt({
        "type": "probe_resp", "bssid": BSSID, "source": BSSID,
        "dest": "ff:ff:ff:ff:ff:ff", "rssi": -35, "ssid": "Test_SSID",
        "channel": 6, "encryption": "WPA2", "rsn_valid": False,
        "rsn_ie_raw": b"\x30\x02\x02\x00",
    }), W0)
    ap = s.access_points[BSSID]
    assert ap.rsn_valid is True and ap.rsn_ie == valid_rsn
    assert ap.akm_suites == [5]
    assert ap.wpa3 is True and ap.pmf_capable is True and ap.pmf_required is True


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
    assert s.clients[client].bssid == BSSID


def test_roam_without_new_akm_does_not_reuse_previous_selection():
    s = WlanSink()
    client = "12:22:33:44:55:66"
    other_bssid = "aa:bb:cc:dd:ee:01"
    s.update(_beacon({"akm_suites": [2]}), W0)
    s.update(_beacon({
        "bssid": other_bssid, "source": other_bssid, "ssid": "Enterprise",
        "akm_suites": [1],
    }), W0)
    s.update(pkt({
        "type": "assoc_req", "bssid": BSSID, "source": client, "dest": BSSID,
        "rssi": -45, "assoc_akm": 2,
    }), W0)
    s.update(pkt({
        "type": "eapol", "bssid": other_bssid, "source": other_bssid, "dest": client,
        "rssi": -45, "raw": bytes(100), "eapol_msg_num": 2,
        "eapol_replay_counter": bytes(8), "eapol_nonce": bytes.fromhex("01" * 32),
        "eapol_mic": bytes.fromhex("02" * 16), "eapol_payload": bytes(99),
    }), W0)
    moved = s.clients[client]
    hs = s.access_points[other_bssid].handshakes[client]
    assert moved.bssid == other_bssid and moved.akm_selected is None
    assert hs.messages[0].akm is None


def test_disconnect_invalidates_same_bssid_akm_before_unproven_eapol():
    from wifit3.crack import handshake as wpa

    client = "12:22:33:44:55:66"
    for disconnect_type, source, dest in (
        ("deauth", BSSID, client), ("disassoc", BSSID, client),
        ("deauth", client, BSSID), ("disassoc", client, BSSID),
    ):
        s = WlanSink()
        s.update(_beacon({
            "akms": ["EAP", "PSK"],
            "akm_suites": [1, 2],
            "rsn_valid": True,
            "rsn_ie_raw": b"\x30\x02\x01\x00",
            "raw": bytes(36),
        }), W0)
        s.update(pkt({
            "type": "assoc_req", "bssid": BSSID, "source": client, "dest": BSSID,
            "rssi": -45, "assoc_akm": 2,
        }), W0)
        s.update(pkt({
            "type": disconnect_type, "bssid": BSSID, "source": source, "dest": dest,
            "rssi": -45,
        }), W0)

        assert s.clients[client].akm_selected is None

        for msg_num, source, dest, nonce, mic, raw in (
            (3, BSSID, client, b"\xaa" * 32, b"\x11" * 16, b"m3" * 40),
            (4, client, BSSID, b"\xbb" * 32, b"\x22" * 16, b"m4" * 40),
        ):
            s.update(pkt({
                "type": "eapol", "bssid": BSSID, "source": source, "dest": dest,
                "rssi": -45, "raw": raw, "eapol_msg_num": msg_num,
                "eapol_replay_counter": bytes.fromhex("00" * 7 + "05"),
                "eapol_nonce": nonce, "eapol_mic": mic, "eapol_payload": bytes(99),
            }), W0)

        hs = s.access_points[BSSID].handshakes[client]
        assert all(message.akm is None for message in hs.messages)
        assert wpa.crackable_pairs(hs) == []


def test_failed_assoc_and_reassoc_responses_invalidate_selected_akm():
    from wifit3.crack import handshake as wpa

    for request_type, response_subtype, response_type in (
        ("assoc_req", 1, "assoc_resp"),
        ("reassoc_req", 3, "reassoc_resp"),
    ):
        s = WlanSink()
        client = "12:22:33:44:55:66"
        s.update(_beacon({
            "akms": ["EAP", "PSK"],
            "akm_suites": [1, 2],
            "rsn_valid": True,
            "rsn_ie_raw": b"\x30\x02\x01\x00",
            "raw": bytes(36),
        }), W0)
        s.update(pkt({
            "type": request_type, "bssid": BSSID, "source": client, "dest": BSSID,
            "rssi": -45, "assoc_akm": 2,
        }), W0)
        raw_response = (
            bytes([response_subtype << 4, 0]) + b"\x00\x00"
            + str_to_mac(client) + str_to_mac(BSSID) * 2 + b"\x00\x00"
            + b"\x11\x00\x01\x00\x01\xc0"
        )
        response = WlanFrameParser.parse_80211_frame(raw_response, -45)
        assert response is not None and response.type == response_type
        s.update(response, W0)

        assert s.clients[client].akm_selected is None

        s.update(pkt({
            "type": "eapol", "bssid": BSSID, "source": BSSID, "dest": client,
            "rssi": -45, "raw": b"m1-after-rejection", "eapol_msg_num": 1,
            "eapol_replay_counter": bytes.fromhex("00" * 7 + "01"),
            "eapol_nonce": b"\xaa" * 32, "eapol_mic": bytes(16),
            "eapol_payload": bytes(99), "eapol_pmkid": b"\x07" * 16,
            "eapol_key_info": 2,
        }), W0)
        hs = s.access_points[BSSID].handshakes[client]
        assert hs.pmkid_akm is None
        assert not wpa.pmkid_crackable(hs)


def test_ap_group_disconnect_invalidates_all_clients_for_bssid():
    for disconnect_type in ("deauth", "disassoc"):
        s = WlanSink()
        clients = ("12:22:33:44:55:66", "12:22:33:44:55:67")
        for client in clients:
            s.update(pkt({
                "type": "assoc_req", "bssid": BSSID, "source": client, "dest": BSSID,
                "rssi": -45, "assoc_akm": 2,
            }), W0)

        s.update(pkt({
            "type": disconnect_type, "bssid": BSSID,
            "source": BSSID, "dest": "ff:ff:ff:ff:ff:ff", "rssi": -45,
        }), W0)

        for client in clients:
            assert s.clients[client].akm_selected is None


def test_group_disconnect_not_from_ap_does_not_clear_other_clients():
    s = WlanSink()
    sender = "12:22:33:44:55:66"
    other_client = "12:22:33:44:55:67"
    for client in (sender, other_client):
        s.update(pkt({
            "type": "assoc_req", "bssid": BSSID, "source": client, "dest": BSSID,
            "rssi": -45, "assoc_akm": 2,
        }), W0)

    s.update(pkt({
        "type": "deauth", "bssid": BSSID,
        "source": sender, "dest": "ff:ff:ff:ff:ff:ff", "rssi": -45,
    }), W0)

    assert s.clients[other_client].akm_selected == 2
    assert s.clients[other_client].bssid == BSSID


def _psk_eap_beacon():
    return _beacon({
        "akms": ["EAP", "PSK"],
        "akm_suites": [1, 2],
        "rsn_valid": True,
        "rsn_ie_raw": b"\x30\x02\x01\x00",
        "raw": bytes(36),
    })


def _four_way(client: str, replay: int) -> list:
    frames = []
    for msg_num in (1, 2, 3, 4):
        from_ap = msg_num in (1, 3)
        frames.append(pkt({
            "type": "eapol", "bssid": BSSID,
            "source": BSSID if from_ap else client, "dest": client if from_ap else BSSID,
            "rssi": -45, "raw": f"m{msg_num}-replay{replay}".encode() * 8,
            "eapol_msg_num": msg_num, "eapol_replay_counter": replay.to_bytes(8, "big"),
            "eapol_nonce": bytes([msg_num]) * 32, "eapol_mic": bytes([msg_num]) * 16,
            "eapol_payload": bytes(99),
        }))
    return frames


def test_m1_after_ten_seconds_of_beacons_keeps_assoc_akm():
    s = WlanSink()
    client = "12:22:33:44:55:66"
    s.update(_psk_eap_beacon(), W0)
    s.update(pkt({
        "type": "assoc_req", "bssid": BSSID, "source": client, "dest": BSSID,
        "rssi": -45, "assoc_akm": 2,
    }), W0)
    for _ in range(100):                     # 100 beacon intervals of 102.4 ms = 10.24 s
        s.update(_psk_eap_beacon(), W0)
    s.update(pkt({
        "type": "eapol", "bssid": BSSID, "source": BSSID, "dest": client,
        "rssi": -45, "raw": b"late-m1", "eapol_msg_num": 1,
        "eapol_replay_counter": bytes.fromhex("00" * 7 + "01"),
        "eapol_nonce": b"\xaa" * 32, "eapol_mic": bytes(16),
        "eapol_payload": bytes(99), "eapol_pmkid": b"\x07" * 16,
        "eapol_key_info": 2,
    }), W0)

    hs = s.access_points[BSSID].handshakes[client]
    assert s.clients[client].akm_selected == 2
    assert hs.messages[0].akm == 2 and hs.pmkid_akm == 2


def test_rekey_four_way_without_new_assoc_keeps_akm():
    s = WlanSink()
    client = "12:22:33:44:55:66"
    s.update(_psk_eap_beacon(), W0)
    s.update(pkt({
        "type": "assoc_req", "bssid": BSSID, "source": client, "dest": BSSID,
        "rssi": -45, "assoc_akm": 2,
    }), W0)
    for frame in _four_way(client, replay=1) + _four_way(client, replay=2):
        s.update(frame, W0)

    hs = s.access_points[BSSID].handshakes[client]
    assert len(hs.messages) == 8
    assert all(message.akm == 2 for message in hs.messages)


def test_reassoc_with_different_akm_gives_newest():
    s = WlanSink()
    client = "12:22:33:44:55:66"
    s.update(_beacon({"akm_suites": [2, 8], "rsn_valid": True}), W0)
    s.update(pkt({
        "type": "assoc_req", "bssid": BSSID, "source": client, "dest": BSSID,
        "rssi": -45, "assoc_akm": 2,
    }), W0)
    s.update(pkt({
        "type": "reassoc_req", "bssid": BSSID, "source": client, "dest": BSSID,
        "rssi": -45, "assoc_akm": 8,
    }), W0)
    for frame in _four_way(client, replay=1):
        s.update(frame, W0)

    hs = s.access_points[BSSID].handshakes[client]
    assert s.clients[client].akm_selected == 8
    assert all(message.akm == 8 for message in hs.messages)


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


def test_forged_alias_funnels_to_own_macs():
    s = WlanSink()
    s.register_forged_mac("aa:bb:cc:dd:ee:01")
    assert "aa:bb:cc:dd:ee:01" in s.own_macs
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


# ----- Data frames register their client --------------------------------------

_OTHER_BSSID = "3c:1f:6d:9a:22:41"
_STA_MAC = "8e:11:22:33:44:55"


def _data_frame(bssid: str = _OTHER_BSSID):
    """A ToDS data frame: the STA in addr2, the DA in addr3."""
    return pkt({"type": "data", "bssid": bssid, "source": _STA_MAC,
                "dest": _STA_MAC, "to_ds": True, "rssi": -95})


def test_a_data_frame_registers_its_client_under_the_bssid():
    s = WlanSink()
    s.update(_beacon(), W0)
    s.update(_data_frame(BSSID), W0)
    assert _STA_MAC in s.clients
    assert s.clients[_STA_MAC].bssid == BSSID


def test_a_data_frame_registers_its_client_before_the_ap_beacons():
    """A client heard before its AP's beacon is still a client."""
    s = WlanSink()
    s.update(_data_frame(), W0)
    assert _STA_MAC in s.clients


def test_a_probe_request_registers_its_client():
    s = WlanSink()
    s.update(pkt({"type": "probe_req", "bssid": _OTHER_BSSID, "source": _STA_MAC,
                  "dest": "ff:ff:ff:ff:ff:ff", "ssid": "Some_SSID", "rssi": -60}), W0)
    assert _STA_MAC in s.clients
    assert "Some_SSID" in s.clients[_STA_MAC].probed_ssids


def _broadcast_probe(ssid: str):
    return pkt({"type": "probe_req", "bssid": "ff:ff:ff:ff:ff:ff", "source": _STA_MAC,
                "dest": "ff:ff:ff:ff:ff:ff", "ssid": ssid, "rssi": -60})


def test_a_broadcast_probe_request_records_its_ssid():
    s = WlanSink()
    s.update(_broadcast_probe("Home_SSID"), W0)
    s.update(_broadcast_probe("Cafe_SSID"), W0)
    assert s.probed_ssids == {"Home_SSID", "Cafe_SSID"}


def test_probed_ssids_ignore_empty_and_hidden():
    s = WlanSink()
    s.update(_broadcast_probe(""), W0)
    s.update(_broadcast_probe("<hidden>"), W0)
    s.update(_beacon(), W0)
    assert s.probed_ssids == set()


def test_an_assoc_request_binds_to_an_ap_we_have_not_beaconed():
    s = WlanSink()
    s.update(pkt({"type": "assoc_req", "bssid": _OTHER_BSSID, "source": _STA_MAC,
                  "ssid": "Real_Net", "rssi": -55}), W0)
    assert s.clients[_STA_MAC].bssid == _OTHER_BSSID


def test_vendor_akm_reaches_handshake_crack_gate():
    from wifit3.crack import handshake as wpa

    s = WlanSink()
    client = "12:22:33:44:55:66"
    s.update(_beacon({
        "akms": ["Vendor"], "vendor_akm_offered": True,
        "rsn_valid": True, "rsn_ie_raw": b"\x30\x02\x01\x00", "raw": bytes(36),
    }), W0)
    for msg_num, source, dest, nonce, mic, raw in (
        (1, BSSID, client, bytes.fromhex("01" * 32), bytes(16), bytes(100)),
        (2, client, BSSID, bytes.fromhex("02" * 32), bytes.fromhex("03" * 16), bytes([1]) * 100),
    ):
        s.update(pkt({
            "type": "eapol", "bssid": BSSID, "source": source, "dest": dest,
            "rssi": -45, "raw": raw, "eapol_msg_num": msg_num,
            "eapol_replay_counter": bytes.fromhex("00" * 7 + "05"),
            "eapol_nonce": nonce, "eapol_mic": mic, "eapol_payload": bytes(99),
        }), W0)
    hs = s.access_points[BSSID].handshakes[client]
    assert hs.vendor_akm_offered is True and hs.rsn_ie_seen is True
    assert wpa.crackable_pairs(hs) == []
    assert wpa.withheld_capture_label(hs) == "Unsupported AKM"


@pytest.mark.parametrize("encryption, akm_name, akm, verdict, label", [
    ("WPA-EAP-TKIP", "EAP", 1, "uncrackable", "EAP/Enterprise"),
    ("WPA-PSK-TKIP", "PSK", 2, "crackable", None),
])
def test_wpa1_akm_reaches_handshake_crack_gate(encryption, akm_name, akm, verdict, label):
    from wifit3.crack import handshake as wpa

    s = WlanSink()
    client = "12:22:33:44:55:66"
    s.update(_beacon({
        "encryption": encryption, "akms": [akm_name], "akm_suites": [akm],
        "rsn_valid": False, "raw": bytes(36),
    }), W0)
    for msg_num, source, dest, nonce, mic, raw in (
        (1, BSSID, client, bytes.fromhex("01" * 32), bytes(16), bytes(100)),
        (2, client, BSSID, bytes.fromhex("02" * 32), bytes.fromhex("03" * 16), bytes([1]) * 100),
    ):
        s.update(pkt({
            "type": "eapol", "bssid": BSSID, "source": source, "dest": dest,
            "rssi": -45, "raw": raw, "eapol_msg_num": msg_num,
            "eapol_replay_counter": bytes.fromhex("00" * 7 + "05"),
            "eapol_nonce": nonce, "eapol_mic": mic, "eapol_payload": bytes(99),
        }), W0)
    hs = s.access_points[BSSID].handshakes[client]
    assert hs.akm_offered == [akm] and hs.rsn_ie_seen is False
    assert wpa.eapol_verdict(None, hs) == verdict
    assert wpa.uncrackable_label(None, hs) == label
