"""A wedged chip mid-cold-bringup must surface a clean, actionable BringUpError."""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import usb.core

from wifit3.chips.ar9271_v2.driver import AR9271V2Driver
from wifit3.device.manager import DeviceManager
from wifit3.errors import BringUpError


def _renders_cleanly(e: BringUpError) -> bool:
    """True if ``e`` has a short ``stage`` label + separate ``detail``, the shape
    ``_fault_message`` needs -- not a whole sentence crammed into ``stage`` alone."""
    msg = DeviceManager._fault_message(SimpleNamespace(chipset="AR9271 v2"), e)
    return bool(e.detail) and not msg.rstrip().endswith("again. failed")


async def _run_in_executor(_pool, fn, *args):
    """Runs the "executor" work inline: every mocked call here is synchronous and side-effect
    free (or raises via its own configured ``side_effect``), so no real thread pool is needed."""
    return fn(*args)


def _driver(mocker, original) -> tuple[AR9271V2Driver, MagicMock]:
    driver = AR9271V2Driver(original)
    mocker.patch.object(driver, "_is_chip_warm", return_value=False)
    mocker.patch.object(driver, "_get_port_numbers", return_value=(1,))
    mocker.patch.object(driver, "_find_matching_ar9271_devices", return_value=[original])
    mocker.patch.object(driver, "_claim")
    mocker.patch.object(driver, "_await_reenumeration",
                        AsyncMock(return_value=SimpleNamespace(bus=1, address=8, port_numbers=(1,))))
    mocker.patch("wifit3.chips.ar9271_v2.driver.usb.util.dispose_resources")
    reader = MagicMock(start=MagicMock(), stop=AsyncMock())
    mocker.patch("wifit3.chips.ar9271_v2.driver.RxReaderThread", return_value=reader)
    return driver, reader


@pytest.mark.asyncio
async def test_firmware_download_timeout_becomes_a_clean_replug_error(mocker, caplog):
    original = SimpleNamespace(bus=1, address=7, port_numbers=(1,))
    driver, reader = _driver(mocker, original)
    mocker.patch("wifit3.chips.ar9271_v2.driver.firmware.download",
                 side_effect=usb.core.USBError("Operation timed out"))
    loop = mocker.MagicMock()
    loop.run_in_executor = _run_in_executor

    with caplog.at_level(logging.WARNING):
        with pytest.raises(BringUpError) as exc_info:
            await driver._bring_up_with_loop(loop, b"fw", lambda pct, msg: None)
    assert exc_info.value.stage == "firmware download"
    assert "replug" in str(exc_info.value).lower()
    assert _renders_cleanly(exc_info.value)
    reader.stop.assert_not_called()                    # never started: nothing to tear down
    assert any("firmware download wedged" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_cold_bringup_timeout_becomes_a_clean_replug_error_and_tears_down(mocker, caplog):
    original = SimpleNamespace(bus=1, address=7, port_numbers=(1,))
    driver, reader = _driver(mocker, original)
    mocker.patch("wifit3.chips.ar9271_v2.driver.firmware.download")
    mocker.patch("wifit3.chips.ar9271_v2.driver.bringup.cold_bringup",
                 side_effect=usb.core.USBError("Operation timed out"))
    loop = mocker.MagicMock()
    loop.run_in_executor = _run_in_executor

    with caplog.at_level(logging.WARNING):
        with pytest.raises(BringUpError) as exc_info:
            await driver._bring_up_with_loop(loop, b"fw", lambda pct, msg: None)
    assert exc_info.value.stage == "HTC/WMI init"
    assert "replug" in str(exc_info.value).lower()
    assert _renders_cleanly(exc_info.value)
    reader.stop.assert_awaited_once()                  # torn down, not leaked
    assert driver._reader is None
    assert any("HTC/WMI init wedged" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_warm_reattach_failure_becomes_a_clean_replug_error(mocker, caplog):
    original = SimpleNamespace(bus=1, address=7, port_numbers=(1,))
    driver, reader = _driver(mocker, original)
    mocker.patch.object(driver, "_is_chip_warm", return_value=True)
    mocker.patch.object(driver, "_clear_pipe_halts")
    mocker.patch("wifit3.chips.ar9271_v2.driver.bringup.warm_reattach",
                 side_effect=RuntimeError("WMI not responding"))
    loop = mocker.MagicMock()
    loop.run_in_executor = _run_in_executor

    with caplog.at_level(logging.WARNING):
        with pytest.raises(BringUpError) as exc_info:
            await driver._bring_up_with_loop(loop, b"fw", lambda pct, msg: None)
    assert exc_info.value.stage == "warm reattach"
    assert _renders_cleanly(exc_info.value)
    reader.stop.assert_awaited_once()                  # torn down, not leaked
    assert any("warm reattach failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_post_boot_handshake_double_failure_becomes_a_clean_replug_error(mocker):
    original = SimpleNamespace(bus=1, address=7, port_numbers=(1,))
    driver, _reader = _driver(mocker, original)
    driver._reader = None
    mocker.patch.object(driver, "_teardown_cold_attempt", AsyncMock())

    from wifit3.chips.ar9271_v2 import htc

    async def _always_mis_framed(*_a, **_k):
        raise htc.HTCReadyError(b"\x00" * 8)

    mocker.patch.object(driver, "_bring_up_with_loop", side_effect=_always_mis_framed)

    with pytest.raises(BringUpError) as exc_info:
        await driver.connect()
    assert exc_info.value.stage == "post-boot handshake"
    assert _renders_cleanly(exc_info.value)


def test_claim_exhausted_retries_becomes_a_clean_replug_error(mocker):
    mocker.patch("wifit3.chips.ar9271_v2.driver.time.sleep")     # skip the real ~6s of retries
    mocker.patch("wifit3.chips.ar9271_v2.driver.usb.util.claim_interface",
                 side_effect=usb.core.USBError("Access denied"))
    original = SimpleNamespace(bus=1, address=7, port_numbers=(1,))
    driver = AR9271V2Driver(original)
    dev = SimpleNamespace(set_configuration=MagicMock())

    with pytest.raises(BringUpError) as exc_info:
        driver._claim(dev)
    assert exc_info.value.stage == "claim"
    assert _renders_cleanly(exc_info.value)
