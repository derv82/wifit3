from types import SimpleNamespace
from unittest.mock import patch

import pytest

from wifit3.chips.ar9271_v2.driver import AR9271V2Driver


@pytest.mark.asyncio
async def test_reenumeration_selects_same_physical_port_with_identical_sibling():
    original = SimpleNamespace(bus=1, address=7, port_numbers=(6,))
    sibling = SimpleNamespace(bus=1, address=6, port_numbers=(1,))
    rebooted = SimpleNamespace(bus=1, address=8, port_numbers=(6,))
    driver = AR9271V2Driver(original)

    with (patch("wifit3.chips.ar9271_v2.driver.asyncio.sleep"),
          patch("wifit3.chips.ar9271_v2.driver.usb.core.find",
                return_value=[sibling, rebooted])):
        found = await driver._await_reenumeration(original)

    assert found is rebooted


@pytest.mark.asyncio
async def test_reenumeration_allows_linux_to_retain_address():
    original = SimpleNamespace(bus=1, address=7, port_numbers=(6,))
    driver = AR9271V2Driver(original)

    with (patch("wifit3.chips.ar9271_v2.driver.asyncio.sleep"),
          patch("wifit3.chips.ar9271_v2.driver.usb.core.find", return_value=[original])):
        found = await driver._await_reenumeration(original)

    assert found is original


@pytest.mark.asyncio
async def test_reenumeration_without_ports_keeps_same_address():
    original = SimpleNamespace(bus=1, address=7, port_numbers=None)
    sibling = SimpleNamespace(bus=1, address=6, port_numbers=None)
    driver = AR9271V2Driver(original)

    with (patch("wifit3.chips.ar9271_v2.driver.asyncio.sleep"),
          patch("wifit3.chips.ar9271_v2.driver.usb.core.find",
                return_value=[sibling, original])):
        found = await driver._await_reenumeration(original)

    assert found is original


@pytest.mark.asyncio
async def test_reenumeration_without_ports_never_selects_sibling():
    original = SimpleNamespace(bus=1, address=7, port_numbers=None)
    sibling = SimpleNamespace(bus=1, address=6, port_numbers=None)
    driver = AR9271V2Driver(original)

    with (patch("wifit3.chips.ar9271_v2.driver.asyncio.sleep"),
          patch("wifit3.chips.ar9271_v2.driver.usb.core.find", return_value=[sibling])):
        found = await driver._await_reenumeration(original)

    assert found is None


@pytest.mark.asyncio
async def test_reenumeration_without_ports_allows_known_single_card_address_change():
    original = SimpleNamespace(bus=1, address=7, port_numbers=None)
    rebooted = SimpleNamespace(bus=1, address=8, port_numbers=None)
    driver = AR9271V2Driver(original)

    with (patch("wifit3.chips.ar9271_v2.driver.asyncio.sleep"),
          patch("wifit3.chips.ar9271_v2.driver.usb.core.find", return_value=[rebooted])):
        found = await driver._await_reenumeration(original, unique_vidpid=True)

    assert found is rebooted
