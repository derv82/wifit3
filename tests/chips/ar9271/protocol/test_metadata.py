import pytest
import struct
from wifit3.chips.ar9271.protocol.metadata import AthMetadataLayer

def test_metadata_parse_rx():
    # RSSI=45, Len=4
    # Format: <LBBBBIHH (8 fields)
    desc = struct.pack("<LBBBBIHH", 4, 45, 11, 0, 1, 1000, 0, 0)
    payload = b'\x11\x22\x33\x44'
    
    frame, rssi, datalen = AthMetadataLayer.parse_rx(desc + payload)
    
    assert datalen == 4
    assert rssi == 45
    assert frame == payload

def test_metadata_parse_rx_too_short():
    frame, rssi, datalen = AthMetadataLayer.parse_rx(b'\x00' * 15)
    assert frame is None
    assert datalen == 0

def test_metadata_pack_tx():
    frame = b'\xaa\xbb\xcc'
    packet = AthMetadataLayer.pack_tx(frame, rate_idx=11, no_ack=True)
    
    assert len(packet) == 8 + 3
    assert packet[8:] == frame
    assert packet[3] == AthMetadataLayer.TX_FLAG_NO_ACK
