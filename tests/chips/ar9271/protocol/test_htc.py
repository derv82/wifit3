import pytest
from wifit3.chips.ar9271.protocol.htc import HTCProtocol

def test_htc_pack():
    htc = HTCProtocol()
    payload = b'\xde\xad\xbe\xef'
    endpoint = 0x01
    
    packet = htc.pack(endpoint, payload)
    
    # Check header length (6 bytes) + payload
    assert len(packet) == 6 + 4
    # EP=0x01, Flags=0x00, Len=0x0004, Pad=0x0000
    assert packet[:6] == b'\x01\x00\x00\x04\x00\x00'
    assert packet[6:] == payload

def test_htc_unpack():
    htc = HTCProtocol()
    # EP=0x02, Flags=0x00, Len=0x0002, Pad=0x0000 + Payload
    raw_data = b'\x02\x00\x00\x02\x00\x00\x11\x22'
    
    ep, flags, payload = htc.unpack(raw_data)
    
    assert ep == 0x02
    assert flags == 0x00
    assert payload == b'\x11\x22'

def test_htc_credits():
    htc = HTCProtocol()
    assert htc.credits == 0
    
    htc.update_credits(10)
    assert htc.credits == 10
    
    htc.consume_credit()
    assert htc.credits == 9

def test_htc_parse_credit_report():
    htc = HTCProtocol()
    # Heuristic: EP 1, data[16] is credit count
    report = bytearray(32)
    report[0] = 0x01
    report[16] = 42
    
    count = htc.parse_credit_report(bytes(report))
    assert count == 42
