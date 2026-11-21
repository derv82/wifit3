import errno

import pytest
import usb.core

from wifit3.chips.rtl8922au import firmware, mac
from wifit3.chips.rtl8922au.constants import (
    R_AX_BOOT_DBG, R_BE_UDM1, R_BE_WCPU_FW_CTRL, R_BE_WLCPU_PORT_PC,
)


@pytest.fixture(autouse=True)
def _clock_1ms_per_read(mocker):
    now_us = 0

    def perf_counter():
        nonlocal now_us
        now_us += 1000
        return now_us / 1_000_000

    def sleep(seconds):
        nonlocal now_us
        now_us += round(seconds * 1_000_000)
    mocker.patch("wifit3.chips.rtl8922au.iopoll.time.perf_counter", side_effect=perf_counter)
    mocker.patch("wifit3.chips.rtl8922au.iopoll.time.sleep", side_effect=sleep)


def _patch_cpu_steps(mocker):
    mocker.patch.object(mac, "disable_cpu")
    mocker.patch.object(mac, "fwdl_enable_wcpu")
    return mocker.patch.object(firmware, "dl_fail_dump")


def test_fw_download_retries_after_a_failed_attempt(mocker):
    dump = _patch_cpu_steps(mocker)
    download = mocker.patch.object(firmware, "download", side_effect=[TimeoutError, None])
    mac.fw_download(mocker.Mock(), 2, 0)
    assert download.call_count == 2
    assert mac.disable_cpu.call_count == 2
    dump.assert_called_once()


def test_fw_download_gives_up_after_five_attempts(mocker):
    dump = _patch_cpu_steps(mocker)
    download = mocker.patch.object(firmware, "download", side_effect=RuntimeError)
    with pytest.raises(RuntimeError):
        mac.fw_download(mocker.Mock(), 2, 0)
    assert download.call_count == 5
    assert dump.call_count == 5


def test_fw_download_does_not_retry_an_unplugged_device(mocker):
    dump = _patch_cpu_steps(mocker)
    download = mocker.patch.object(firmware, "download",
                                   side_effect=usb.core.USBError("gone", errno=errno.ENODEV))
    with pytest.raises(usb.core.USBError):
        mac.fw_download(mocker.Mock(), 2, 0)
    download.assert_called_once()
    dump.assert_not_called()


def test_dl_fail_dump_reads_the_fw_state_then_15_program_counters(mocker, caplog):
    mocker.patch.object(firmware.time, "sleep")
    t = mocker.Mock()
    t.read32.return_value = 0
    firmware.dl_fail_dump(t)
    assert [c.args[0] for c in t.read32.call_args_list] == (
        [R_BE_WCPU_FW_CTRL, R_AX_BOOT_DBG] + [R_BE_WLCPU_PORT_PC] * 15)
    assert caplog.text.count("[ERR]fw PC = 0x0") == 15


def test_download_suit_logs_an_h2c_path_ready_timeout(mocker, caplog):
    mocker.patch.object(firmware, "_fwdl_check_path_ready", side_effect=TimeoutError)
    with pytest.raises(TimeoutError):
        firmware.download_suit(mocker.Mock(), 2, b"", {})
    assert "[ERR]H2C path ready" in caplog.text


def test_wcpu_on_warns_when_an_aon_debug_register_is_not_empty(mocker, caplog):
    t = mocker.Mock()
    t.read32.side_effect = lambda addr: 0x5 if addr == R_BE_UDM1 else 0
    mac.wcpu_on(t, 0, True)
    assert "[SER] AON L2 Debug register not empty before Boot." in caplog.text
    assert "[SER] wcpu_on: R_BE_UDM1 = 0x5" in caplog.text
