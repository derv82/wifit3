import zlib

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


# ---- QoS alignment-padding + FCS (HW-confirmed bug, 2026-05-25) ------------
# ath9k inserts 2 bytes of DMA alignment padding after a 26-B QoS MAC header so
# the payload is 4-byte aligned, but the over-the-air FCS excludes it. Without
# stripping it, every QoS frame (all downlink-unicast data AND all 4-way EAPOL)
# fails the FCS gate. Synthesized below with fake locally-administered MACs so
# no real network identifiers land in the repo.

def _rx_header(declared_len: int, snr: int = 0x1c) -> bytes:
    h = bytearray(WMIProtocol.HTC_RX_HEADER_LEN)
    h[4:6] = declared_len.to_bytes(2, "big")  # frame length (incl. pad + FCS)
    h[6] = 0x00                               # rs_status OK
    h[8] = snr                                # RSSI SNR
    h[10:16] = b"\x80\x16\x80\x80\x01\xff"    # firmware magic
    return bytes(h)


def _qos_eapol_m1_payload(*, insert_pad: bool = True) -> bytes:
    # 26-byte QoS-data header, from_ds (AP->client), fake addresses.
    fc, dur = b"\x88\x02", b"\x00\x00"
    ra = bytes.fromhex("02deadbeef01")   # client (RA)
    bssid = bytes.fromhex("02deadbeef02")
    hdr = fc + dur + ra + bssid + bssid + b"\x10\x00" + b"\x00\x00"  # 26 B
    # LLC/SNAP + a minimal 802.1X EAPOL-Key M1 (ACK set, MIC/INSTALL clear).
    dot1x = bytearray(99)
    dot1x[0], dot1x[1] = 0x02, 0x03      # version, type=EAPOL-Key
    dot1x[4] = 0x02                      # key desc type = RSN
    dot1x[5:7] = (0x0080).to_bytes(2, "big")  # key info: KEY_ACK only -> M1
    body = b"\xaa\xaa\x03\x00\x00\x00\x88\x8e" + bytes(dot1x)
    fcs = (zlib.crc32(hdr + body) & 0xFFFFFFFF).to_bytes(4, "little")
    frame = hdr + (b"\x00\x00" if insert_pad else b"") + body + fcs
    return _rx_header(len(frame)) + frame


def test_parse_rx_frame_qos_eapol_padding_stripped():
    parsed = WMIProtocol.parse_rx_frame(_qos_eapol_m1_payload(insert_pad=True))
    assert parsed is not None, "QoS EAPOL must survive the FCS gate after de-pad"
    assert parsed["type"] == "eapol"
    assert parsed["eapol_msg_num"] == 1


def test_strip_alignment_padding_qos_removes_two_bytes():
    hdr = b"\x88\x02" + b"\x00" * 24          # 26-byte QoS header
    body = b"\xaa\xaa\x03\x00\x00\x00\x88\x8e" + b"\x01" * 20
    assert WMIProtocol._strip_alignment_padding(hdr + b"\xDE\xAD" + body) == hdr + body


def test_strip_alignment_padding_nonqos_is_noop():
    hdr = b"\x08\x02" + b"\x00" * 22          # 24-byte non-QoS data header
    frame = hdr + b"\xaa\xaa\x03\x00\x00\x00\x88\x8e" + b"\x01" * 20
    assert WMIProtocol._strip_alignment_padding(frame) == frame


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
