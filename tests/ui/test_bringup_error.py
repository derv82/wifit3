"""Bring-up failures must reach the user, not vanish into a False return.

End-to-end for ONE driver (RTL8187L): a USB fault during init becomes a `BringUpError`,
and the splash surfaces it as a persistent error label + an error toast that the next USB
poll does not wipe. The rest of the fleet shares the same `BringUpError` contract, so we
lock the behaviour once here rather than per-driver.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import usb.core
from textual.widgets import Label

from wifit3.chips.rtl8187.driver import RTL8187Driver
from wifit3.errors import BringUpError
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.splash import SplashView


@pytest.mark.asyncio
async def test_driver_init_io_failure_becomes_bringuperror(monkeypatch):
    """A USB error on the first init op (the interface claim) surfaces as BringUpError —
    not a silent log + return False."""
    driver = RTL8187Driver(Mock())
    monkeypatch.setattr(driver, "_claim",
                        Mock(side_effect=usb.core.USBError("simulated control-transfer failure")))

    with pytest.raises(BringUpError):
        await driver.connect()


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")  # ui/conftest.py — boot touches no hardware
async def test_splash_surfaces_driver_bringup_failure(monkeypatch):
    """A real driver failing bring-up (USB fault during init) must show in the splash's
    persistent error label + an error toast — and a later USB poll must NOT overwrite it
    (the status line gets overwritten ~2x/s; the error label must survive)."""
    driver = RTL8187Driver(Mock())
    monkeypatch.setattr(driver, "_claim",
                        Mock(side_effect=usb.core.USBError("simulated init failure")))

    # Minimal interface whose connect() IS the real driver's, so the failure travels the true
    # path: driver init -> BringUpError -> splash. close() is the partial-claim cleanup.
    iface = SimpleNamespace(
        name="rtl8187", description="RTL8187L (test)", vid=0x0BDA, pid=0x8187,
        dev=None, connect=driver.connect, close=AsyncMock())

    app = WifiteApp()
    async with app.run_test() as pilot:
        splash = app.screen
        assert isinstance(splash, SplashView)

        toasts: list[tuple] = []
        monkeypatch.setattr(splash, "notify", lambda *a, **k: toasts.append((a, k)))

        splash.perform_start(iface)            # @work — pump the loop until it surfaces
        label = splash.query_one("#error-label", Label)
        for _ in range(20):
            await pilot.pause()
            if label.display:
                break

        assert label.display is True, "error label should be visible after a bring-up failure"

        assert toasts, "a bring-up failure should raise a toast"
        args, kwargs = toasts[-1]
        assert kwargs.get("severity") == "error"
        # The splash trims the " (...)" suffix off the description, so the toast names the bare
        # chipset ("RTL8187L"), not the full "RTL8187L (test)".
        assert "RTL8187L" in args[0] and "(test)" not in args[0] and "bring-up failed" in args[0]

        # The core fix: force a full poll tick (it rewrites the status line) and confirm it
        # leaves the error label alone.
        splash._last_signature = None
        await splash.poll_usb()
        assert splash.query_one("#error-label", Label).display is True
