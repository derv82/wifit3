from unittest.mock import Mock

import pytest

from wifit3.chips.rtl8192fu.driver import RTL8192FUDriver
from wifit3.models import DeviceID


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
        0.02,
        0.08,
        0.12,
        0.18,
        0.40,
        0.58,
        0.68,
        0.76,
        0.88,
        0.94,
        1.0,
    ]
    assert [call.args[1] for call in progress.call_args_list] == [
        "Claiming USB interface",
        "Probing chip state",
        "Reading EFUSE",
        "Power-on + firmware download",
        "PHY init (mac/bb/agc/rf tables)",
        "FIFO + RX/MAC init",
        "Applying TX power",
        "Radio calibration",
        "Finishing hardware init",
        "Enabling monitor mode",
        "RTL8192FU online",
    ]
