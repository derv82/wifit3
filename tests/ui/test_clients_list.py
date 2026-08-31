"""ClientsList._make_row: the device-class emoji gets its own fixed-width column left of the
MAC (never concatenated into it -- .cl-bssid is a fixed width: 17, exactly one MAC's worth of
columns, so an emoji+space prefix would overflow/truncate it), and the full label lands as a
tooltip rather than crowding the row. Clicking either label pops up FingerprintDetail."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.widgets import Label

from wifit3.ui.screens.focus_v2.clients_list import ClientsList, FingerprintDetail
from wifit3.wlan.fingerprint import Fingerprint

_MAC = "18:7f:88:aa:bb:cc"


def _click(widget, x=5, y=3) -> events.Click:
    return events.Click(widget, x, y, 0, 0, button=1, shift=False, meta=False, ctrl=False,
                        screen_x=x, screen_y=y)


class _DemoApp(App):
    def __init__(self, clients):
        super().__init__()
        self._clients = clients

    def compose(self) -> ComposeResult:
        yield ClientsList(self._clients, id="clients")


def _labels(row):
    """Widgets are unmounted here, so ``.children`` is empty; ``_pending_children`` holds what
    was passed to the constructor."""
    return [w for w in row._pending_children if isinstance(w, Label)]


_RING = Fingerprint("🔔", "Ring device", "high")


def test_row_with_fingerprint_has_its_own_column_and_tooltip():
    cl = ClientsList([])
    row = cl._make_row(_MAC, -50, 3, _RING)
    fp_label, mac_label = _labels(row)[0], _labels(row)[1]
    assert fp_label.has_class("cl-fp") and str(fp_label.content) == "🔔"
    assert fp_label.tooltip == "Ring device"
    assert mac_label.has_class("cl-bssid") and str(mac_label.content) == _MAC


def test_row_without_fingerprint_shows_a_blank_fp_column_no_tooltip():
    cl = ClientsList([])
    row = cl._make_row(_MAC, -50, 3, None)
    fp_label, mac_label = _labels(row)[0], _labels(row)[1]
    assert str(fp_label.content) == "" and fp_label.tooltip is None
    assert str(mac_label.content) == _MAC


def test_fingerprinted_labels_get_the_clickable_visual_marker():
    """A known fingerprint must look different from a plain label -- otherwise nothing signals
    that clicking it does anything."""
    cl = ClientsList([])
    row = cl._make_row(_MAC, -50, 3, _RING)
    fp_label, mac_label = _labels(row)[0], _labels(row)[1]
    assert fp_label.has_class("fp-known") and mac_label.has_class("fp-known")


def test_unfingerprinted_labels_get_no_clickable_marker():
    cl = ClientsList([])
    row = cl._make_row(_MAC, -50, 3, None)
    fp_label, mac_label = _labels(row)[0], _labels(row)[1]
    assert not fp_label.has_class("fp-known") and not mac_label.has_class("fp-known")


def test_compose_and_sync_thread_the_fingerprint_through():
    client = SimpleNamespace(bssid=_MAC, power=-50, packets=1, fingerprint=_RING)
    cl = ClientsList([client])
    rows = list(cl.compose())
    row_container = rows[1]                              # [0] is the "Deauth all" button
    first_row = row_container._pending_children[0]
    fp_label = _labels(first_row)[0]
    assert str(fp_label.content) == "🔔" and fp_label.tooltip == "Ring device"


# ----- fingerprint detail popup ---------------------------------------------

def test_fingerprinted_row_registers_both_labels_as_click_targets():
    cl = ClientsList([])
    row = cl._make_row(_MAC, -50, 3, _RING)
    fp_label, mac_label = _labels(row)[0], _labels(row)[1]
    assert cl._detail_targets[fp_label] == (_MAC, _RING)
    assert cl._detail_targets[mac_label] == (_MAC, _RING)


def test_unfingerprinted_row_registers_no_click_targets():
    cl = ClientsList([])
    cl._make_row(_MAC, -50, 3, None)
    assert cl._detail_targets == {}


@pytest.mark.asyncio
async def test_clicking_a_registered_label_pushes_the_detail_popup():
    app = _DemoApp([])
    async with app.run_test() as pilot:
        cl = app.query_one("#clients", ClientsList)
        row = cl._make_row(_MAC, -50, 3, _RING)
        fp_label = _labels(row)[0]
        await cl._rows_host().mount(row)
        app.push_screen = Mock()

        cl.on_click(_click(fp_label))
        await pilot.pause(0)

        app.push_screen.assert_called_once()
        pushed = app.push_screen.call_args.args[0]
        assert isinstance(pushed, FingerprintDetail)
        assert pushed._mac == _MAC and pushed._fp.label == "Ring device"


@pytest.mark.asyncio
async def test_clicking_an_unregistered_widget_is_a_noop():
    app = _DemoApp([])
    async with app.run_test():
        cl = app.query_one("#clients", ClientsList)
        cl._make_row(_MAC, -50, 3, _RING)
        app.push_screen = Mock()

        cl.on_click(_click(Label("unrelated")))

        app.push_screen.assert_not_called()


def test_removing_a_row_drops_its_click_targets():
    cl = ClientsList([])
    row = cl._make_row(_MAC, -50, 3, _RING)
    cl._rows_host = Mock(return_value=SimpleNamespace(mount=Mock()))
    # _remove_row queries the live DOM for the row; stand in a no-op query since nothing's mounted.
    cl.query_one = Mock(return_value=row)
    cl._remove_row(_MAC)
    assert cl._detail_targets == {}


def test_detail_popup_dismisses_on_backdrop_click_not_inner_content():
    popup = FingerprintDetail(_MAC, Fingerprint("🔔", "Ring device", "high"), offset=(1, 1))
    popup.dismiss = Mock()

    popup.on_click(_click(popup))              # the transparent backdrop itself
    popup.dismiss.assert_called_once()

    popup.dismiss.reset_mock()
    popup.on_click(_click(Label("inside the box")))    # some other widget (the box/labels)
    popup.dismiss.assert_not_called()


@pytest.mark.asyncio
async def test_detail_popup_clamps_to_stay_fully_on_screen():
    """A click near the screen edge used to position the box so its offset put part of it past
    the visible area -- offset is absolute from the box's own top-left, not screen-clamped."""
    app = _DemoApp([])
    async with app.run_test() as pilot:
        screen_w, screen_h = app.size
        popup = FingerprintDetail(_MAC, Fingerprint("🔔", "Ring device", "high"),
                                  offset=(screen_w - 1, screen_h - 1))
        await app.push_screen(popup)
        await pilot.pause()

        box = popup.query_one("#fp-box")
        x, y = box.styles.offset.x.value, box.styles.offset.y.value
        assert x + box.outer_size.width <= screen_w
        assert y + box.outer_size.height <= screen_h
