import pytest
from wifit3.chips.ar9271.protocol.wmi import WMIProtocol


# Real beacon captured 2026-05-22 from htc_9271_cleanroom.fw via
# WIFIT3_AR9271_DUMP_RX. Used as a gold sample for parse_rx_frame regression.
#   header byte 8 = 0x1c → SNR 28 → -67 dBm
#   beacon at offset 36, BSSID aa:da:c4:0d:9c:fe (hidden SSID).
_REAL_BEACON_HEX = "008e87ad00f600001c1c8016808001ff1b000000000000001c018080808080808080808080000000ffffffffffffaadac40d9cfeaadac40d9cfe50638051e8eadb000000640031140000010882848b960c12182403010a0504000100002a010032043048606c460573c00000002d1aac011bffffff00000000000000000001000000000000000000003d160a0017000000000000000000000000000000000000004a0e14000a002c01c8001400050019007f0801000f0000000040dd180050f2020101800003a4000027a4000042435e0062322f00dd0900037f01010000ff7fdd1e001d0f100107000098dac40d9cfe98dac40d9cfeadbe00009cfe0001000030140100000fac040100000fac040100000fac0200007f73a830"


def test_parse_rx_frame_real_beacon():
    payload = bytes.fromhex(_REAL_BEACON_HEX)
    parsed = WMIProtocol.parse_rx_frame(payload)
    assert parsed is not None, "real captured beacon must parse"
    assert parsed["type"] == "beacon"
    assert parsed["bssid"] == "aa:da:c4:0d:9c:fe"
    assert parsed["rssi"] == -67   # NOISE_FLOOR (-95) + SNR 28


def test_parse_rx_frame_rejects_missing_magic():
    payload = bytearray(bytes.fromhex(_REAL_BEACON_HEX))
    payload[10] = 0x00   # corrupt the firmware magic
    assert WMIProtocol.parse_rx_frame(bytes(payload)) is None


def test_parse_rx_frame_rejects_length_mismatch():
    payload = bytearray(bytes.fromhex(_REAL_BEACON_HEX))
    payload[4] = 0xff    # claim a frame length that doesn't match wire len
    assert WMIProtocol.parse_rx_frame(bytes(payload)) is None


def test_parse_rx_frame_rejects_too_short():
    assert WMIProtocol.parse_rx_frame(b"\x00" * 49) is None
    assert WMIProtocol.parse_rx_frame(b"") is None


def test_parse_rx_frame_rejects_bad_fcs():
    # Flip a byte in the middle of the 802.11 body — FCS will fail.
    payload = bytearray(bytes.fromhex(_REAL_BEACON_HEX))
    payload[100] ^= 0xff
    assert WMIProtocol.parse_rx_frame(bytes(payload)) is None


def test_parse_rx_frame_rejects_firmware_rx_error_flag():
    # byte 6 is the cleanroom firmware's rs_status. Non-zero = bad-FCS or
    # other RX error. Must be rejected even when the frame body looks valid.
    payload = bytearray(bytes.fromhex(_REAL_BEACON_HEX))
    payload[6] = 0x01
    assert WMIProtocol.parse_rx_frame(bytes(payload)) is None


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
