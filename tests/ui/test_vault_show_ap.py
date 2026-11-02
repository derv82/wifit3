"""The Vault's Show AP button: shown while the AP is in the array, and lands on Focus on that AP."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from wifit3.campaigns.campaign import Campaign
from wifit3.models import AccessPoint
from wifit3.ui.screens.vault_drawer import VaultDrawer
from wifit3.ui.screens.vault_item import ShowApPressed, _ApStatePanel

BSSID = "aa:bb:cc:dd:ee:01"


class _App:
    """The screen stack as Textual mutates it: synchronously on push and pop."""

    def __init__(self, *stack, array) -> None:
        self.screen_stack = list(stack)
        self.array = array
        self.target_ap = None
        self.focus = SimpleNamespace(name="focus")

    @property
    def screen(self):
        return self.screen_stack[-1]

    def get_screen(self, name):
        return self.focus

    def pop_screen(self) -> None:
        self.screen_stack.pop()

    def push_screen(self, screen) -> None:
        self.screen_stack.append(screen)


def _drawer(app: _App):
    drawer = SimpleNamespace(app=app, notify=MagicMock())
    drawer.dismiss = app.pop_screen
    app.push_screen(drawer)
    return drawer


def _array_with_ap():
    ap = AccessPoint(bssid=BSSID, ssid="TestNet", channel=6)
    return ap, SimpleNamespace(access_points={BSSID: ap})


def test_show_ap_over_scanner_pushes_focus_on_the_ap():
    ap, array = _array_with_ap()
    scanner = SimpleNamespace(name="scanner")
    app = _App(scanner, array=array)

    VaultDrawer._show_ap_pressed(_drawer(app), ShowApPressed(BSSID))

    assert app.screen_stack == [scanner, app.focus]
    assert app.target_ap is ap


def test_show_ap_with_focus_on_the_stack_goes_back_down_to_it():
    ap, array = _array_with_ap()
    scanner, modal = SimpleNamespace(name="scanner"), SimpleNamespace(name="modal")
    app = _App(scanner, None, modal, array=array)
    app.screen_stack[1] = app.focus

    VaultDrawer._show_ap_pressed(_drawer(app), ShowApPressed(BSSID))

    assert app.screen_stack == [scanner, app.focus]
    assert app.target_ap is ap


def test_show_ap_for_an_ap_no_longer_in_the_array_stays_in_the_vault():
    scanner = SimpleNamespace(name="scanner")
    app = _App(scanner, array=SimpleNamespace(access_points={}))
    drawer = _drawer(app)

    VaultDrawer._show_ap_pressed(drawer, ShowApPressed(BSSID))

    assert app.screen_stack == [scanner, drawer]
    assert app.target_ap is None
    drawer.notify.assert_called_once_with("AP not in range")


def _panel(array, line_count: int):
    lines = [SimpleNamespace(show_ap_button=SimpleNamespace(display=None, disabled=None, tooltip=None))
             for _ in range(line_count)]
    return SimpleNamespace(app=SimpleNamespace(array=array), _state=SimpleNamespace(bssid=BSSID),
                           _ssid_lines=lines)


def test_show_ap_shows_on_the_first_ssid_line_only_while_the_ap_is_in_the_array():
    _, array = _array_with_ap()
    panel = _panel(array, 2)

    _ApStatePanel._refresh_show_ap_button(panel)
    assert [line.show_ap_button.display for line in panel._ssid_lines] == [True, False]

    array.access_points.clear()
    _ApStatePanel._refresh_show_ap_button(panel)
    assert [line.show_ap_button.display for line in panel._ssid_lines] == [False, False]

    panel.app.array = None
    _ApStatePanel._refresh_show_ap_button(panel)
    assert [line.show_ap_button.display for line in panel._ssid_lines] == [False, False]


def test_show_ap_is_disabled_while_a_campaign_is_active():
    _, array = _array_with_ap()
    panel = _panel(array, 1)
    Campaign.active = SimpleNamespace(key="wps_pin")

    _ApStatePanel._refresh_show_ap_button(panel)
    button = panel._ssid_lines[0].show_ap_button
    assert (button.disabled, button.tooltip) == (True, "Disabled; wps_pin campaign is active")

    Campaign.active = None
    _ApStatePanel._refresh_show_ap_button(panel)
    assert (button.disabled, button.tooltip) == (False, "Focus this AP")


def test_show_ap_press_posts_show_ap_pressed_for_the_ap_in_view():
    _, array = _array_with_ap()
    panel = _panel(array, 2)
    panel.add_key_button = object()
    panel.post_message = MagicMock()
    event = SimpleNamespace(button=panel._ssid_lines[0].show_ap_button, stop=MagicMock())

    _ApStatePanel._button_pressed(panel, event)

    assert panel.post_message.call_args.args[0].bssid == BSSID
    event.stop.assert_called_once()
