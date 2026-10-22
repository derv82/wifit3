import struct
from unittest.mock import Mock

import pytest

from wifit3.chips.rtl8192fu.driver import RTL8192FUDriver
from wifit3.models import DeviceID


def _rx_packet(frame: bytes, *, report: bool = False) -> bytes:
    word2 = 1 << 28 if report else 0
    return struct.pack("<6I", len(frame), 0, word2, 0, 0, 0) + frame


def test_dispatch_records_a_tracked_raw_ack_without_packet_callback():
    driver = RTL8192FUDriver(Mock(), DeviceID(0x2001, 0x3328, "RTL8192FU"))
    transmitter = bytes.fromhex("001122334455")
    driver._ack_detect_on = True
    driver._our_tx_macs.add(transmitter)

    driver._dispatch(_rx_packet(b"\xd4\x00\x00\x00" + transmitter))

    assert driver.acks_seen(transmitter) == 1


def test_dispatch_ignores_untracked_or_report_ack_payloads():
    driver = RTL8192FUDriver(Mock(), DeviceID(0x2001, 0x3328, "RTL8192FU"))
    tracked = bytes.fromhex("001122334455")
    other = bytes.fromhex("aabbccddeeff")
    driver._ack_detect_on = True
    driver._our_tx_macs.add(tracked)

    driver._dispatch(_rx_packet(b"\xd4\x00\x00\x00" + other))
    driver._dispatch(_rx_packet(b"\xd4\x00\x00\x00" + tracked, report=True))

    assert driver.acks_seen(tracked) == 0


@pytest.mark.asyncio
async def test_connect_reports_each_cold_bringup_phase_in_source_order(mocker):
    driver = RTL8192FUDriver(Mock(), DeviceID(0x2001, 0x3328, "RTL8192FU"))
    driver.transport = Mock()
    operations: list[str] = []
    for name in (
        "_identify_chip",
        "_read_efuse",
        "_firmware_bringup",
        "_table_bringup",
        "_configure_fifo_and_mac",
        "_apply_tx_power",
        "_calibrate_radio",
        "_finish_cold_init",
    ):
        mocker.patch.object(driver, name, side_effect=lambda name=name: operations.append(name))
    mocker.patch.object(driver, "_set_channel_sync", side_effect=lambda _: operations.append("_set_channel_sync"))

    reader = Mock()
    mocker.patch("wifit3.chips.rtl8192fu.driver.RxReaderThread", return_value=reader)
    mocker.patch("wifit3.chips.rtl8192fu.driver.enable_rf", side_effect=lambda _: operations.append("enable_rf"))
    mocker.patch(
        "wifit3.chips.rtl8192fu.driver.open_rx_filters",
        side_effect=lambda _: operations.append("open_rx_filters"),
    )
    mocker.patch(
        "wifit3.chips.rtl8192fu.driver.configure_monitor_filter",
        side_effect=lambda _: operations.append("configure_monitor_filter"),
    )
    progress = Mock()

    assert await driver.connect(progress)

    assert operations == [
        "_identify_chip",
        "_read_efuse",
        "_firmware_bringup",
        "_table_bringup",
        "_configure_fifo_and_mac",
        "_apply_tx_power",
        "_calibrate_radio",
        "_finish_cold_init",
        "enable_rf",
        "open_rx_filters",
        "configure_monitor_filter",
        "_set_channel_sync",
    ]
    assert [call.args[0] for call in progress.call_args_list] == [
        0.00,
        0.05,
        0.10,
        0.15,
        0.25,
        0.35,
        0.55,
        0.60,
        0.70,
        0.85,
        0.95,
        1.0,
    ]
    assert [call.args[1] for call in progress.call_args_list] == [
        "Claiming USB interface",
        "Probing chip state",
        "MAC power-on (cold)",
        "Pre-FW init (LLT + DROP_DATA_EN)",
        "Enable FW download",
        "Uploading firmware",
        "Disable FW download",
        "Validating FW (FW_READY_LEGACY)",
        "Post-FW MAC init",
        "PHY init (mac/bb/agc/rf tables)",
        "Tuning to channel 1",
        "RTL8192FU online",
    ]


@pytest.mark.asyncio
async def test_close_matches_source_stop_order_before_power_off(mocker):
    driver = RTL8192FUDriver(Mock(), DeviceID(0x2001, 0x3328, "RTL8192FU"))
    driver.transport = Mock(interface_number=0)
    driver._efuse = Mock()
    driver.is_warm = True
    operations: list[tuple[str, int, int] | tuple[str]] = []
    driver.transport.write8.side_effect = lambda register, value: operations.append(
        ("write8", register, value)
    )
    driver.transport.write16.side_effect = lambda register, value: operations.append(
        ("write16", register, value)
    )
    driver.transport.release.side_effect = lambda: operations.append(("release",))
    mocker.patch(
        "wifit3.chips.rtl8192fu.driver.disable_rf",
        side_effect=lambda _: operations.append(("disable_rf",)),
    )
    mocker.patch(
        "wifit3.chips.rtl8192fu.driver.power_off",
        side_effect=lambda _: operations.append(("power_off",)),
    )

    await driver.close()

    assert operations == [
        ("write8", 0x0522, 0xFF),
        ("write16", 0x06A0, 0),
        ("write16", 0x06A4, 0),
        ("write8", 0x0522, 0xFF),
        ("disable_rf",),
        ("power_off",),
        ("release",),
    ]
    assert driver._efuse is None
    assert not driver.is_warm


@pytest.mark.asyncio
async def test_physical_disconnect_forwards_error_and_releases_without_register_io(mocker):
    driver = RTL8192FUDriver(Mock(), DeviceID(0x2001, 0x3328, "RTL8192FU"))
    driver.transport = Mock(interface_number=0)
    disconnect = Mock()
    driver.register_disconnect_callback(disconnect)
    error = OSError(19, "No such device")
    mocker.patch("wifit3.chips.rtl8192fu.driver.is_device_gone", return_value=True)
    power_off = mocker.patch("wifit3.chips.rtl8192fu.driver.power_off")

    driver._on_rx_fatal(error)
    await driver.close()

    disconnect.assert_called_once_with(error)
    driver.transport.write8.assert_not_called()
    driver.transport.write16.assert_not_called()
    power_off.assert_not_called()
    driver.transport.release.assert_called_once_with()
