import asyncio

import usb.core

from wifit3.wlan.interface import WlanInterface

from tests.frames import pkt


def test_wlan_interface_caching(mocker):
    # Mock driver
    mock_driver = mocker.MagicMock()
    iface = WlanInterface(driver_instance=mock_driver, name="wlan0", description="Test Interface")
    
    # Simulate an AP Beacon parsed by the parser
    parsed_beacon = {
        "type": "beacon",
        "bssid": "aa:bb:cc:dd:ee:ff",
        "source": "aa:bb:cc:dd:ee:ff",
        "dest": "ff:ff:ff:ff:ff:ff",
        "rssi": -40,
        "ssid": "Test_SSID",
        "channel": 6,
        "encryption": "WPA2"
    }
    
    iface._on_frame_parsed(pkt(parsed_beacon))

    aps = iface.get_access_points()
    assert len(aps) == 1
    assert aps[0].bssid == "aa:bb:cc:dd:ee:ff"
    assert aps[0].ssid == "Test_SSID"
    assert aps[0].signal == -40
    assert aps[0].channel == 6
    assert aps[0].encryption == "WPA2"
    assert aps[0].beacons == 1
    
    # Simulate a second beacon updates signal and count
    parsed_beacon["rssi"] = -50
    iface._on_frame_parsed(pkt(parsed_beacon))
    
    aps = iface.get_access_points()
    assert aps[0].beacons == 2
    assert aps[0].signal == -45  # Averaged (-40 + -50) // 2

def _beacon(enc, *, akms=None, rssi=-40):
    return pkt({
        "type": "beacon",
        "bssid": "aa:bb:cc:dd:ee:ff",
        "source": "aa:bb:cc:dd:ee:ff",
        "dest": "ff:ff:ff:ff:ff:ff",
        "rssi": rssi,
        "ssid": "Test_SSID",
        "channel": 6,
        "encryption": enc,
        "akms": akms or ([] if enc in ("OPEN", "WEP") else ["PSK"]),
    })


def test_encryption_keeps_strongest_evidence_not_latest(mocker):
    """A dropped/truncated beacon (no RSN IE -> 'WEP') must not flap a known
    WPA2 AP to WEP; and a mis-parsed first 'WEP' beacon must be upgradeable to
    WPA2 by a later beacon that actually carries the IE."""
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="t")

    # WPA2 confirmed, then an RSN-less beacon arrives (the flicker case).
    iface._on_frame_parsed(_beacon("WPA2-PSK-CCMP"))
    iface._on_frame_parsed(_beacon("WEP"))
    assert iface.get_access_points()[0].encryption == "WPA2-PSK-CCMP"

    # Order-independence: first frame mis-reads as WEP, then real WPA2 wins.
    iface2 = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan1", description="t")
    iface2._on_frame_parsed(_beacon("WEP"))
    assert iface2.get_access_points()[0].encryption == "WEP"   # provisional
    iface2._on_frame_parsed(_beacon("WPA2-PSK-CCMP"))
    ap = iface2.get_access_points()[0]
    assert ap.encryption == "WPA2-PSK-CCMP"
    assert ap.akms == ["PSK"]
    # And it stays — a later RSN-less beacon can't downgrade it.
    iface2._on_frame_parsed(_beacon("WEP"))
    assert iface2.get_access_points()[0].encryption == "WPA2-PSK-CCMP"


