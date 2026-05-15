import pytest
from wifit3.wlan.packet import WlanFrameParser

def test_wlan_frame_parser_validates():
    # A random bunch of bytes too small to be a frame
    assert WlanFrameParser.parse_80211_frame(b'\x00\x01\x02', -50) is None

def test_wlan_frame_parser_extracts_ssid():
    # Construct a minimal fake beacon frame to test tag parsing
    # MAC Header (24 bytes)
    fc = b'\x80\x00' # Beacon
    dur = b'\x00\x00'
    addr1 = b'\xff\xff\xff\xff\xff\xff'
    addr2 = b'\x11\x22\x33\x44\x55\x66'
    addr3 = b'\x11\x22\x33\x44\x55\x66'
    seq = b'\x00\x00'
    mac_hdr = fc + dur + addr1 + addr2 + addr3 + seq
    
    # Fixed Params (12 bytes)
    fixed = b'\x00' * 12
    
    # Tag 0 (SSID): "Test"
    tag_ssid = b'\x00\x04Test'
    
    frame = mac_hdr + fixed + tag_ssid
    
    parsed = WlanFrameParser.parse_80211_frame(frame, -50)
    assert parsed is not None
    assert parsed["type"] == "beacon"
    assert parsed["bssid"] == "11:22:33:44:55:66"
    assert parsed["ssid"] == "Test"
