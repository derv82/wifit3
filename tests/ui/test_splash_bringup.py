"""Splash START drives the engine end-to-end for the happy path: a connectable card is pooled into
the array and the scanner is requested. The real WifiteApp / BringupManager / BringupPrompter (the
progress modal really opens and closes) run; only build_interface is stubbed."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from textual.widgets import SelectionList

import wifit3.wlan.bringup as bringup
from wifit3.chips.driver import DeviceID
from wifit3.setup.base import SetupResult
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.splash import SplashView
from wifit3.wlan.bringup import BringupResult


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
    dev = DeviceID(0x148F, 0x5372, "RT5372 (test)")

    app = WifiteApp()
    async with app.run_test() as pilot:
        splash = app.screen
        assert isinstance(splash, SplashView)
        switched = []
        monkeypatch.setattr(app, "switch_screen", lambda name: switched.append(name))

        splash.perform_start([dev])
        for _ in range(40):
            await pilot.pause()
            if switched:
                break

        assert switched == ["scanner"]
        assert app.array is not None and len(app.array.members) == 1
        assert iface.connect.await_count == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_multi_card_start_brings_up_only_checked(monkeypatch):
    # 2+ cards -> checkbox list, all checked by default. Unchecking one and pressing START must bring
    # up only the checked card, one run() per card (no silent auto-pool of the rest).
    devA = DeviceID(0x0E8D, 0x7961, "MT7921AU", bus=2, address=32)
    devB = DeviceID(0x0E8D, 0x7961, "MT7921AU", bus=2, address=35)

    app = WifiteApp()
    async with app.run_test() as pilot:
        splash = app.screen
        assert isinstance(splash, SplashView)

        splash.render_devices([devA, devB])
        await pilot.pause()
        sl = splash.query_one("#device-select", SelectionList)
        assert sl.display is True and sorted(sl.selected) == [0, 1]

        sl.deselect(1)                       # uncheck devB
        await pilot.pause()
        assert sl.selected == [0]

        ran = []

        async def _fake_run(device_id, **kw):
            ran.append(device_id)
            return BringupResult.ready()

        monkeypatch.setattr(app.bringup, "run", _fake_run)
        monkeypatch.setattr(app, "switch_screen", lambda name: None)

        splash.action_start()
        for _ in range(40):
            await pilot.pause()
            if ran:
                break

        assert ran == [devA]                 # only the checked card, devB skipped


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_enter_uninstalls_when_uninstall_button_focused(monkeypatch):
    # Enter is focus-aware: with the Uninstall button focused it uninstalls the highlighted card,
    # not starts. (Elsewhere Enter starts.)
    dev = DeviceID(0x148F, 0x5372, "RT5372 (test)")
    app = WifiteApp()
    async with app.run_test() as pilot:
        splash = app.screen
        splash.render_devices([dev])         # single card -> ListView, highlighted
        await pilot.pause()

        uninstalled, started = [], []

        async def _fake_uninstall(device_id):
            uninstalled.append(device_id)
            return SetupResult(ok=True, message="removed")

        async def _fake_run(device_id, **kw):
            started.append(device_id)
            return BringupResult.ready()

        monkeypatch.setattr(app.bringup, "uninstall", _fake_uninstall)
        monkeypatch.setattr(app.bringup, "run", _fake_run)

        splash.query_one("#uninstall-btn").focus()
        await pilot.pause()
        splash.action_enter()
        for _ in range(40):
            await pilot.pause()
            if uninstalled:
                break

        assert uninstalled == [dev] and started == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_single_click_highlights_double_click_starts(monkeypatch):
    # Single-card view: a single click only highlights (no start); a double click starts it, the same
    # as Enter / START.
    dev = DeviceID(0x148F, 0x5372, "RT5372 (test)")
    app = WifiteApp()
    started = []

    async def _fake_run(device_id, **kw):
        started.append(device_id)
        return BringupResult.ready()

    async with app.run_test(size=(120, 40)) as pilot:
        app.bringup.run = _fake_run
        monkeypatch.setattr(app, "switch_screen", lambda name: started.append(("switch", name)))
        splash = app.screen
        splash.render_devices([dev])
        await pilot.pause()

        await pilot.click("#device-list ListItem")            # single click
        await pilot.pause()
        assert started == []                                  # highlight only, no start

        await pilot.click("#device-list ListItem", times=2)   # double click
        for _ in range(40):
            await pilot.pause()
            if started:
                break
        assert dev in started                                 # started, like Enter / START