def test_forged_mac_does_not_create_client_or_append_eapol(mocker):
    """When an attack registers a forged STA MAC, frames addressed to it
    should: (a) not show up in iface.clients, (b) still create a Handshake
    so PMKID has a home, but (c) NOT append EAPOL frames to that handshake
    (those are just AP retries of M1 we'll never answer)."""
    mock_driver = mocker.MagicMock()
    iface = WlanInterface(driver_instance=mock_driver, name="wlan0", description="Test")

    # Beacon to seed AP
    iface._on_frame_parsed(pkt({
        "type": "beacon",
        "bssid": "aa:bb:cc:dd:ee:ff",
        "source": "aa:bb:cc:dd:ee:ff",
        "dest": "ff:ff:ff:ff:ff:ff",
        "rssi": -40,
        "ssid": "TestAP",
        "channel": 1,
        "encryption": "WPA2",
        "raw": b"\x00" * 36,
    }))

    forged = "02:aa:bb:cc:dd:ee"
    iface.register_forged_mac(forged)

    # Simulate AP -> us EAPOL M1 with a PMKID KDE already parsed out.
    pmkid = bytes.fromhex("ad2fad48da558cdfeb19cea25e2ce5af")
    iface._on_frame_parsed(pkt({
        "type": "eapol",
        "bssid": "aa:bb:cc:dd:ee:ff",
        "source": "aa:bb:cc:dd:ee:ff",
        "dest": forged,
        "rssi": -45,
        "raw": b"\x00" * 100,
        "eapol_msg_num": 1,
        "eapol_replay_counter": b"\x00" * 8,
        "eapol_nonce": b"\x01" * 32,
        "eapol_mic": b"\x00" * 16,
        "eapol_key_data_len": 22,
        "eapol_payload": b"\x00" * 121,
        "eapol_pmkid": pmkid,
    }))

    # (a) forged MAC must NOT appear in clients
    assert forged not in iface.clients

    # (b) Handshake exists for forged STA — PMKID lives there
    ap = iface.access_points["aa:bb:cc:dd:ee:ff"]
    hs = ap.handshakes[forged]
    assert hs.pmkid == pmkid

    # (c) EAPOL frames list stays empty — no "Partial x1" in the UI
    assert hs.messages == []


def _seed_ap(iface, bssid, encryption):
    iface._on_frame_parsed(pkt({
        "type": "beacon",
        "bssid": bssid,
        "source": bssid,
        "dest": "ff:ff:ff:ff:ff:ff",
        "rssi": -40,
        "ssid": "WepAP",
        "channel": 6,
        "encryption": encryption,
        "raw": b"\x00" * 36,
    }))


def _wep_data(bssid, client, iv):
    # AP→client: source=client, dest=client, bssid carried separately.
    return pkt({
        "type": "wep_data",
        "bssid": bssid,
        "source": client,
        "dest": client,
        "rssi": -45,
        "wep_iv": iv,
        "wep_keyid": 0,
        "raw": b"\x00" * 40,
    })


def test_wep_ivs_tallied_onto_ap(mocker):
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="T")
    bssid = "12:22:33:44:55:66"
    _seed_ap(iface, bssid, "WEP")

    iface._on_frame_parsed(_wep_data(bssid, "aa:bb:cc:dd:ee:01", b"\x01\x02\x03"))
    iface._on_frame_parsed(_wep_data(bssid, "aa:bb:cc:dd:ee:01", b"\x01\x02\x03"))  # dup
    iface._on_frame_parsed(_wep_data(bssid, "aa:bb:cc:dd:ee:01", b"\x04\x05\x06"))

    ap = iface.access_points[bssid]
    assert ap.wep is not None
    assert ap.wep.unique_ivs == 2
    assert ap.wep.total_frames == 3
    # The transmitting client should also be registered + associated.
    assert "aa:bb:cc:dd:ee:01" in iface.clients
    assert iface.clients["aa:bb:cc:dd:ee:01"].bssid == bssid


def test_register_self_mac_creates_you_client(mocker):
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="T")
    mac = iface.register_self_mac(b"\x02\x00\x00\x00\x00\x01", bssid="12:22:33:44:55:66")
    assert mac == "02:00:00:00:00:01"
    c = iface.clients[mac]
    assert c.is_self is True
    assert c.bssid == "12:22:33:44:55:66"
    # Forged self MAC is a distinct concept from the hidden PMKID forged_macs.
    assert mac in iface.self_macs
    assert mac not in iface.forged_macs


def test_unregister_self_mac_drops_you_client(mocker):
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="T")
    mac = iface.register_self_mac(b"\x02\x00\x00\x00\x00\x01")
    iface.unregister_self_mac(mac)
    assert mac not in iface.clients
    assert mac not in iface.self_macs


