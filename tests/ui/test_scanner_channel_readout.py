"""The scanner header's right slot shows the live hopped channel(s), padded so hops
don't resize it, instead of a wall clock, and it renders without needing a window resize."""
from textual.app import App, ComposeResult

from wifit3.ui.screens.scanner import _ChannelReadout, _ScannerHeader


class _Iface:
    def __init__(self, ch):
        self.current_channel = ch


class _Array:
    def __init__(self, chans):
        self.members = [_Iface(c) for c in chans]


class _Host(App):
    def __init__(self, array=None):
        super().__init__()
        self.array = array

    def compose(self) -> ComposeResult:
        yield _ScannerHeader()


async def test_single_card_channel_is_visible_on_first_paint():
    app = _Host(_Array([1]))
    async with app.run_test() as pilot:
        await pilot.pause()
        readout = app.query_one(_ChannelReadout)
        assert readout.channels == "CH:  1"
        assert readout.region.width > 0        # sized without a resize


async def test_multi_card_joined_and_width_padded():
    app = _Host(_Array([9, 149]))
    async with app.run_test() as pilot:
        await pilot.pause()
        readout = app.query_one(_ChannelReadout)
        # width-3 channel field keeps every token 6 cells wide (no jarring on 9->10, 44->149).
        assert readout.channels == "CH:  9 | CH:149"


async def test_no_array_is_blank():
    app = _Host(None)
    async with app.run_test() as pilot:
        await pilot.pause()
        readout = app.query_one(_ChannelReadout)
        assert readout.channels == ""
