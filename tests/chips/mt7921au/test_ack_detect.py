"""mt7921au TX-ACK detection: the RX tap that counts the AP's link-layer ACKs to a MAC we
inject as, the software seq stamp, and the inject descriptor's HW ACK-retry wiring. No
hardware (synthetic frames).

``_enable_rx_acks`` clears RFCR DROP_UNWANTED_CTL via an FW MCU write (a real register write,
not a no-op); the tap lives in ``_on_raw_rx``, before the parser drops the ACK control frame.
The tally and arming live on the ``Driver`` base (``record_ack`` / ``enable_rx_acks`` /
``acks_seen``)."""
import struct
from unittest.mock import AsyncMock, MagicMock

from wifit3.chips.mt7921au import tx
from wifit3.chips.mt7921au.driver import MT7921AUDriver


def _ack_rx(ra: bytes) -> bytes:
    """A connac2 RX buffer whose MPDU is a 10-byte 802.11 ACK to ``ra``: no RXD groups,
    so hdr_gap = 6 words = 24 B and rxd0 length = 24 + 10 (rxd1/rxd2 = 0)."""
    data = bytearray(40)
    struct.pack_into("<I", data, 0, 24 + 10)   # rxd0 length
    data[24] = 0xD4                             # FC: ACK control subtype
    data[28:34] = ra                            # addr1 / RA
    return bytes(data)


def _mgmt_frame(ta: bytes = bytes.fromhex("020000000001")) -> bytes:
    """A 24-B deauth: FC=0xC0, addr1 (RA), addr2 (TA=our injected source), addr3, seqctl."""
    return (b"\xc0\x00\x00\x00" + bytes.fromhex("aabbccddeeff") + ta
            + bytes.fromhex("aabbccddeeff") + b"\x00\x00")


def _driver() -> MT7921AUDriver:
    d = MT7921AUDriver(MagicMock())
    d.transport.send_mcu_command = AsyncMock()   # admit/drop_ack_frames RFCR MCU writes
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


async def test_enable_rx_acks_admits_ctl_via_mcu():
    d = _driver()
    await d.enable_rx_acks()                    # _enable_rx_acks -> admit_ack_frames -> MCU
    assert d.transport.send_mcu_command.await_count == 1


def test_stamp_tx_seq_increments_and_preserves_frag():
    d = _driver()
    frame = _mgmt_frame()
    out = d._stamp_tx_seq(frame)
    assert out is not frame                     # software-stamp returns a fresh buffer
    assert (out[22] | (out[23] << 8)) == 0x10   # first stamp steps seq 0 -> 0x10, frag preserved
    assert d._stamp_tx_seq(frame)[22] == 0x20   # each call advances the driver's counter


async def test_inject_builds_descriptor_with_hw_retry_limit():
    d = _driver()
    d.transport.send_bulk_checked = AsyncMock(return_value=True)
    frame = _mgmt_frame()
    assert await d.inject_frame(frame) is True   # base entry -> _stamp_tx_seq -> _inject_frame
    d.transport.send_bulk_checked.assert_awaited_once()
    wire = d.transport.send_bulk_checked.await_args.args[0]
    txd3 = int.from_bytes(wire[16:20], "little")           # txwi[3]: [SDIO 4B][txwi 3*4B]
    assert (txd3 >> 11) & 0x1F == d.DEFAULT_HW_ACK_RETRIES  # MT_TXD3_REM_TX_COUNT
    assert txd3 & tx.MT_TXD3_NO_ACK == 0                    # always request an ACK now