def test_wep_ivs_ignored_for_non_wep_ap(mocker):
    """Guard: an ExtIV-clear frame on a WPA2 AP must not inflate a WEP count."""
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="T")
    bssid = "12:22:33:44:55:66"
    _seed_ap(iface, bssid, "WPA2")

    iface._on_frame_parsed(_wep_data(bssid, "aa:bb:cc:dd:ee:01", b"\x01\x02\x03"))

    assert iface.access_points[bssid].wep is None


def _wep_broadcast(bssid, source, *, to_ds, length=68):
    return pkt({
        "type": "wep_data",
        "bssid": bssid,
        "source": source,
        "dest": "ff:ff:ff:ff:ff:ff",
        "to_ds": to_ds,
        "rssi": -45,
        "wep_iv": b"\x09\x08\x07",
        "raw": b"\x00" * length,
    })


def test_broadcast_wep_stored_as_arp_candidate_either_direction(mocker):
    """Both ToDS and FromDS broadcast WEP frames are kept — the replay engine
    re-addresses them, so a FromDS relay is as usable as a ToDS request."""
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="T")
    bssid = "12:22:33:44:55:66"
    _seed_ap(iface, bssid, "WEP")
    iface._on_frame_parsed(_wep_broadcast(bssid, "aa:bb:cc:dd:ee:01", to_ds=True))
    iface._on_frame_parsed(_wep_broadcast(bssid, "aa:bb:cc:dd:ee:02", to_ds=False))
    assert iface.wep_store.arp_candidate_count(bssid) == 2
    assert iface.wep_store.broadcast_seen_count(bssid) == 2


def test_real_client_still_creates_handshake_with_eapol_frames(mocker):
    """Counterpart to the forged-MAC test: a real client (not in
    iface.forged_macs) gets normal client + handshake registration."""
    mock_driver = mocker.MagicMock()
    iface = WlanInterface(driver_instance=mock_driver, name="wlan0", description="Test")

    iface._on_frame_parsed(pkt({
        "type": "beacon",
        "bssid": "aa:bb:cc:dd:ee:ff",
        "source": "aa:bb:cc:dd:ee:ff",
        "dest": "ff:ff:ff:ff:ff:ff",
        "rssi": -40,
        "ssid": "TestAP",
        "channel": 1,
        "encryption": "WPA2",
        "raw": b"\x00" * 36,
    }))

    real_client = "12:22:33:44:55:66"
    iface._on_frame_parsed(pkt({
        "type": "eapol",
        "bssid": "aa:bb:cc:dd:ee:ff",
        "source": "aa:bb:cc:dd:ee:ff",
        "dest": real_client,
        "rssi": -45,
        "raw": b"\x00" * 100,
        "eapol_msg_num": 1,
        "eapol_replay_counter": b"\x00" * 8,
        "eapol_nonce": b"\x01" * 32,
        "eapol_mic": b"\x00" * 16,
        "eapol_key_data_len": 0,
        "eapol_payload": b"\x00" * 99,
    }))

    assert real_client in iface.clients
    ap = iface.access_points["aa:bb:cc:dd:ee:ff"]
    assert real_client in ap.handshakes
    assert len(ap.handshakes[real_client].messages) == 1


def test_assoc_req_stamps_client_akm(mocker):
    """A (Re)Assoc Request's RSN-IE AKM is recorded on the Client, so a later
    PMKID-only capture on a transition AP can be classified without an M2."""
    mock_driver = mocker.MagicMock()
    iface = WlanInterface(driver_instance=mock_driver, name="wlan0", description="Test")
    client = "12:22:33:44:55:66"
    iface._on_frame_parsed(pkt({
        "type": "assoc_req",
        "bssid": "aa:bb:cc:dd:ee:ff",
        "source": client,
        "dest": "aa:bb:cc:dd:ee:ff",
        "rssi": -45,
        "assoc_akm": 0x02,
    }))
    assert iface.clients[client].akm_selected == 0x02


