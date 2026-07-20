"""Regression: the Scanner must not run its WPS PBC auto-invade while it's
suspended under another screen (Focus).

Textual's Screen.is_current is True for background screens too, so a suspended
Scanner reads is_current == True — the original guard never bailed and the Scanner
raced Focus's own PBC capture over the single radio (assoc rejected + EAPOL
timeout). The foreground gate must use screen-stack identity (app.screen is self).
"""

import pytest
from unittest.mock import Mock
from textual.screen import Screen
from textual.widgets import Label

from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.scanner import ScannerView
from wifit3.models import AccessPoint


class _Overlay(Screen):
    def compose(self):
        yield Label("overlay")


class _FakeIface:
    """_poll_pbc only needs get_access_points; on_screen_resume needs start_hopping."""
    def __init__(self, aps):
        self._aps = aps

    def get_access_points(self):
        return self._aps

    async def start_hopping(self, channels=None, interval=0.25):
        pass


def _pbc_window_ap():
    # wps_pbc_active = wps & wps_selected_registrar & device_password_id == 0x0004
    return AccessPoint(
        bssid="aa:bb:cc:11:22:33", ssid="TestNet", channel=1,
        wps=True, wps_selected_registrar=True, wps_device_password_id=0x0004,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_pbc_poll_bails_while_suspended_then_acts_when_foreground():
    app = WifiteApp()
    async with app.run_test() as pilot:
        app.push_screen("scanner")
        await pilot.pause()
        scanner = app.screen
        assert isinstance(scanner, ScannerView)

        ap = _pbc_window_ap()
        assert ap.wps_pbc_active and not ap.has_psk
        app.active_interface = _FakeIface([ap])
        scanner._on_pbc_window = Mock()      # spy: did we try to act on a window?

        # Suspended under an overlay: must NOT act (and must not consume the edge).
        app.push_screen(_Overlay())
        await pilot.pause()
        assert app.screen is not scanner     # genuinely suspended …
        assert scanner.is_current            # … yet is_current is still True — the trap
        scanner._poll_pbc()
        assert not scanner._on_pbc_window.called

        # Foreground again: the still-open window is acted on (edge survived).
        app.pop_screen()
        await pilot.pause()
        assert app.screen is scanner
        scanner._poll_pbc()
        scanner._on_pbc_window.assert_called_once()
        assert scanner._on_pbc_window.call_args.args[0] is ap
