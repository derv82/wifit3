"""Regression: the Scanner must not run its WPS PBC auto-invade while it's
suspended under another screen (Focus).

Textual's Screen.is_current is True for background screens too, so a suspended
Scanner reads is_current == True: the original guard never bailed and the Scanner
raced Focus's own PBC capture over the single radio (assoc rejected + EAPOL
timeout). The foreground gate must use screen-stack identity (app.screen is self).
"""

import asyncio

import pytest
from textual.app import App
from textual.screen import Screen
from textual.widgets import Label

from wifit3.campaigns.campaign import Campaign
from wifit3.campaigns.pbc import WpsPbcCapture
from wifit3.campaigns.wps.registrar import AttemptOutcome, PinResult
from wifit3.ui.screens.scanner import ScannerView
from wifit3.models import AccessPoint
from wifit3.persist.vault import Vault


class _Overlay(Screen):
    def compose(self):
        yield Label("overlay")


class _Host(App):
    def __init__(self, array=None):
        super().__init__()
        self.array = array
        self.pbc_enabled = True
        self.vault = Vault()

    def on_mount(self):
        self.push_screen(ScannerView())


class _FakeRadio:
    def __init__(self):
        self.current_channel = 1
        self.supported_channels = [1, 6, 11]
        self.chipset = "test"


class _FakeArray:
    """The Scanner-facing array surface used by the PBC lifecycle tests."""

    def __init__(self, aps):
        self._aps = aps
        self.members = [_FakeRadio()]
        self.supported_channels = [1, 6, 11]
        self.access_points = {ap.bssid: ap for ap in aps}
        self.clients = {}
        self.forged_macs = set()

    def get_access_points(self, include_eviltwin=True):
        return self._aps

    def select_iface(self, channel):
        return self.members[0]

    async def start_hopping(self, channels=None, interval=0.25):
        pass

    async def stop_hopping(self):
        pass

    def remove_access_point(self, bssid):
        return self.access_points.pop(bssid, None)


def _pbc_window_ap():
    # wps_pbc_active = wps & wps_selected_registrar & device_password_id == 0x0004
    return AccessPoint(
        bssid="aa:bb:cc:11:22:33", ssid="TestNet", channel=1,
        wps=True, wps_selected_registrar=True, wps_device_password_id=0x0004,
    )


@pytest.mark.asyncio
async def test_poll_ignores_pbc_windows_while_scanner_is_suspended():
    ap = _pbc_window_ap()
    app = _Host(_FakeArray([ap]))
    async with app.run_test() as pilot:
        await pilot.pause(0)
        scanner = app.screen
        seen = []
        scanner._on_pbc_window = seen.append
        app.push_screen(_Overlay())
        await pilot.pause(0)
        scanner._poll_pbc()
        assert seen == []


@pytest.mark.asyncio
async def test_pbc_reserves_campaign_slot_before_second_window(monkeypatch):
    first = _pbc_window_ap()
    second = AccessPoint(
        bssid="aa:bb:cc:11:22:44", ssid="SecondNet", channel=6,
        wps=True, wps_selected_registrar=True, wps_device_password_id=0x0004,
    )
    started = asyncio.Event()

    async def wait_for_stop(self):
        started.set()
        while not self.stopped:
            await asyncio.sleep(0)
        self.outcome = AttemptOutcome(PinResult.ABORTED, "<PBC>", detail="stopped")

    monkeypatch.setattr(WpsPbcCapture, "_loop", wait_for_stop)
    app = _Host(_FakeArray([first, second]))
    async with app.run_test() as pilot:
        await pilot.pause(0)
        scanner = app.screen
        scanner._on_pbc_window(first)
        scanner._on_pbc_window(second)
        await started.wait()
        assert scanner._pbc_campaign.target is first
        assert Campaign.active is scanner._pbc_campaign
        await scanner._stop_pbc()
        assert Campaign.active is None
        assert scanner._pbc_campaign is None
