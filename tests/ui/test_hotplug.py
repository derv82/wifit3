"""Mid-session hotplug end-to-end: an arrival while on the Scanner prompts, and Yes brings the card
into the pool. The real WifiteApp / BringupManager / prompter run; only build_interface is stubbed."""
from unittest.mock import AsyncMock

import pytest

import wifit3.wlan.bringup as bringup
from wifit3.chips.driver import DeviceID
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.new_device import NewDeviceDialog
from wifit3.ui.screens.scanner import ScannerView
from wifit3.ui.screens.splash import SplashView


class _FakeIface:
    """A hashable stand-in (the array keys _partition by member) with the connect + hop surface."""
    supported_channels = [1, 6, 11]
    current_channel = 1

    def __init__(self, dev):
        self.name, self.vid, self.pid = "wlan0", dev.vid, dev.pid
        self.bus, self.address = dev.bus, dev.address
        self.description = dev.description
        self.on_tx = None
        self.connect = AsyncMock(return_value=True)
        self.close = AsyncMock()

    @property
    def instance_key(self):
        return (self.vid, self.pid, self.bus, self.address)

    def register_rx_callback(self, cb):
        pass

    def register_disconnect_callback(self, cb):
        pass

    async def set_channel(self, ch, scan=False):
        return True

    async def start_hopping(self, channels=None, interval=0.5):
        pass

    async def stop_hopping(self):
        pass


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_hotplug_prompt_yes_pools_the_card(monkeypatch):
    dev = DeviceID(0x0BDA, 0x8812, "RTL8812AU (test)")
    iface = _FakeIface(dev)
    monkeypatch.setattr(bringup, "build_interface", lambda device_id, name="wlan0": iface)

    app = WifiteApp()
    async with app.run_test() as pilot:
        app.switch_screen("scanner")          # leave Splash so an arrival counts as a hotplug
        await pilot.pause()
        assert isinstance(app.screen, ScannerView)

        app._on_devices_changed([dev], [dev], [])
        for _ in range(20):
            await pilot.pause()
            if isinstance(app.screen, NewDeviceDialog):
                break
        assert isinstance(app.screen, NewDeviceDialog)

        app.screen.dismiss(True)          # user presses Yes (the button->dismiss wiring is in test_new_device)
        for _ in range(40):
            await pilot.pause()
            if app.array is not None and app.array.members:
                break
        assert app.array is not None and len(app.array.members) == 1
        assert iface.connect.await_count == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_re_arrival_of_pooled_card_does_not_prompt(monkeypatch):
    # A replug during bring-up re-enumerates the card and it re-arrives on the bus; since it's already
    # pooled, the listener must not prompt to bring it up again.
    dev = DeviceID(0x0BDA, 0x8812, "RTL8812AU (test)", bus=1, address=9)
    iface = _FakeIface(dev)
    monkeypatch.setattr(bringup, "build_interface", lambda device_id, name="wlan0": iface)

    app = WifiteApp()
    async with app.run_test() as pilot:
        app.switch_screen("scanner")
        await pilot.pause()
        app._on_devices_changed([dev], [dev], [])
        for _ in range(20):
            await pilot.pause()
            if isinstance(app.screen, NewDeviceDialog):
                break
        app.screen.dismiss(True)
        for _ in range(40):
            await pilot.pause()
            if app.array is not None and app.array.members:
                break
        assert app.array is not None and len(app.array.members) == 1

        app._on_devices_changed([dev], [dev], [])   # same instance re-arrives
        for _ in range(20):
            await pilot.pause()
        assert isinstance(app.screen, ScannerView)  # no second prompt
        assert len(app.array.members) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_arrival_on_splash_updates_the_list_not_a_prompt():
    dev = DeviceID(0x148F, 0x5370, "RT5370 (test)")
    app = WifiteApp()
    async with app.run_test() as pilot:
        assert isinstance(app.screen, SplashView)
        app._on_devices_changed([dev], [dev], [])
        await pilot.pause()
        assert isinstance(app.screen, SplashView)     # no prompt on Splash
        labels = [str(i.query_one("Label").render()) for i in app.screen.query("ListView > ListItem")]
        assert any("RT5370 (test)" in text for text in labels)
