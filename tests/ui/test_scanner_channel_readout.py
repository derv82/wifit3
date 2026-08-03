"""The scanner header's right slot shows the live hopped channel(s), padded so hops
don't resize it, instead of a wall clock, and it renders without needing a window resize."""
import pytest
import pytest_asyncio
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


@pytest.mark.asyncio
async def test_single_card_channel_is_visible_on_first_paint():
    # Own boot: this asserts the slot is sized at mount (before any resize), which a
    # re-poll on a shared header can't reproduce.
    app = _Host(_Array([1]))
    async with app.run_test() as pilot:
        await pilot.pause(0)
        readout = app.query_one(_ChannelReadout)
        assert readout.channels == "CH:  1"
        assert readout.region.width > 0        # sized without a resize


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def header_host():
    """One header boot shared by the string-content tests; each swaps app.array and
    re-polls (the same 0.25s poll the live header runs), no re-compose."""
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause(0)
        yield app, pilot


async def _channels_for(host, array):
    app, pilot = host
    app.array = array
    readout = app.query_one(_ChannelReadout)
    readout._poll()
    await pilot.pause(0)
    return readout.channels


@pytest.mark.asyncio(loop_scope="module")
async def test_multi_card_joined_and_width_padded(header_host):
    # width-3 channel field keeps every token 6 cells wide (no jarring on 9->10, 44->149).
    assert await _channels_for(header_host, _Array([9, 149])) == "CH:  9 | CH:149"


@pytest.mark.asyncio(loop_scope="module")
async def test_no_array_is_blank(header_host):
    assert await _channels_for(header_host, None) == ""
