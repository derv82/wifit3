import pytest
from wifit3.chips.ar9271.protocol.wmi import WMIProtocol

def test_wmi_sequence_ids():
    wmi = WMIProtocol()
    assert wmi._get_next_seq() == 1
    assert wmi._get_next_seq() == 2
    
    # Test wrap around (1-254)
    wmi._next_seq_id = 254
    assert wmi._get_next_seq() == 254
    assert wmi._get_next_seq() == 1

def test_wmi_pack_command():
    wmi = WMIProtocol()
    payload = b'\x11\x22\x33\x44'
    
    packet, seq = wmi.pack_command(WMIProtocol.WMI_REG_WRITE_CMDID, payload)
    
    assert seq == 1
    # Command=0x0015, Seq=0x0001
    assert packet[:4] == b'\x00\x15\x00\x01'
    assert packet[4:] == payload

def test_wmi_unpack_event():
    wmi = WMIProtocol()
    # Event=0x0001 (READY), Seq=0x0000, Payload=...
    raw_data = b'\x00\x01\x00\x00\xaa\xbb'
    
    event_id, seq, payload = wmi.unpack_event(raw_data)
    
    assert event_id == WMIProtocol.WMI_READY_EVENTID
    assert seq == 0
    assert payload == b'\xaa\xbb'

def test_wmi_unpack_too_short():
    wmi = WMIProtocol()
    with pytest.raises(ValueError, match="too short"):
        wmi.unpack_event(b'\x00\x01\x00')
