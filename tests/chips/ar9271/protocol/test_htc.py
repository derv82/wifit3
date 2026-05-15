import pytest
from wifit3.chips.ar9271.protocol.htc import HTCProtocol

def test_htc_pack():
    htc = HTCProtocol()
    payload = b'\xde\xad\xbe\xef'
    endpoint = 0x01
    
    packet = htc.pack_wmi(endpoint, payload)
    
    # 6 byte HTC + 2 byte pad + 4 byte payload = 12
    assert len(packet) == 6 + 2 + 4
    # EP=0x01, Flags=0x00, Len=0x0006, Pad=0x0000
    assert packet[:6] == b'\x01\x00\x00\x06\x00\x00'
    assert packet[6:] == b'\x00\x00' + payload

def test_htc_unpack():
    htc = HTCProtocol()
    # EP=0x02, Flags=0x00, Len=0x0002, Ctrl=0x00000000 + Payload
    raw_data = b'\x02\x00\x00\x02\x00\x00\x00\x00\x11\x22'
    
    ep, flags, trailer_len, payload = htc.unpack(raw_data, 0x83)
    
    assert ep == 0x02
    assert flags == 0x00
    assert trailer_len == 0
    assert payload == b'\x11\x22'

def test_htc_credits():
    htc = HTCProtocol()
    assert htc.credits == 0
    
    htc.update_credits(10)
    assert htc.credits == 10
    
    htc.consume_credit()
    assert htc.credits == 9

def test_htc_parse_ready_msg():
    htc = HTCProtocol()
    # MsgID(0x0001), Credits(10), CreditSize(128)
    report = b'\x00\x01\x00\x0a\x00\x80\x00\x00'
    
    credits, size = htc.parse_ready_msg(report)
    assert credits == 10
    assert size == 128

