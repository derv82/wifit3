import asyncio
from types import SimpleNamespace

import pytest

from wifit3.chips.driver import FakeMacSupport
from wifit3.chips.rtl8192fu import constants as C
from wifit3.chips.rtl8192fu.driver import RTL8192FUDriver
from wifit3.chips.rtl8192fu.tx import build_tx_packet, bulk_out_index, stamp_sequence


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


async def test_active_monitor_programs_and_restores_source_mac(mocker):
    driver = object.__new__(RTL8192FUDriver)
    driver.transport = SimpleNamespace(read8=mocker.Mock(return_value=0x0C), write8=mocker.Mock())
    driver._active_monitor_lock = asyncio.Lock()
    driver._efuse = SimpleNamespace(mac_address=bytes.fromhex("001122334455"))
    mac = bytes.fromhex("aabbccddeeff")
    bssid = bytes.fromhex("123456789abc")

    assert RTL8192FUDriver.FAKE_MAC is FakeMacSupport.SPOOFABLE
    assert await driver.enter_active_monitor(mac, bssid) == mac
    assert driver.transport.write8.call_args_list == [
        mocker.call(C.REG_MSR, 0x0C | C.MSR_LINKTYPE_STATION)
    ] + [
        mocker.call(C.REG_MACID + offset, value) for offset, value in enumerate(mac)
    ] + [
        mocker.call(C.REG_BSSID + offset, value) for offset, value in enumerate(bssid)
    ]

    driver.transport.write8.reset_mock()
    await driver.exit_active_monitor()
    assert driver.transport.write8.call_args_list == [
        mocker.call(C.REG_MSR, 0x0C | C.MSR_LINKTYPE_NONE)
    ] + [
        mocker.call(C.REG_MACID + offset, value)
        for offset, value in enumerate(driver._efuse.mac_address)
    ] + [mocker.call(C.REG_BSSID + offset, 0) for offset in range(6)]


async def test_active_monitor_uses_ap_link_type_when_mac_is_bssid(mocker):
    driver = object.__new__(RTL8192FUDriver)
    driver.transport = SimpleNamespace(read8=mocker.Mock(return_value=0), write8=mocker.Mock())
    driver._active_monitor_lock = asyncio.Lock()
    mac = bytes.fromhex("aabbccddeeff")

    await driver.enter_active_monitor(mac, mac)

    driver.transport.write8.assert_any_call(C.REG_MSR, C.MSR_LINKTYPE_AP)


@pytest.mark.parametrize(
    ("endpoint_count", "management_index", "data_index"),
    [(1, 0, 0), (2, 0, 1), (3, 0, 2), (6, 0, 2)],
)
def test_bulk_out_queue_mapping(endpoint_count, management_index, data_index):
    data_frame = bytes.fromhex("48013a01122233445566aabbccddeeffaabbccddeeffe01b")

    assert bulk_out_index(DIRECTED_DEAUTH, endpoint_count) == management_index
    assert bulk_out_index(data_frame, endpoint_count) == data_index


async def test_driver_writes_management_packet_to_first_bulk_endpoint(mocker):
    driver = object.__new__(RTL8192FUDriver)
    driver.transport = SimpleNamespace(
        endpoints=SimpleNamespace(bulk_out=(0x05, 0x06, 0x08)),
        bulk_write=mocker.Mock(),
    )
    driver._tx_lock = asyncio.Lock()
    driver._io_lock = asyncio.Lock()

    assert await driver._inject_frame(DIRECTED_DEAUTH) is True

    driver.transport.bulk_write.assert_called_once_with(
        0x05, build_tx_packet(DIRECTED_DEAUTH)
    )


async def test_driver_writes_data_packet_to_low_queue_endpoint(mocker):
    frame = bytes.fromhex("48013a01122233445566aabbccddeeffaabbccddeeffe01b")
    driver = object.__new__(RTL8192FUDriver)
    driver.transport = SimpleNamespace(
        endpoints=SimpleNamespace(bulk_out=(0x05, 0x06, 0x08, 0x09, 0x0C, 0x0D)),
        bulk_write=mocker.Mock(),
    )
    driver._tx_lock = asyncio.Lock()
    driver._io_lock = asyncio.Lock()

    assert await driver._inject_frame(frame) is True

    driver.transport.bulk_write.assert_called_once_with(0x08, build_tx_packet(frame))


async def test_tx_device_removal_notifies_the_disconnect_path(mocker):
    driver = object.__new__(RTL8192FUDriver)
    error = OSError(19, "No such device")
    disconnect = mocker.Mock()
    driver.transport = SimpleNamespace(
        endpoints=SimpleNamespace(bulk_out=(0x05,)),
        bulk_write=mocker.Mock(side_effect=error),
    )
    driver._tx_lock = asyncio.Lock()
    driver._io_lock = asyncio.Lock()
    driver._device_gone = False
    driver._disconnect_callback = disconnect
    mocker.patch("wifit3.chips.rtl8192fu.driver.is_device_gone", return_value=True)

    with pytest.raises(OSError, match="No such device"):
        await driver._inject_frame(DIRECTED_DEAUTH)

    disconnect.assert_called_once_with(error)
    assert driver._device_gone


def test_build_tx_packet_matches_captured_data_layout():
    frame = bytes.fromhex("48013a01122233445566aabbccddeeffaabbccddeeffe01b")
    descriptor = bytes.fromhex(
        "18002800000000000000010000000000001f00000000000000000000cee00000"
        "0000000000e01b00"
    )

    assert build_tx_packet(frame) == descriptor + frame


def test_tx_rejects_control_frames():
    control_frame = bytearray(DIRECTED_DEAUTH)
    control_frame[0] = 0xD4

    with pytest.raises(ValueError, match="control-frame TX is not ported"):
        build_tx_packet(bytes(control_frame))
    with pytest.raises(ValueError, match="control-frame TX is not ported"):
        stamp_sequence(bytes(control_frame), 0)


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
