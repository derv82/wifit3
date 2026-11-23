"""A wedged chip mid-cold-bringup must surface a clean, actionable BringUpError."""
import errno
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import usb.core

from wifit3.chips.ar9271_v2.driver import AR9271V2Driver
from wifit3.chips.ar9271_v2.transport import AR9271Transport
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
    mocker.patch.object(AR9271Transport, "alloc_reg_in_urbs")
    mocker.patch.object(AR9271Transport, "dealloc_reg_in_urbs")
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
    AR9271Transport.dealloc_reg_in_urbs.assert_called_once()
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
    AR9271Transport.dealloc_reg_in_urbs.assert_called_once()
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


def test_warm_probe_leaves_the_pipe_toggles_alone(mocker):
    # a rebooted chip ignores clear_halt, so a host-side reset desyncs REG_IN and HTC READY is lost
    calls: list[str] = []
    dev = SimpleNamespace(
        set_configuration=MagicMock(),
        clear_halt=lambda ep: calls.append(f"clear:{ep}"),
        read=lambda ep, size, timeout: calls.append("read") or b"\x00\x00\x00\x4e",
    )
    driver = AR9271V2Driver(dev)
    mocker.patch("wifit3.chips.ar9271_v2.driver.usb.util.claim_interface")
    mocker.patch("wifit3.chips.ar9271_v2.driver.usb.util.release_interface")

    assert driver._is_chip_warm() is True
    assert not any(call.startswith("clear:") for call in calls)


def _probe_dev(reads):
    """bulk-IN returns ``reads`` in order (bytes, or None for a read timeout), then times out."""
    reads = list(reads)

    def read(ep, size, timeout):
        result = reads.pop(0) if reads else None
        if result is None:
            raise usb.core.USBTimeoutError("Operation timed out")
        return result

    return SimpleNamespace(set_configuration=MagicMock(), clear_halt=lambda ep: None, read=read)


HIF_RX = bytes([0, 0, 0, 0x4e])


def test_warm_probe_ignores_what_a_rebooted_chip_left_in_the_fifo(mocker):
    mocker.patch("wifit3.chips.ar9271_v2.driver.usb.util.claim_interface")
    mocker.patch("wifit3.chips.ar9271_v2.driver.usb.util.release_interface")
    driver = AR9271V2Driver(_probe_dev([HIF_RX, HIF_RX, None]))
    assert driver._is_chip_warm() is False


def test_warm_probe_sees_rx_that_keeps_streaming(mocker):
    mocker.patch("wifit3.chips.ar9271_v2.driver.usb.util.claim_interface")
    mocker.patch("wifit3.chips.ar9271_v2.driver.usb.util.release_interface")
    driver = AR9271V2Driver(_probe_dev([HIF_RX, None, HIF_RX]))
    assert driver._is_chip_warm() is True


@pytest.mark.parametrize("adopted, removed, rebooted", [
    (True, False, True), (True, True, False), (False, False, False)])
async def test_close_reboots_to_the_bootloader_like_ath9k_hif_usb_disconnect(
        mocker, adopted, removed, rebooted):
    # hif_usb.c:1447-1450: only when the firmware was loaded and the device is still attached
    mocker.patch("wifit3.chips.ar9271_v2.driver.usb.util.dispose_resources")
    driver = AR9271V2Driver(SimpleNamespace())
    driver.wmi = object() if adopted else None
    driver.transport.removed = removed
    calls = []
    mocker.patch.object(driver, "_ath9k_htc_stop", side_effect=lambda: calls.append("ath9k_htc_stop"))
    mocker.patch.object(driver.transport, "reboot", side_effect=lambda: calls.append("reboot"))
    await driver.close()
    assert calls == (["ath9k_htc_stop", "reboot"] if rebooted else [])     # ath9k_htc_stop runs first


async def test_close_asks_the_rx_reader_to_stop_before_stopping_rx(mocker):
    # after WMI_STOP_RECV bulk-IN is silent, so a reader still reading would wait out its timeout
    mocker.patch("wifit3.chips.ar9271_v2.driver.usb.util.dispose_resources")
    driver = AR9271V2Driver(SimpleNamespace())
    driver.wmi = object()
    calls = []

    async def reader_stop():
        calls.append("reader stop")

    driver._reader = SimpleNamespace(stop=reader_stop)
    mocker.patch.object(driver, "_ath9k_htc_stop", side_effect=lambda: calls.append("ath9k_htc_stop"))
    mocker.patch.object(driver.transport, "reboot")
    await driver.close()
    assert calls == ["reader stop", "ath9k_htc_stop"]
    assert driver._reader is None


async def test_close_finishes_when_the_device_goes_during_the_stop_commands(mocker):
    # wmi.c:323-324: once AH_UNPLUGGED, ath9k_wmi_cmd returns 0 and ath9k_htc_stop carries on
    dispose = mocker.patch("wifit3.chips.ar9271_v2.driver.usb.util.dispose_resources")
    driver = AR9271V2Driver(SimpleNamespace())
    driver.wmi = SimpleNamespace(
        cmd_or_none=MagicMock(side_effect=usb.core.USBError("gone", errno=errno.ENODEV)))
    driver.htc_tx = SimpleNamespace(tx_drain=MagicMock())
    driver._operation_lock = threading.RLock()
    mocker.patch.object(driver.transport, "reboot")
    await driver.close()
    driver.htc_tx.tx_drain.assert_called_once()
    dispose.assert_called_once()


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
