"""mt76x0u TX-ACK detection: the RX tap that counts the AP's link-layer ACKs to a
MAC we inject as, and the inject wait-for-ack poll. No hardware — synthetic frames."""
import struct
import time
from unittest.mock import MagicMock

from wifit3.chips.mt76x0u.driver import MT76x0UDriver


def _ack_rx(ra: bytes) -> bytes:
    """A bulk-IN buffer whose MPDU is a 10-byte 802.11 ACK to ``ra``: 36-byte RXWI
    prefix, ctl.MPDU_LEN = 10, MPDU begins at HEADER_SIZE (36)."""
    data = bytearray(64)
    struct.pack_into("<H", data, 0, 60)          # dma_len
    struct.pack_into("<I", data, 8, 10 << 16)    # ctl.MPDU_LEN = 10
    data[36] = 0xD4                              # FC: ACK control subtype
    data[40:46] = ra                             # addr1 / RA
    return bytes(data)


def _driver() -> MT76x0UDriver:
    d = MT76x0UDriver(MagicMock(), MagicMock())
    d._parsed = []
    d.register_rx_callback(d._parsed.append)
    return d


def test_tap_counts_ack_to_our_mac():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._ack_detect_on = True
    d._on_raw_rx(_ack_rx(ra))
    assert d.acks_seen(ra) == 1
    assert ra in d._ack_last_ts
    assert d._parsed == []          # an ACK is never handed to the frame parser


def test_tap_ignores_ack_to_foreign_mac():
    d = _driver()
    ra = bytes.fromhex("aabbccddeeff")
    d._ack_detect_on = True
    d._on_raw_rx(_ack_rx(ra))
    assert d.acks_seen(ra) == 0     # but not one of ours
    assert d._ack_last_ts == {}


def test_tap_off_by_default():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._on_raw_rx(_ack_rx(ra))       # _ack_detect_on stays False
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
