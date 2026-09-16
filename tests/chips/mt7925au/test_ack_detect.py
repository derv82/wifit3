"""mt7925au TX-ACK detection: the RX tap that counts a recipient's link-layer ACKs to a MAC we
inject as. No hardware (synthetic connac3 RX buffers).

``_enable_rx_acks`` is a documented no-op — the connac3 sniffer RX filter (MT_FILTER_CONTROL, set
in enter_monitor) already admits ACK control frames to any RA, so there is nothing to toggle
(unlike the connac2 sibling, which clears RFCR DROP_UNWANTED_CTL). The tap lives in ``_on_raw_rx``,
before the parser drops the ACK control frame; the tally and arming live on the ``Driver`` base
(``record_ack`` / ``enable_rx_acks`` / ``acks_seen``)."""
import struct
from unittest.mock import AsyncMock, MagicMock

from wifit3.chips.mt7925au.driver import MT7925AUDriver


def _ack_rx(ra: bytes) -> bytes:
    """A connac3 RX buffer whose MPDU is a 10-byte 802.11 ACK to ``ra``: no RXD groups, so the
    8-dword base RXD (32 B) is the whole header and rxd0 length = 32 + 10 (rxd1/2/3 = 0)."""
    data = bytearray(42)
    struct.pack_into("<I", data, 0, 32 + 10)   # rxd0 length
    data[32] = 0xD4                             # FC: ACK control subtype
    data[36:42] = ra                           # RA = frame[4:10]
    return bytes(data)


def _driver() -> MT7925AUDriver:
    d = MT7925AUDriver(MagicMock())
    d.transport.send_mcu_command = AsyncMock()   # so a stray MCU write would be observable
    d._parsed = []
    d.register_rx_callback(d._parsed.append)
    return d


async def test_tap_counts_ack_to_our_mac():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    await d.enable_rx_acks()                    # arms the base tally (clears _our_tx_macs)
    d._our_tx_macs.add(ra)
    d._on_raw_rx(_ack_rx(ra))
    assert d.acks_seen(ra) == 1
    assert d._parsed == []                      # an ACK is never handed to the frame parser


async def test_tap_ignores_ack_to_foreign_mac():
    d = _driver()
    ra = bytes.fromhex("aabbccddeeff")
    await d.enable_rx_acks()
    d._on_raw_rx(_ack_rx(ra))                   # armed, but ra is not one of ours
    assert d.acks_seen(ra) == 0


def test_tap_off_by_default():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._on_raw_rx(_ack_rx(ra))                   # never enabled -> _ack_detect_on stays False
    assert d.acks_seen(ra) == 0


async def test_disable_rx_acks_stops_the_tally():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    await d.enable_rx_acks()
    d._our_tx_macs.add(ra)
    await d.disable_rx_acks()
    d._on_raw_rx(_ack_rx(ra))
    assert d.acks_seen(ra) == 0


async def test_enable_rx_acks_is_noop_no_mcu_write():
    d = _driver()
    await d.enable_rx_acks()                    # connac3: sniffer filter already admits ACKs
    assert d.transport.send_mcu_command.await_count == 0