def test_from_ds_client_is_receiver_not_addr3_origin(mocker):
    """AP->client (FromDS) frames carry the wired-side origin in addr3 (parsed as 'source').
    The client is the receiver (dest); attributing 'source' minted phantom clients from the
    gateway/router MAC on bridged networks — the Focus 'hundreds of clients' bug."""
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="t")
    ap, client = "aa:bb:cc:dd:ee:ff", "12:22:33:44:55:66"
    upstream = "de:ad:be:ef:00:01"   # addr3: DS-side origin, NOT a client of the AP
    iface._on_frame_parsed(pkt({
        "type": "data", "to_ds": False, "from_ds": True,
        "bssid": ap, "dest": client, "source": upstream, "rssi": -50,
    }))
    assert client in iface.clients
    assert upstream not in iface.clients
    assert iface.clients[client].bssid == ap


def test_to_ds_client_is_sender_not_addr3_da(mocker):
    """Client->AP (ToDS) frames put the client in addr2 ('source') and the DA in addr3
    ('dest'), which need not be the AP. The client is the sender."""
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="t")
    ap, client = "aa:bb:cc:dd:ee:ff", "12:22:33:44:55:66"
    far_da = "de:ad:be:ef:00:02"     # addr3: a downstream DA, not a client
    iface._on_frame_parsed(pkt({
        "type": "data", "to_ds": True, "from_ds": False,
        "bssid": ap, "source": client, "dest": far_da, "rssi": -50,
    }))
    assert client in iface.clients
    assert far_da not in iface.clients
    assert iface.clients[client].bssid == ap


def test_group_mac_destination_is_not_a_client(mocker):
    """Multicast/broadcast MACs (IPv6 33:33, IPv4 01:00:5e, broadcast ff:…) are frame
    destinations, not stations — a downstream (FromDS) multicast frame would otherwise
    register its group MAC (addr1) as a phantom client."""
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="t")
    ap, upstream = "aa:bb:cc:dd:ee:ff", "de:ad:be:ef:00:01"
    for group in ("33:33:00:00:00:02", "01:00:5e:7f:ff:fa", "ff:ff:ff:ff:ff:ff"):
        iface._on_frame_parsed(pkt({
            "type": "data", "to_ds": False, "from_ds": True,
            "bssid": ap, "dest": group, "source": upstream, "rssi": -50,
        }))
    assert iface.clients == {}


def test_transition_pmkid_only_classified_via_assoc(mocker):
    """Phase 2 payoff: on a WPA2/WPA3 transition AP a PMKID-only capture (no M2)
    is classified from the client's Assoc-Req AKM — PSK -> crackable, SAE -> not."""
    from wifit3.engine.wpa import handshake as wpa

    for client_akm, expect_crackable in ((0x02, True), (0x08, False)):
        mock_driver = mocker.MagicMock()
        iface = WlanInterface(driver_instance=mock_driver, name="wlan0", description="Test")
        bssid, client = "aa:bb:cc:dd:ee:ff", "12:22:33:44:55:66"
        iface._on_frame_parsed(pkt({          # transition beacon: offers PSK + SAE
            "type": "beacon", "bssid": bssid, "source": bssid,
            "dest": "ff:ff:ff:ff:ff:ff", "rssi": -40, "ssid": "T", "channel": 1,
            "encryption": "WPA2/WPA3", "akm_suites": [0x02, 0x08], "raw": b"\x00" * 36,
        }))
        iface._on_frame_parsed(pkt({          # client associates, declaring its AKM
            "type": "assoc_req", "bssid": bssid, "source": client,
            "dest": bssid, "rssi": -45, "assoc_akm": client_akm,
        }))
        iface._on_frame_parsed(pkt({          # M1 carrying a PMKID KDE, no M2 follows
            "type": "eapol", "bssid": bssid, "source": bssid, "dest": client,
            "rssi": -45, "raw": b"\x00" * 100, "eapol_msg_num": 1,
            "eapol_replay_counter": b"\x00" * 8, "eapol_nonce": b"\x01" * 32,
            "eapol_mic": b"\x00" * 16, "eapol_key_data_len": 0,
            "eapol_payload": b"\x00" * 99, "eapol_pmkid": b"\x07" * 16,
        }))
        hs = iface.access_points[bssid].handshakes[client]
        assert hs.pmkid == b"\x07" * 16
        assert hs.akm_client == client_akm
        assert wpa.pmkid_crackable(hs) is expect_crackable


