"""Mid-session hotplug end-to-end: an arrival while on the Scanner prompts, and Yes brings the card
into the pool. The real WifiteApp / DeviceManager / prompter run; only wlan_iface is stubbed."""
from unittest.mock import AsyncMock

import pytest

import wifit3.device.manager as manager
from wifit3.chips.driver import DeviceID
from wifit3.errors import WifiteDeviceLostError
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.error_modals import RecoverableErrorModal
from wifit3.ui.screens.new_device import NewDeviceDialog
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
async def test_arrival_on_splash_updates_the_list_not_a_prompt():
    dev = DeviceID(0x148F, 0x5370, "RT5370 (test)")
    app = WifiteApp()
    async with app.run_test() as pilot:
        assert isinstance(app.screen, SplashView)
        app._on_devices_changed([dev], [dev], [])
        await pilot.pause(0)
        assert isinstance(app.screen, SplashView)     # no prompt on Splash
        labels = [str(i.query_one("Label").render()) for i in app.screen.query("ListView > ListItem")]
        assert any("RT5370 (test)" in text for text in labels)


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_reconnect_after_last_card_lost_dismisses_recovery_modal(monkeypatch):
    dev = DeviceID(0x0BDA, 0x8812, "RTL8812AU (test)")
    iface = _FakeIface(dev)
    monkeypatch.setattr(manager, "wlan_iface", lambda device_id, name="wlan0": iface)

    app = WifiteApp()
    async with app.run_test() as pilot:
        app.switch_screen("scanner")
        await pilot.pause(0)
        app.push_screen(RecoverableErrorModal(WifiteDeviceLostError("the wireless adapter")))
        await pilot.pause(0)
        assert isinstance(app.screen, RecoverableErrorModal)

        app._on_devices_changed([dev], [dev], [])
        for _ in range(20):
            await pilot.pause(0)
            if isinstance(app.screen, NewDeviceDialog):
                break
        assert not any(isinstance(s, RecoverableErrorModal) for s in app.screen_stack)
