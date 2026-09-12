"""Regression: the Scanner must not run its WPS PBC auto-invade while it's
suspended under another screen (Focus).

Textual's Screen.is_current is True for background screens too, so a suspended
Scanner reads is_current == True: the original guard never bailed and the Scanner
raced Focus's own PBC capture over the single radio (assoc rejected + EAPOL
timeout). The foreground gate must use screen-stack identity (app.screen is self).
"""

from textual.app import App
from textual.screen import Screen
from textual.widgets import Label

from wifit3.ui.screens.scanner import ScannerView
from wifit3.models import AccessPoint


class _Overlay(Screen):
    def compose(self):
        yield Label("overlay")


class _Host(App):
    def __init__(self):
        super().__init__()
        self.array = None
        self.pbc_enabled = True

    def on_mount(self):
        self.push_screen(ScannerView())


class _FakeIface:
    """Stubs app.array: get_access_points for _poll_pbc, start_hopping for on_screen_resume,
    members for the header's _ChannelReadout._poll timer."""
    members = []

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