def test_wpa3_and_pmf_flags_propagate_to_access_point(mocker):
    """Parser detects wpa3 / transition_mode / pmf_capable / pmf_required from
    the RSN IE; WlanInterface must copy them to the AccessPoint model so the
    Focus security panel can render them."""
    mock_driver = mocker.MagicMock()
    iface = WlanInterface(driver_instance=mock_driver, name="wlan0", description="Test")

    # New AP: pure WPA3-SAE, PMF required
    iface._on_frame_parsed(pkt({
        "type": "beacon",
        "bssid": "aa:bb:cc:dd:ee:ff",
        "source": "aa:bb:cc:dd:ee:ff",
        "dest": "ff:ff:ff:ff:ff:ff",
        "rssi": -40,
        "ssid": "SAE-AP",
        "channel": 1,
        "encryption": "WPA3",
        "wpa3": True,
        "transition_mode": False,
        "pmf_capable": True,
        "pmf_required": True,
        "raw": b"\x00" * 36,
    }))
    ap = iface.access_points["aa:bb:cc:dd:ee:ff"]
    assert ap.wpa3 is True
    assert ap.transition_mode is False
    assert ap.pmf_capable is True
    assert ap.pmf_required is True
    assert ap.encryption == "WPA3"

    # Second beacon: AP switched to transition mode (rare in practice but
    # exercises the update path). Flags must refresh.
    iface._on_frame_parsed(pkt({
        "type": "beacon",
        "bssid": "aa:bb:cc:dd:ee:ff",
        "source": "aa:bb:cc:dd:ee:ff",
        "dest": "ff:ff:ff:ff:ff:ff",
        "rssi": -42,
        "ssid": "SAE-AP",
        "channel": 1,
        "encryption": "WPA3",
        "wpa3": True,
        "transition_mode": True,
        "pmf_capable": True,
        "pmf_required": False,
        "raw": b"\x00" * 36,
    }))
    assert ap.transition_mode is True
    assert ap.pmf_required is False


def test_wlan_interface_decloaking(mocker):
    mock_driver = mocker.MagicMock()
    iface = WlanInterface(driver_instance=mock_driver, name="wlan0", description="Test Interface")

    # Hidden network beacon
    parsed_beacon = {
        "type": "beacon",
        "bssid": "12:22:33:44:55:66",
        "rssi": -60,
        "ssid": "<hidden>"
    }
    iface._on_frame_parsed(pkt(parsed_beacon))

    ap = iface.access_points["12:22:33:44:55:66"]
    assert ap.ssid is None
    assert ap.decloak_method is None

    # Decloak via probe response
    parsed_probe = {
        "type": "probe_resp",
        "bssid": "12:22:33:44:55:66",
        "rssi": -60,
        "ssid": "Hidden_No_More"
    }
    iface._on_frame_parsed(pkt(parsed_probe))

    assert ap.ssid == "Hidden_No_More"
    assert ap.decloak_method == "probe_resp"


def test_wlan_interface_decloaking_via_assoc_req(mocker):
    mock_driver = mocker.MagicMock()
    iface = WlanInterface(driver_instance=mock_driver, name="wlan0", description="Test")

    iface._on_frame_parsed(pkt({
        "type": "beacon",
        "bssid": "12:22:33:44:55:66",
        "rssi": -60,
        "ssid": "<hidden>",
    }))
    ap = iface.access_points["12:22:33:44:55:66"]
    assert ap.ssid is None

    iface._on_frame_parsed(pkt({
        "type": "assoc_req",
        "bssid": "12:22:33:44:55:66",
        "source": "aa:aa:aa:aa:aa:aa",
        "dest": "12:22:33:44:55:66",
        "rssi": -65,
        "ssid": "TestSSID",
    }))

    assert ap.ssid == "TestSSID"
    assert ap.decloak_method == "assoc_req"


