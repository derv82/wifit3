import asyncio
from types import SimpleNamespace

import pytest

from wifit3.chips.rtl8192fu.driver import RTL8192FUDriver
from wifit3.chips.rtl8192fu.tx import build_tx_packet, stamp_sequence


DIRECTED_DEAUTH = bytes.fromhex(
    "c0003a01"
    "122233445566"
    "aabbccddeeff"
    "aabbccddeeff"
    "0000"
    "0700"
)


def test_build_tx_packet_matches_captured_directed_deauth_layout():
    expected_descriptor = bytes.fromhex(
        "1a002800"
        "00120000"
        "00000100"
        "00010000"
        "00001a00"
        "00000000"
        "00000000"
        "d6ec0000"
        "00000000"
        "00000000"
    )

    assert build_tx_packet(DIRECTED_DEAUTH) == expected_descriptor + DIRECTED_DEAUTH


def test_build_tx_packet_sets_broadcast_multicast_bit_and_checksum():
    broadcast = bytearray(DIRECTED_DEAUTH)
    broadcast[4:10] = b"\xff" * 6
    packet = build_tx_packet(bytes(broadcast))

    assert int.from_bytes(packet[0:2], "little") == len(broadcast)
    assert packet[3] & 1
    checksum = 0
    for offset in range(0, 32, 2):
        checksum ^= int.from_bytes(packet[offset:offset + 2], "little")
    assert checksum == 0xFFFF


def test_build_tx_packet_selects_beacon_queue():
    beacon = bytearray(DIRECTED_DEAUTH)
    beacon[0] = 0x80

    packet = build_tx_packet(bytes(beacon))

    assert int.from_bytes(packet[4:8], "little") >> 8 & 0x1F == 0x10


def test_build_tx_packet_copies_sequence_number_to_descriptor():
    frame = stamp_sequence(DIRECTED_DEAUTH, 0xABC)
    packet = build_tx_packet(frame)

    assert int.from_bytes(frame[22:24], "little") >> 4 == 0xABC
    assert int.from_bytes(packet[36:40], "little") == 0xABC000


def test_stamp_sequence_preserves_fragment_number_and_wraps():
    frame = bytearray(DIRECTED_DEAUTH)
    frame[22:24] = (0x000B).to_bytes(2, "little")

    stamped = stamp_sequence(bytes(frame), 0x1001)

    assert int.from_bytes(stamped[22:24], "little") == 0x001B


async def test_rx_ack_hooks_use_existing_monitor_filter():
    driver = object.__new__(RTL8192FUDriver)

    assert await driver._enable_rx_acks() is None
    assert await driver._disable_rx_acks() is None


async def test_driver_writes_management_packet_to_first_bulk_endpoint(mocker):
    driver = object.__new__(RTL8192FUDriver)
    driver.transport = SimpleNamespace(
        endpoints=SimpleNamespace(bulk_out=(0x05, 0x06, 0x08)),
        bulk_write=mocker.Mock(),
    )
    driver._tx_lock = asyncio.Lock()

    assert await driver._inject_frame(DIRECTED_DEAUTH) is True

    driver.transport.bulk_write.assert_called_once_with(
        0x05, build_tx_packet(DIRECTED_DEAUTH)
    )


def test_tx_rejects_non_management_frames():
    data_frame = bytearray(DIRECTED_DEAUTH)
    data_frame[0] = 0x08

    with pytest.raises(ValueError, match="data/control TX is not ported"):
        build_tx_packet(bytes(data_frame))
    with pytest.raises(ValueError, match="data/control TX is not ported"):
        stamp_sequence(bytes(data_frame), 0)


def test_tx_frame_size_includes_descriptor_in_usb_limit():
    largest = bytearray(0xFFFF - 40)
    largest[0] = 0x00
    build_tx_packet(bytes(largest))

    oversized = bytes(0xFFFF - 39)
    with pytest.raises(ValueError, match="too large"):
        build_tx_packet(oversized)
    with pytest.raises(ValueError, match="too large"):
        stamp_sequence(oversized, 0)


@pytest.mark.parametrize("frame", [b"", bytes(23)])
def test_tx_rejects_short_frames(frame):
    with pytest.raises(ValueError, match="shorter than its header"):
        build_tx_packet(frame)
    with pytest.raises(ValueError, match="shorter than its header"):
        stamp_sequence(frame, 0)
