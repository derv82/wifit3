"""ScannerView's eviction of APs not heard for EVICT_DURATION_S."""
import time

import pytest

from wifit3.models import AccessPoint
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.scanner import EVICT_DURATION_S
from wifit3.wlan.array import WlanArray


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_the_focused_ap_is_not_evicted_while_focus_is_open():
    app = WifiteApp()
    async with app.run_test() as pilot:
        array = WlanArray()
        focused = AccessPoint(bssid="aa:bb:cc:dd:ee:01", ssid="Focused", channel=6)
        other = AccessPoint(bssid="aa:bb:cc:dd:ee:02", ssid="Other", channel=6)
        array.access_points[focused.bssid] = focused
        array.access_points[other.bssid] = other
        app.array = array
        app.push_screen(app.scanner_view)
        await pilot.pause(0)
        scanner = app.screen
        app.target_ap = focused
        app.push_screen(app.focus_view)
        await pilot.pause(0.1)

        focused.last_seen = other.last_seen = time.time() - EVICT_DURATION_S - 1
        scanner._evict_expired_aps()
        assert set(array.access_points) == {focused.bssid}

        app.pop_screen()
        await pilot.pause(0.1)
        scanner._evict_expired_aps()
        assert array.access_points == {}
