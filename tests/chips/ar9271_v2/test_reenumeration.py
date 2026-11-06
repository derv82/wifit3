from types import SimpleNamespace
from unittest.mock import patch

import pytest

from wifit3.chips.ar9271_v2 import constants as C
from wifit3.chips.ar9271_v2.driver import AR9271V2Driver
from tests.device.fakes import FakeBackend


def _card(address, port_numbers):
    return SimpleNamespace(bus=1, address=address, port_numbers=port_numbers,
                           idVendor=C.AR9271_VID, idProduct=C.AR9271_PID)


@pytest.mark.asyncio
async def test_reenumeration_selects_same_physical_port_with_identical_sibling():
    original = _card(7, (6,))
    sibling = _card(6, (1,))
    rebooted = _card(8, (6,))
    driver = AR9271V2Driver(original)

    with (patch("wifit3.chips.ar9271_v2.driver.asyncio.sleep"),
          patch("wifit3.chips.ar9271_v2.driver.usb_backend",
                return_value=FakeBackend([sibling, rebooted]))):
        found = await driver._await_reenumeration(original)

    assert found is rebooted


@pytest.mark.asyncio
async def test_reenumeration_allows_linux_to_retain_address():
    original = _card(7, (6,))
    driver = AR9271V2Driver(original)

    with (patch("wifit3.chips.ar9271_v2.driver.asyncio.sleep"),
          patch("wifit3.chips.ar9271_v2.driver.usb_backend", return_value=FakeBackend([original]))):
        found = await driver._await_reenumeration(original)

    assert found is original


@pytest.mark.asyncio
async def test_reenumeration_without_ports_keeps_same_address():
    original = _card(7, None)
    sibling = _card(6, None)
    driver = AR9271V2Driver(original)

    with (patch("wifit3.chips.ar9271_v2.driver.asyncio.sleep"),
          patch("wifit3.chips.ar9271_v2.driver.usb_backend",
                return_value=FakeBackend([sibling, original]))):
        found = await driver._await_reenumeration(original)

    assert found is original


@pytest.mark.asyncio
async def test_reenumeration_without_ports_never_selects_sibling():
    original = _card(7, None)
    sibling = _card(6, None)
    driver = AR9271V2Driver(original)

    with (patch("wifit3.chips.ar9271_v2.driver.asyncio.sleep"),
          patch("wifit3.chips.ar9271_v2.driver.usb_backend", return_value=FakeBackend([sibling]))):
        found = await driver._await_reenumeration(original)

    assert found is None


@pytest.mark.asyncio
async def test_reenumeration_without_ports_allows_known_single_card_address_change():
    original = _card(7, None)
    rebooted = _card(8, None)
    driver = AR9271V2Driver(original)

    with (patch("wifit3.chips.ar9271_v2.driver.asyncio.sleep"),
          patch("wifit3.chips.ar9271_v2.driver.usb_backend", return_value=FakeBackend([rebooted]))):
        found = await driver._await_reenumeration(original, unique_vidpid=True)

    assert found is rebooted
