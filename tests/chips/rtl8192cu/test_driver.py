"""RTL8192CUDriver: connect claims, reads endpoints from libusb, then runs probe and hw_init."""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from wifit3.chips.rtl8192cu import SUPPORTED_IDS
from wifit3.chips.rtl8192cu import constants as C
from wifit3.chips.rtl8192cu.driver import RTL8192CUDriver
from wifit3.chips.rtl8192cu.priv import RtlPriv
from wifit3.errors import BringUpError

from .fake import FAKE_MAC, NORMAL_SIE_EP_2OUT, SYS_CFG_88C_TSMC, FakeChip, probed_efuse


async def test_connect_runs_probe_and_hw_init_then_stops(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    entry = next(d for d in SUPPORTED_IDS if (d.vid, d.pid) == (0x0BDA, 0x8176))
    driver = RTL8192CUDriver.from_usb_device(MagicMock(), entry)
    io = FakeChip({C.REG_SYS_CFG: SYS_CFG_88C_TSMC, C.REG_9346CR: C.EEPROM_EN, **NORMAL_SIE_EP_2OUT},
                  probed_efuse())
    driver.priv = RtlPriv(io)
    endpoints = [SimpleNamespace(bEndpointAddress=a, bmAttributes=t)
                 for a, t in ((0x81, 2), (0x02, 2), (0x03, 2), (0x84, 3))]
    driver.transport = MagicMock(interface=endpoints, **{"is_high_speed.return_value": True})

    with pytest.raises(BringUpError, match="rtl_usb_start"):
        await driver.connect()

    driver.transport.claim.assert_called_once()
    driver.transport.release.assert_called_once()
    assert driver.mac_address == FAKE_MAC.hex(":")
    assert driver.priv.usb.out_eps[:2] == [2, 3]
    assert driver.priv.rtlhal.fw_ready