def test_wlan_interface_decloak_method_not_overwritten(mocker):
    """First decloak method wins; later probe-resps shouldn't reclassify it."""
    mock_driver = mocker.MagicMock()
    iface = WlanInterface(driver_instance=mock_driver, name="wlan0", description="Test")

    iface._on_frame_parsed(pkt({
        "type": "beacon",
        "bssid": "12:22:33:44:55:66",
        "rssi": -60,
        "ssid": "<hidden>",
    }))
    iface._on_frame_parsed(pkt({
        "type": "assoc_req",
        "bssid": "12:22:33:44:55:66",
        "source": "aa:aa:aa:aa:aa:aa",
        "dest": "12:22:33:44:55:66",
        "rssi": -65,
        "ssid": "TestSSID",
    }))
    iface._on_frame_parsed(pkt({
        "type": "probe_resp",
        "bssid": "12:22:33:44:55:66",
        "rssi": -60,
        "ssid": "TestSSID",
    }))

    ap = iface.access_points["12:22:33:44:55:66"]
    assert ap.decloak_method == "assoc_req"


# ---------------------------------------------------------------------------
# Sibling detection (virtual-BSSID clustering)
# ---------------------------------------------------------------------------

def _seed_beacon(iface, bssid: str, channel: int, ssid="Foo"):
    iface._on_frame_parsed(pkt({
        "type": "beacon",
        "bssid": bssid,
        "rssi": -60,
        "ssid": ssid,
        "channel": channel,
    }))


def test_siblings_last_byte_differs(mocker):
    """Last-byte-increment vendor scheme (1 bit diff in byte 5)."""
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="x")
    _seed_beacon(iface, "aa:bb:cc:dd:ee:00", channel=44, ssid="TestSSID")
    _seed_beacon(iface, "aa:bb:cc:dd:ee:02", channel=44, ssid="<hidden>")

    a = iface.access_points["aa:bb:cc:dd:ee:00"]
    b = iface.access_points["aa:bb:cc:dd:ee:02"]
    assert a.siblings == ["aa:bb:cc:dd:ee:02"]
    assert b.siblings == ["aa:bb:cc:dd:ee:00"]


def test_siblings_first_byte_differs(mocker):
    """Locally-administered first byte vendor scheme (3-bit diff in byte 0)."""
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="x")
    _seed_beacon(iface, "00:bb:cc:dd:ee:ff", channel=10, ssid="TestSSID")
    _seed_beacon(iface, "07:bb:cc:dd:ee:ff", channel=10, ssid="<hidden>")

    a = iface.access_points["00:bb:cc:dd:ee:ff"]
    b = iface.access_points["07:bb:cc:dd:ee:ff"]
    assert a.siblings == ["07:bb:cc:dd:ee:ff"]
    assert b.siblings == ["00:bb:cc:dd:ee:ff"]


def test_siblings_different_channel_no_match(mocker):
    """Same near-identical BSSIDs but different channels — NOT siblings."""
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="x")
    _seed_beacon(iface, "aa:bb:cc:dd:ee:00", channel=1, ssid="Foo")
    _seed_beacon(iface, "aa:bb:cc:dd:ee:02", channel=6, ssid="Bar")

    assert iface.access_points["aa:bb:cc:dd:ee:00"].siblings == []
    assert iface.access_points["aa:bb:cc:dd:ee:02"].siblings == []


def test_siblings_two_bytes_two_bits_matches(mocker):
    """Multi-byte single-bit vendor scheme: 2 bits across 2 bytes.
        02:bb:cc:00:ee:ff   visible
        00:bb:cc:01:ee:ff   <hidden>
    Two BYTES differ (positions 0 and 3) but only 2 BITS total: the U/L
    bit on byte 0 (02↔00) and bit 0 on byte 3 (00↔01). The byte-count
    rule we shipped first missed this; bit-count catches it."""
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="x")
    _seed_beacon(iface, "02:bb:cc:00:ee:ff", channel=11, ssid="TestSSID")
    _seed_beacon(iface, "00:bb:cc:01:ee:ff", channel=11, ssid="<hidden>")

    a = iface.access_points["02:bb:cc:00:ee:ff"]
    b = iface.access_points["00:bb:cc:01:ee:ff"]
    assert a.siblings == ["00:bb:cc:01:ee:ff"]
    assert b.siblings == ["02:bb:cc:00:ee:ff"]


