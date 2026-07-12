"""ar9271 (v1, cleanroom-fw) TX-ACK detection: the RX tap that counts the AP's link-layer ACKs
to a MAC we inject as, and the inject wait-for-ack poll. No hardware — synthetic WMI RX events."""
import struct
import time
import zlib
from unittest.mock import MagicMock

from wifit3.chips.ar9271.driver import AR9271Driver
from wifit3.chips.ar9271.protocol.wmi import WMIProtocol


def _ack_frame(ra: bytes) -> bytes:
    """A 14-B on-wire ACK to ``ra``: 10-B 0xD4 MPDU + valid 4-B LE CRC32 FCS."""
    mpdu = bytearray(10)
    mpdu[0] = 0xD4                                       # FC: ACK control subtype
    mpdu[4:10] = ra                                      # addr1 / RA
    fcs = (zlib.crc32(bytes(mpdu)) & 0xFFFFFFFF).to_bytes(4, "little")
    return bytes(mpdu) + fcs


def _rx_event(frame: bytes, ev_id: int = 0x0400, seq: int = 7) -> bytes:
    """A WMI RX-event payload: WMI hdr | 36-B cleanroom RX header (magic + rs_status + declared
    length) | 802.11 frame. ev_id 0x0400 = WMI_RECV_PDU_V14_ID. See protocol/wmi.py."""
    hdr = bytearray(36)
    struct.pack_into(">H", hdr, 4, len(frame))          # off 4-5: frame length (excl header)
    hdr[6] = 0x00                                        # off 6: rs_status = clean
    hdr[8] = 40                                          # off 8: RSSI SNR
    hdr[10:16] = b"\x80\x16\x80\x80\x01\xff"             # off 10-15: firmware RX magic
    return struct.pack(">HH", ev_id, seq) + bytes(hdr) + frame


def _driver() -> AR9271Driver:
    d = AR9271Driver(MagicMock())
    d._parsed = []
    d.register_rx_callback(d._parsed.append)
    return d


def test_ack_ra_extracts_valid_ack():
    ra = bytes.fromhex("020000000001")
    payload = _rx_event(_ack_frame(ra))[4:]             # strip the WMI header
    assert WMIProtocol.ack_ra(payload) == ra


def test_ack_ra_rejects_bad_fcs():
    ra = bytes.fromhex("020000000001")
    frame = bytearray(_ack_frame(ra))
    frame[-1] ^= 0xFF                                    # corrupt the FCS
    payload = _rx_event(bytes(frame))[4:]
    assert WMIProtocol.ack_ra(payload) is None


def test_ack_ra_rejects_missing_magic():
    ra = bytes.fromhex("020000000001")
    payload = bytearray(_rx_event(_ack_frame(ra))[4:])
    payload[10] ^= 0xFF                                  # break the RX-header magic
    assert WMIProtocol.ack_ra(bytes(payload)) is None


def test_tap_counts_ack_to_our_mac():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._ack_detect_on = True
    d._on_wmi_packet(_rx_event(_ack_frame(ra)))
    assert d.acks_seen(ra) == 1
    assert d._all_acks_seen == 1
    assert ra in d._ack_last_ts
    assert d._parsed == []          # an ACK is never handed to the frame parser


def test_tap_ignores_ack_to_foreign_mac():
    d = _driver()
    ra = bytes.fromhex("aabbccddeeff")
    d._ack_detect_on = True
    d._on_wmi_packet(_rx_event(_ack_frame(ra)))
    assert d._all_acks_seen == 1    # seen on-channel
    assert d.acks_seen(ra) == 0     # but not one of ours
    assert d._ack_last_ts == {}


def test_tap_off_by_default():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._on_wmi_packet(_rx_event(_ack_frame(ra)))     # _ack_detect_on stays False
    assert d._all_acks_seen == 0
    assert d.acks_seen(ra) == 0
    assert d._parsed == []          # ACK is a control frame -> parser drops it either way


async def test_await_ack_true_when_ts_fresh():
    d = _driver()
    ta = bytes.fromhex("020000000001")
    since = time.monotonic()
    d._ack_last_ts[ta] = since + 1.0            # ACK landed after `since`
    assert await d._await_ack(ta, since, 0.05) is True


async def test_await_ack_false_on_timeout():
    d = _driver()
    ta = bytes.fromhex("020000000001")
    since = time.monotonic()
    assert await d._await_ack(ta, since, 0.005) is False   # no ts recorded -> window elapses
