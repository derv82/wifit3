from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.widgets import Label

from wifit3.ui.screens.focus_v2.clients_list import ClientsList, ClientWidget, FingerprintModal
from wifit3.wlan.fingerprint import Fingerprint

_MAC = "18:7f:88:aa:bb:cc"
_RING = Fingerprint("🔔", "Ring device")


def _client(mac=_MAC, power=-50, packets=3, fingerprint=None):
    return SimpleNamespace(mac=mac, signal=power, packets=packets, fingerprint=fingerprint)


def _composed(client) -> ClientWidget:
    """A ClientWidget with compose run so its label refs exist (no mount needed)."""
    w = ClientWidget(client)
    list(w.compose())
    return w


def _click(widget, x=5, y=3) -> events.Click:
    return events.Click(widget, x, y, 0, 0, button=1, shift=False, meta=False, ctrl=False,
                        screen_x=x, screen_y=y)


class _DemoApp(App):
    def __init__(self, clients):
        super().__init__()
        self._clients = clients

    def compose(self) -> ComposeResult:
        yield ClientsList(self._clients, id="clients")


# ----- badge column + clickable marker --------------------------------------

def test_fingerprinted_row_has_own_badge_column_marker_and_tooltip():
    w = _composed(_client(fingerprint=_RING))
    assert w._fp_label.has_class("cl-fp") and str(w._fp_label.content) == "🔔"
    assert w._fp_label.tooltip == "Ring device"
    assert w._mac_label.has_class("cl-bssid") and str(w._mac_label.content) == _MAC
    assert w._fp_label.has_class("fp-known") and w._mac_label.has_class("fp-known")


def test_unfingerprinted_row_blank_badge_no_marker():
    w = _composed(_client(fingerprint=None))
    assert str(w._fp_label.content) == "" and w._fp_label.tooltip is None
    assert str(w._mac_label.content) == _MAC
    assert not w._fp_label.has_class("fp-known") and not w._mac_label.has_class("fp-known")


# ----- messages -------------------------------------------------------------

def test_deauth_button_posts_deauth_requested_with_mac():
    w = _composed(_client(fingerprint=_RING))
    w.post_message = Mock()
    event = Mock()
    w.on_button_pressed(event)
    event.stop.assert_called_once()
    msg = w.post_message.call_args.args[0]
    assert isinstance(msg, ClientWidget.DeauthRequested) and msg.mac == _MAC


def test_clicking_fingerprinted_badge_posts_fingerprint_clicked():
    w = _composed(_client(fingerprint=_RING))
    w.post_message = Mock()
    w.on_click(_click(w._fp_label))
    msg = w.post_message.call_args.args[0]
    assert isinstance(msg, ClientWidget.FingerprintClicked)
    assert msg.mac == _MAC and msg.fingerprint is _RING and msg.offset == (5, 3)


def test_clicking_the_mac_of_a_fingerprinted_row_also_posts():
    w = _composed(_client(fingerprint=_RING))
    w.post_message = Mock()
    w.on_click(_click(w._mac_label))
    assert isinstance(w.post_message.call_args.args[0], ClientWidget.FingerprintClicked)


def test_clicking_an_unfingerprinted_row_is_a_noop():
    w = _composed(_client(fingerprint=None))
    w.post_message = Mock()
    w.on_click(_click(w._fp_label))
    w.post_message.assert_not_called()


def test_clicking_a_non_target_child_is_a_noop():
    w = _composed(_client(fingerprint=_RING))
    w.post_message = Mock()
    w.on_click(_click(w._pwr_label))   # power/packets are not fingerprint click targets
    w.post_message.assert_not_called()


# ----- live sync ------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_adds_updates_in_place_and_drops_by_mac():
    a_mac, b_mac = "aa:aa:aa:00:00:01", "bb:bb:bb:00:00:02"
    app = _DemoApp([_client(a_mac, power=-40, packets=1)])
    async with app.run_test() as pilot:
        cl = app.query_one("#clients", ClientsList)
        assert set(cl._rows) == {a_mac}

        cl.sync([_client(a_mac, power=-55, packets=9), _client(b_mac, power=-60, packets=2)])
        await pilot.pause()
        assert set(cl._rows) == {a_mac, b_mac}
        row_a = cl._rows[a_mac]
        assert str(row_a._pwr_label.content) == "-55" and str(row_a._pkts_label.content) == "9"

        cl.sync([_client(b_mac, power=-60, packets=2)])
        await pilot.pause()
        assert set(cl._rows) == {b_mac}


# ----- detail popup ---------------------------------------------------------

def test_detail_popup_dismisses_on_backdrop_click_not_inner_content():
    popup = FingerprintModal(_MAC, _RING, offset=(1, 1))
    popup.dismiss = Mock()
    popup.on_click(_click(popup))                  # the transparent backdrop itself
    popup.dismiss.assert_called_once()
    popup.dismiss.reset_mock()
    popup.on_click(_click(Label("inside the box")))  # some inner widget
    popup.dismiss.assert_not_called()


@pytest.mark.asyncio
async def test_detail_popup_clamps_fully_on_screen():
    """The deferred clamp settles a frame after mount; a click at the edge must not leave the box
    hanging off-screen."""
    app = _DemoApp([])
    async with app.run_test() as pilot:
        w, h = app.size
        popup = FingerprintModal(_MAC, _RING, offset=(w - 1, h - 1))
        await app.push_screen(popup)
        box = popup.query_one("#fp-box")
        prev = None
        for _ in range(6):                          # settle until the offset stops moving
            await pilot.pause()
            cur = (box.styles.offset.x.value, box.styles.offset.y.value)
            if cur == prev:
                break
            prev = cur
        x, y = box.styles.offset.x.value, box.styles.offset.y.value
        assert x + box.outer_size.width <= w
        assert y + box.outer_size.height <= h