def test_siblings_one_byte_many_bits_matches(mocker):
    """Single-byte multi-bit vendor scheme: 6 bits all in byte 0.
        00:bb:cc:dd:ee:ff   visible
        3f:bb:cc:dd:ee:ff   <hidden>
    ONE byte differs (byte 0) but it differs by 6 bits (00=00000000,
    3f=00111111). Pure Hamming-distance threshold of 4 misses this; the
    byte-diff=1 OR-branch catches it."""
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="x")
    _seed_beacon(iface, "00:bb:cc:dd:ee:ff", channel=2, ssid="TestSSID")
    _seed_beacon(iface, "3f:bb:cc:dd:ee:ff", channel=2, ssid="<hidden>")

    a = iface.access_points["00:bb:cc:dd:ee:ff"]
    b = iface.access_points["3f:bb:cc:dd:ee:ff"]
    assert a.siblings == ["3f:bb:cc:dd:ee:ff"]
    assert b.siblings == ["00:bb:cc:dd:ee:ff"]


def test_siblings_too_many_bits_AND_too_many_bytes_no_match(mocker):
    """Both branches must fail to count as 'not a sibling'. Here byte-diff
    is 2 (above 1) AND bit-diff is 5 (above 4) → no match."""
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="x")
    _seed_beacon(iface, "aa:bb:cc:dd:ee:00", channel=44, ssid="Foo")
    # ee→e9 = 3 bits, 00→03 = 2 bits → 5 bits total, 2 bytes total.
    _seed_beacon(iface, "aa:bb:cc:dd:e9:03", channel=44, ssid="Bar")

    assert iface.access_points["aa:bb:cc:dd:ee:00"].siblings == []
    assert iface.access_points["aa:bb:cc:dd:e9:03"].siblings == []


def test_siblings_zero_byte_diff_no_match(mocker):
    """An AP is never its own sibling; same bssid → ignored (we already
    early-return on identity)."""
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="x")
    _seed_beacon(iface, "aa:bb:cc:dd:ee:00", channel=44, ssid="Foo")
    iface._recompute_siblings_for("aa:bb:cc:dd:ee:00")
    assert iface.access_points["aa:bb:cc:dd:ee:00"].siblings == []


def test_siblings_three_way_cluster(mocker):
    """Three virtual BSSIDs on the same channel — all link bidirectionally
    to the other two."""
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="x")
    _seed_beacon(iface, "aa:bb:cc:dd:ee:02", channel=6, ssid="Main")
    _seed_beacon(iface, "aa:bb:cc:dd:ee:03", channel=6, ssid="<hidden>")
    _seed_beacon(iface, "aa:bb:cc:dd:ee:05", channel=6, ssid="Guest")

    a = iface.access_points["aa:bb:cc:dd:ee:02"]
    b = iface.access_points["aa:bb:cc:dd:ee:03"]
    c = iface.access_points["aa:bb:cc:dd:ee:05"]
    assert sorted(a.siblings) == ["aa:bb:cc:dd:ee:03", "aa:bb:cc:dd:ee:05"]
    assert sorted(b.siblings) == ["aa:bb:cc:dd:ee:02", "aa:bb:cc:dd:ee:05"]
    assert sorted(c.siblings) == ["aa:bb:cc:dd:ee:02", "aa:bb:cc:dd:ee:03"]


def test_siblings_channel_change_drops_stale_link(mocker):
    """If a sibling roams to a different channel, the link drops on both
    sides on the next beacon — they're no longer co-radio."""
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="x")
    _seed_beacon(iface, "aa:bb:cc:dd:ee:00", channel=44, ssid="Foo")
    _seed_beacon(iface, "aa:bb:cc:dd:ee:02", channel=44, ssid="Bar")
    a = iface.access_points["aa:bb:cc:dd:ee:00"]
    b = iface.access_points["aa:bb:cc:dd:ee:02"]
    assert a.siblings and b.siblings

    # b hops to a different channel
    _seed_beacon(iface, "aa:bb:cc:dd:ee:02", channel=149, ssid="Bar")

    assert iface.access_points["aa:bb:cc:dd:ee:00"].siblings == []
    assert iface.access_points["aa:bb:cc:dd:ee:02"].siblings == []


