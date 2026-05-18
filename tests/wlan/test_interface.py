import pytest
from wifit3.wlan.interface import WlanInterface

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
    
    iface._on_frame_parsed(parsed_beacon)
    
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
    iface._on_frame_parsed(parsed_beacon)
    
    aps = iface.get_access_points()
    assert aps[0].beacons == 2
    assert aps[0].signal == -45  # Averaged (-40 + -50) // 2

def test_forged_mac_does_not_create_client_or_append_eapol(mocker):
    """When an attack registers a forged STA MAC, frames addressed to it
    should: (a) not show up in iface.clients, (b) still create a Handshake
    so PMKID has a home, but (c) NOT append EAPOL frames to that handshake
    (those are just AP retries of M1 we'll never answer)."""
    mock_driver = mocker.MagicMock()
    iface = WlanInterface(driver_instance=mock_driver, name="wlan0", description="Test")

    # Beacon to seed AP
    iface._on_frame_parsed({
        "type": "beacon",
        "bssid": "aa:bb:cc:dd:ee:ff",
        "source": "aa:bb:cc:dd:ee:ff",
        "dest": "ff:ff:ff:ff:ff:ff",
        "rssi": -40,
        "ssid": "TestAP",
        "channel": 1,
        "encryption": "WPA2",
        "raw": b"\x00" * 36,
    })

    forged = "02:aa:bb:cc:dd:ee"
    iface.register_forged_mac(forged)

    # Simulate AP -> us EAPOL M1 with a PMKID KDE already parsed out.
    pmkid = bytes.fromhex("ad2fad48da558cdfeb19cea25e2ce5af")
    iface._on_frame_parsed({
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
    })

    # (a) forged MAC must NOT appear in clients
    assert forged not in iface.clients

    # (b) Handshake exists for forged STA — PMKID lives there
    ap = iface.access_points["aa:bb:cc:dd:ee:ff"]
    hs = ap.handshakes[forged]
    assert hs.pmkid == pmkid

    # (c) EAPOL frames list stays empty — no "Partial x1" in the UI
    assert hs.eapol_frames == []


def test_real_client_still_creates_handshake_with_eapol_frames(mocker):
    """Counterpart to the forged-MAC test: a real client (not in
    iface.forged_macs) gets normal client + handshake registration."""
    mock_driver = mocker.MagicMock()
    iface = WlanInterface(driver_instance=mock_driver, name="wlan0", description="Test")

    iface._on_frame_parsed({
        "type": "beacon",
        "bssid": "aa:bb:cc:dd:ee:ff",
        "source": "aa:bb:cc:dd:ee:ff",
        "dest": "ff:ff:ff:ff:ff:ff",
        "rssi": -40,
        "ssid": "TestAP",
        "channel": 1,
        "encryption": "WPA2",
        "raw": b"\x00" * 36,
    })

    real_client = "11:22:33:44:55:66"
    iface._on_frame_parsed({
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
    })

    assert real_client in iface.clients
    ap = iface.access_points["aa:bb:cc:dd:ee:ff"]
    assert real_client in ap.handshakes
    assert len(ap.handshakes[real_client].eapol_frames) == 1


def test_wpa3_and_pmf_flags_propagate_to_access_point(mocker):
    """Parser detects wpa3 / transition_mode / pmf_capable / pmf_required from
    the RSN IE; WlanInterface must copy them to the AccessPoint model so the
    Focus security panel can render them."""
    mock_driver = mocker.MagicMock()
    iface = WlanInterface(driver_instance=mock_driver, name="wlan0", description="Test")

    # New AP: pure WPA3-SAE, PMF required
    iface._on_frame_parsed({
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
    })
    ap = iface.access_points["aa:bb:cc:dd:ee:ff"]
    assert ap.wpa3 is True
    assert ap.transition_mode is False
    assert ap.pmf_capable is True
    assert ap.pmf_required is True
    assert ap.encryption == "WPA3"

    # Second beacon: AP switched to transition mode (rare in practice but
    # exercises the update path). Flags must refresh.
    iface._on_frame_parsed({
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
    })
    assert ap.transition_mode is True
    assert ap.pmf_required is False


def test_wlan_interface_decloaking(mocker):
    mock_driver = mocker.MagicMock()
    iface = WlanInterface(driver_instance=mock_driver, name="wlan0", description="Test Interface")
    
    # Hidden network beacon
    parsed_beacon = {
        "type": "beacon",
        "bssid": "11:22:33:44:55:66",
        "rssi": -60,
        "ssid": "<hidden>"
    }
    iface._on_frame_parsed(parsed_beacon)
    
    ap = iface.access_points["11:22:33:44:55:66"]
    assert ap.ssid is None
    
    # Decloak via probe response
    parsed_probe = {
        "type": "probe_resp",
        "bssid": "11:22:33:44:55:66",
        "rssi": -60,
        "ssid": "Hidden_No_More"
    }
    iface._on_frame_parsed(parsed_probe)
    
    assert ap.ssid == "Hidden_No_More"
