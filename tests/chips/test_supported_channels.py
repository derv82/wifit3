"""Every driver's SUPPORTED_CHANNELS covers the shared Driver.CHANNELS_* lists."""
import importlib
import pkgutil

import pytest

from wifit3 import chips
from wifit3.chips.driver import Driver
from wifit3.chips.mt76x0u.driver import MT76x0UDriver
from wifit3.chips.mt76x2u.driver import MT76x2UDriver
from wifit3.chips.rt2800usb.driver import RT2800USBDriver
from wifit3.chips.rt5572.driver import RT5572Driver

DRIVERS_WITHOUT_DFS = {MT76x0UDriver, MT76x2UDriver, RT2800USBDriver}


def _driver_classes() -> list[type[Driver]]:
    classes = []
    for mod_info in pkgutil.iter_modules(chips.__path__, chips.__name__ + "."):
        if not mod_info.ispkg:
            continue
        mod = importlib.import_module(mod_info.name)
        if hasattr(mod, "import_driver"):
            classes.append(mod.import_driver())
    return classes


@pytest.mark.parametrize("driver_class", _driver_classes(), ids=lambda c: c.__name__)
def test_every_driver_supports_2g_channels(driver_class):
    assert set(Driver.CHANNELS_2G) <= set(driver_class.SUPPORTED_CHANNELS)


@pytest.mark.parametrize("driver_class", _driver_classes(), ids=lambda c: c.__name__)
def test_every_5g_driver_supports_5g_and_dfs_channels(driver_class):
    supported = set(driver_class.SUPPORTED_CHANNELS)
    if max(supported) <= 14:
        pytest.skip("2.4 GHz only")
    expected_dfs = set(Driver.CHANNELS_DFS)
    if driver_class in DRIVERS_WITHOUT_DFS:
        expected_dfs = set()
    if driver_class is RT5572Driver:
        expected_dfs.discard(144)
    assert set(Driver.CHANNELS_5G) | expected_dfs <= supported
