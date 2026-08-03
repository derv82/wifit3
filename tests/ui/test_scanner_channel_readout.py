"""The scanner header's right slot shows the live hopped channel(s), padded so hops
don't resize it, instead of a wall clock, and it renders without needing a window resize."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from textual.app import App, ComposeResult

from wifit3.ui.screens.scanner import _ChannelReadout, _ScannerHeader


def _array(*channels):
    return SimpleNamespace(members=[SimpleNamespace(current_channel=c) for c in channels])


class _Host(App):
    def __init__(self, array=None):
        super().__init__()
        self.array = array

    def compose(self) -> ComposeResult:
        yield _ScannerHeader()


@pytest.mark.asyncio
async def test_single_card_channel_is_visible_on_first_paint():
    # Own boot: the slot sizes at first paint; a shared header is already past it.
    app = _Host(_array(1))
    async with app.run_test() as pilot:
        await pilot.pause(0)
        readout = app.query_one(_ChannelReadout)
        assert readout.channels == "CH:  1"
        assert readout.region.width > 0


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def channel_readout():
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause(0)
        yield app.query_one(_ChannelReadout)


@pytest.mark.asyncio(loop_scope="module")
async def test_multi_card_joined_and_width_padded(channel_readout):
    channel_readout.app.array = _array(9, 149)
    channel_readout._poll()
    assert channel_readout.channels == "CH:  9 | CH:149"


@pytest.mark.asyncio(loop_scope="module")
async def test_no_array_is_blank(channel_readout):
    channel_readout.app.array = None
    channel_readout._poll()
    assert channel_readout.channels == ""
