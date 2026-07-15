"""ar9271_v2 TX-ACK detection: the RX tap that counts the AP's link-layer ACKs to a MAC we
inject as, and the inject wait-for-ack poll. No hardware — synthetic HIF-stream frames."""
import struct
import time
from unittest.mock import MagicMock

from wifit3.chips.ar9271_v2.driver import AR9271V2Driver


def _ack_buf(ra: bytes) -> bytes:
    """One bulk-IN HIF transfer carrying a 14-B on-wire ACK to ``ra`` (10-B MPDU + 4-B FCS,
    which iter_frames strips). Layout: HIF hdr (4) | htc_frame_hdr (8) | ath_htc_rx_status (40)
    | 802.11 frame | pad. See ar9271_v2/rx_decode.py."""
    mpdu = bytearray(10)
    mpdu[0] = 0xD4                                       # FC: ACK control subtype
    mpdu[4:10] = ra                                      # addr1 / RA
    frame = bytes(mpdu) + b"\x00\x00\x00\x00"            # + FCS trailer
    rxs = bytearray(40)
    struct.pack_into(">H", rxs, 8, len(frame))          # rs_datalen (be16) = 14
    rxs[19] = 0xFF                                       # rs_keyix = ATH9K_RXKEYIX_INVALID
    body = bytes(8) + bytes(rxs) + frame                # htc_frame_hdr + rx_status + frame
    pad = b"\x00" * ((4 - (len(body) & 3)) & 3)
    return struct.pack("<HH", len(body), 0x4E00) + body + pad


def _driver() -> AR9271V2Driver:
    d = AR9271V2Driver(MagicMock())
    d._parsed = []
    d.register_rx_callback(d._parsed.append)
    return d


def test_tap_counts_ack_to_our_mac():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._ack_detect_on = True
    d._dispatch(_ack_buf(ra))
    assert d.acks_seen(ra) == 1
    assert ra in d._ack_last_ts
    assert d._parsed == []          # an ACK is never handed to the frame parser


def test_tap_ignores_ack_to_foreign_mac():
    d = _driver()
    ra = bytes.fromhex("aabbccddeeff")
    d._ack_detect_on = True
    d._dispatch(_ack_buf(ra))
    assert d.acks_seen(ra) == 0     # but not one of ours
    assert d._ack_last_ts == {}


def test_tap_off_by_default():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._dispatch(_ack_buf(ra))       # _ack_detect_on stays False
    assert d.acks_seen(ra) == 0


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
