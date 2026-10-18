"""Cold bring-up ordering: `_cold_bring_up` sequences its MAC/PHY blocks in the
kernel `rtl8xxxu_init_device` order (`core.c:3918-4412`)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import wifit3.chips.rtl8192eu.driver as drv
from wifit3.chips.rtl8192eu.constants import REG_SYS_CFG
from wifit3.chips.rtl8192eu.driver import RTL8192EUDriver
from wifit3.chips.rtl8192eu.efuse import EfuseDefaults
from wifit3.chips.rtl8192eu.mac import TxQueues
from wifit3.chips.rtl8192eu.rx import Endpoints
from wifit3.errors import BringUpError

from tests.chips.rtl8192eu._mock import MockTransport

# Every bring-up step `_cold_bring_up` runs, in the kernel order.
_EXPECTED_ORDER = [
    "probe_endpoints",
    "config_endpoints",
    "power_on",
    "pre_fw_queue_init",
    "load_firmware_blob",
    "download_firmware",
    "start_firmware",
    "read_and_parse",
    "post_fw_mac_init",
    "post_mac_init_phy",
    "set_rf_switch_ctrl",
    "post_phy_mac_init",
    "enable_rx_data_path",
    "init_adaptive_controls",
    "init_beacon_params",
    "enable_cck_ofdm_block",
    "invalidate_cam_entries",
    "set_tx_power",
    "init_hw_sequencer",
    "init_rtl8192e_tail",
    "phy_lc_calibrate",
    "phy_iq_calibrate",
    "enable_thermal_meter",
    "set_nav_upper",
    "disable_usb_hrpwm",
    "enable_mgmt_ack",
    "fix_ldpc_rx_hang",
    "read_cck_agc",
    "enable_rf",
    "finish_attach",
]

# The driver-namespace helper functions (patched by name), minus the three that
# need custom return values.
_MODULE_FUNCS = [
    "pre_fw_queue_init", "load_firmware_blob", "download_firmware", "start_firmware",
    "post_fw_mac_init", "post_mac_init_phy", "set_rf_switch_ctrl", "post_phy_mac_init",
    "enable_rx_data_path", "init_adaptive_controls", "init_beacon_params",
    "enable_cck_ofdm_block", "invalidate_cam_entries", "set_channel_2g_20mhz",
    "set_tx_power", "init_hw_sequencer", "init_rtl8192e_tail", "phy_lc_calibrate",
    "phy_iq_calibrate", "enable_thermal_meter", "set_nav_upper", "disable_usb_hrpwm",
    "enable_mgmt_ack", "fix_ldpc_rx_hang", "enable_rf",
]


def _make_driver() -> RTL8192EUDriver:
    driver = RTL8192EUDriver(MagicMock())
    transport = MockTransport()
    transport.seed(REG_SYS_CFG, 0)
    driver.transport = transport
    return driver


def _recorder(calls: list[str], name: str, result=None):
    def _fn(*_a, **_k):
        calls.append(name)
        return result
    return _fn


def _record(monkeypatch, calls: list[str], driver: RTL8192EUDriver, *, efuse_error=None):
    monkeypatch.setattr(
        drv, "probe_endpoints",
        lambda dev: (calls.append("probe_endpoints"),
                     Endpoints(bulk_in=[0x84], bulk_out=[0x02, 0x03, 0x04], interrupt=[]))[1],
    )
    monkeypatch.setattr(
        drv, "config_endpoints",
        lambda t, n: (calls.append("config_endpoints"), TxQueues(True, True, True))[1],
    )
    monkeypatch.setattr(driver, "_power_on", _recorder(calls, "power_on"))
    monkeypatch.setattr(driver, "_read_cck_agc_report_type", _recorder(calls, "read_cck_agc"))

    async def _finish(_update, *, from_warm):
        calls.append("finish_attach")
        return True

    monkeypatch.setattr(driver, "_finish_attach", _finish)
    for name in _MODULE_FUNCS:
        result = b"\x00" * 64 if name == "load_firmware_blob" else None
        monkeypatch.setattr(drv, name, _recorder(calls, name, result))

    def _efuse(*_a):
        calls.append("read_and_parse")
        if efuse_error is not None:
            raise efuse_error
        return EfuseDefaults()

    monkeypatch.setattr(drv, "read_and_parse", _efuse)


async def test_cold_bring_up_runs_the_kernel_mac_phy_order(monkeypatch):
    calls: list[str] = []
    driver = _make_driver()
    _record(monkeypatch, calls, driver)

    assert await driver._cold_bring_up(lambda *a: None) is True
    assert calls == _EXPECTED_ORDER
    assert driver.current_channel is None


async def test_cold_bring_up_wraps_efuse_failure(monkeypatch):
    calls: list[str] = []
    driver = _make_driver()
    _record(monkeypatch, calls, driver, efuse_error=IOError("efuse boom"))

    with pytest.raises(BringUpError) as excinfo:
        await driver._cold_bring_up(lambda *a: None)
    assert excinfo.value.stage == "efuse"
    assert "efuse boom" in excinfo.value.detail


async def test_cold_bring_up_rejects_a_device_without_bulk_out(monkeypatch):
    calls: list[str] = []
    driver = _make_driver()
    _record(monkeypatch, calls, driver)
    monkeypatch.setattr(
        drv, "probe_endpoints",
        lambda dev: Endpoints(bulk_in=[0x84], bulk_out=[], interrupt=[]),
    )

    with pytest.raises(BringUpError) as excinfo:
        await driver._cold_bring_up(lambda *a: None)
    assert excinfo.value.stage == "endpoints"
