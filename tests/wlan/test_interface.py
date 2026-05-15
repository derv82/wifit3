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