def test_on_device_lost_latches_and_fans_once(mocker):
    """The disconnect sink fires subscribers exactly once (latched) and trips the hop flag."""
    iface = WlanInterface(driver_instance=mocker.MagicMock(), name="wlan0", description="t")
    iface._is_hopping = True
    seen = []
    iface.register_disconnect_callback(seen.append)

    first = usb.core.USBError("gone", errno=19)
    iface._on_device_lost(first)
    iface._on_device_lost(usb.core.USBError("again", errno=19))  # latched → ignored

    assert seen == [first]
    assert iface._device_lost is True
    assert iface._is_hopping is False


async def test_hopper_surfaces_device_gone_and_stops(mocker):
    """An unplug mid-hop: the hopper's tune raises device-gone, the guard routes it to the
    disconnect sink and stops hopping instead of killing the hop task with an unhandled raise."""
    driver = mocker.MagicMock()

    async def boom(channel, scan=False):
        raise usb.core.USBError("no dev", errno=19)   # LIBUSB_ERROR_NO_DEVICE

    driver.set_channel = boom
    iface = WlanInterface(driver_instance=driver, name="wlan0", description="t")
    seen = []
    iface.register_disconnect_callback(seen.append)

    await iface.start_hopping(channels=[1], interval=0.01)
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.01)

    assert len(seen) == 1 and isinstance(seen[0], usb.core.USBError)
    assert iface._is_hopping is False
    await iface.stop_hopping()


async def test_deauth_sets_unicast_ack_nav(mocker):
    """A client-targeted deauth burst carries the unicast-ACK NAV (0x013A) in the duration
    of both spoofed frames — the destination (addr1) ACKs, so we reserve SIFS + a 1 Mbps
    ACK. Built in the shared interface path, so this holds for every driver."""
    driver = mocker.MagicMock()
    driver.inject_frame = mocker.AsyncMock(return_value=True)
    iface = WlanInterface(driver_instance=driver, name="wlan0", description="t")

    await iface.deauth("aa:bb:cc:dd:ee:ff", "00:11:22:33:44:55", burst_count=1)

    frames = [c.args[0] for c in driver.inject_frame.call_args_list]
    client_deauth, ap_deauth = frames[0], frames[1]
    # client_deauth addr1 = client (unicast) → NAV 0x013A (little-endian)
    assert client_deauth[4:10] == bytes.fromhex("001122334455")
    assert client_deauth[2:4] == b"\x3a\x01"
    # ap_deauth addr1 = AP (unicast) → NAV 0x013A
    assert ap_deauth[4:10] == bytes.fromhex("aabbccddeeff")
    assert ap_deauth[2:4] == b"\x3a\x01"


async def test_broadcast_deauth_zeroes_nav_for_group_target(mocker):
    """'Deauth all' addresses the client frame to ff:ff:ff:ff:ff:ff — a group address that
    is never ACKed, so its NAV is 0; the AP-directed frame stays unicast → 0x013A."""
    driver = mocker.MagicMock()
    driver.inject_frame = mocker.AsyncMock(return_value=True)
    iface = WlanInterface(driver_instance=driver, name="wlan0", description="t")

    await iface.deauth("aa:bb:cc:dd:ee:ff", "ff:ff:ff:ff:ff:ff", burst_count=1)

    frames = [c.args[0] for c in driver.inject_frame.call_args_list]
    client_deauth, ap_deauth = frames[0], frames[1]
    # client_deauth addr1 = broadcast → NAV 0
    assert client_deauth[4:10] == b"\xff\xff\xff\xff\xff\xff"
    assert client_deauth[2:4] == b"\x00\x00"
    # ap_deauth addr1 = AP (unicast) → NAV 0x013A
    assert ap_deauth[2:4] == b"\x3a\x01"
