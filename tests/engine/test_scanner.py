import pytest
from unittest.mock import MagicMock
from wifit3.engine.models import AccessPoint
from wifit3.engine.scanner import Scanner
from scapy.all import RadioTap, Dot11, Dot11Beacon, Dot11Elt

def test_access_point_model():
    ap = AccessPoint(bssid="00:11:22:33:44:55", ssid="Test_WiFi", signal=-50)
    assert ap.bssid == "00:11:22:33:44:55"
    assert ap.ssid == "Test_WiFi"
    assert ap.signal == -50
    assert ap.beacons == 0

def test_scanner_callback():
    found_ap = None
    def callback(ap):
        nonlocal found_ap
        found_ap = ap

    scanner = Scanner(callback=callback)
    
    # Use a MagicMock for the packet
    packet = MagicMock()
    packet.haslayer.side_effect = lambda layer: layer in [RadioTap, Dot11, Dot11Beacon, Dot11Elt]
    
    # Setup Dot11 layer
    dot11 = MagicMock()
    dot11.addr3 = "AA:BB:CC:DD:EE:FF"
    
    # Setup SSID (Dot11Elt)
    elt = MagicMock()
    elt.ID = 0
    elt.info = b"Test_SSID"
    # Crucial: payload.getlayer must return None to break the 'while elt' loop in scanner.py
    elt.payload.getlayer.return_value = None 
    
    # Route __getitem__ to the correct mocks
    def get_item(layer):
        if layer == Dot11: return dot11
        if layer == Dot11Elt: return elt
        return MagicMock()
    
    packet.__getitem__.side_effect = get_item
    
    # Setup RadioTap signal
    packet.dBm_AntSignal = -40
    
    scanner._handle_packet(packet)
    
    assert found_ap is not None
    assert found_ap.bssid == "AA:BB:CC:DD:EE:FF"
    assert found_ap.ssid == "Test_SSID"
    assert found_ap.signal == -40
