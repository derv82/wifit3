"""Splash START drives the engine end-to-end for the happy path: a connectable card is pooled into
the array and the scanner is requested. The real WifiteApp / BringupManager / BringupPrompter (the
progress modal really opens and closes) run; only build_interface is stubbed."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import wifit3.wlan.bringup as bringup
from wifit3.chips.driver import DeviceID
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.splash import SplashView


def _fake_iface():
    return SimpleNamespace(
        name="wlan0", description="RT5372 (test)", vid=0x148F, pid=0x5372,
        bus=1, address=1, instance_key=(0x148F, 0x5372, 1, 1),
        supported_channels=[1, 6, 11], on_tx=None,
        register_rx_callback=lambda cb: None, register_disconnect_callback=lambda cb: None,
        connect=AsyncMock(return_value=True), close=AsyncMock())


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_start_pools_card_and_requests_scanner(monkeypatch):
    iface = _fake_iface()
    monkeypatch.setattr(bringup, "build_interface", lambda device_id, name="wlan0": iface)
    monkeypatch.setattr(bringup, "find_devices", lambda: [])   # no other cards to pool
    dev = DeviceID(0x148F, 0x5372, "RT5372 (test)")

    app = WifiteApp()
    async with app.run_test() as pilot:
        splash = app.screen
        assert isinstance(splash, SplashView)
        switched = []
        monkeypatch.setattr(app, "switch_screen", lambda name: switched.append(name))

        splash.perform_start(dev)
        for _ in range(40):
            await pilot.pause()
            if switched:
                break

        assert switched == ["scanner"]
        assert app.array is not None and len(app.array.members) == 1
        assert iface.connect.await_count == 1
