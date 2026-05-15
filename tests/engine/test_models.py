import pytest
from wifit3.engine.models import AccessPoint, Client, Handshake

def test_access_point_model_defaults():
    ap = AccessPoint(bssid="00:11:22:33:44:55", ssid="Test_WiFi", signal=-50)
    assert ap.bssid == "00:11:22:33:44:55"
    assert ap.ssid == "Test_WiFi"
    assert ap.signal == -50
    assert ap.beacons == 0
    assert ap.wpa3 is False
    assert ap.pmf_capable is False

def test_handshake_is_complete():
    hs = Handshake(bssid="00:11:22:33:44:55", client_mac="AA:BB:CC:DD:EE:FF")
    assert not hs.is_complete
    
    hs.beacon_frame = b"fake_beacon"
    assert not hs.is_complete
    
    # Add one EAPOL frame
    hs.eapol_frames_by_replay["01020304"] = [b"frame1"]
    assert not hs.is_complete
    
    # Add a second EAPOL frame with the same replay counter
    hs.eapol_frames_by_replay["01020304"].append(b"frame2")
    assert hs.is_complete
