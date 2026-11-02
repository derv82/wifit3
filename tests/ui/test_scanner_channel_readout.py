"""The scanner header's right slot shows the live hopped channel(s), padded so hops
don't resize it, instead of a wall clock, and it renders without needing a window resize."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import pytest_asyncio
from textual.app import App, ComposeResult
from textual.widgets._header import HeaderTitle

from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.scanner import _ChannelBar, _ChannelReadout, _ScannerHeader


def _array(*channels):
    return SimpleNamespace(members=[SimpleNamespace(current_channel=c) for c in channels])


class _Host(App):
    CSS = WifiteApp.CSS

    def __init__(self, array=None):
        super().__init__()
        self.array = array

    def compose(self) -> ComposeResult:
        yield _ScannerHeader()
        yield _ChannelBar()


@pytest.mark.asyncio
async def test_single_card_channel_is_visible_on_first_paint():
    # Own boot: the slot sizes at first paint; a shared header is already past it.
    app = _Host(_array(1))
    async with app.run_test() as pilot:
        await pilot.pause(0)
        readout = app.query_one(_ChannelReadout)
        assert readout.channels == "CH:  1"
        assert readout.region.width > 0


@pytest.mark.asyncio
async def test_title_is_centered_independently_of_channel_readout():
    app = _Host(_array(1, 149, 165))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0)
        title = app.query_one(HeaderTitle)
        readout = app.query_one(_ChannelReadout)
        assert title.region.x == 0
        assert title.region.width == app.size.width
        assert readout.region.right == app.size.width
        assert readout.display is True
        assert app.query_one(_ScannerHeader).region.height == 1
        assert app.query_one(_ChannelBar).display is False


@pytest.mark.asyncio
async def test_channels_move_when_three_spaces_no_longer_fit():
    app = _Host(_array(1))
    async with app.run_test(size=(21, 24)) as pilot:
        await pilot.pause(0)
        header = app.query_one(_ScannerHeader)
        readout = app.query_one(_ChannelReadout)
        channel_bar = app.query_one(_ChannelBar)
        assert header.region.height == 1
        assert readout.display is False
        assert channel_bar.display is True
        assert channel_bar.region.y == header.region.bottom
        assert channel_bar.region.height == 1


@pytest.mark.asyncio
async def test_overlapping_channels_use_separate_bar():
    app = _Host(_array(1, 149, 165))
    async with app.run_test(size=(30, 24)) as pilot:
        await pilot.pause(0)
        header = app.query_one(_ScannerHeader)
        title = app.query_one(HeaderTitle)
        readout = app.query_one(_ChannelReadout)
        channel_bar = app.query_one(_ChannelBar)
        assert header.region.height == 1
        assert title.region.y == 0
        assert readout.display is False
        assert channel_bar.display is True
        assert channel_bar.region.y == header.region.bottom
        assert channel_bar.region.right == app.size.width

        app.array.members[0].current_channel = 6
        readout._poll()
        await pilot.pause(0)
        assert str(channel_bar.content) == "CH:  6 | CH:149 | CH:165"


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


@pytest.mark.asyncio
async def test_unchanged_channels_do_not_relayout_header():
    app = _Host(_array(1, 6))
    async with app.run_test() as pilot:
        await pilot.pause(0)
        header = app.query_one(_ScannerHeader)
        update_layout = Mock(wraps=header.update_channel_layout)
        header.update_channel_layout = update_layout
        readout = app.query_one(_ChannelReadout)

        readout._poll()

        update_layout.assert_not_called()
        app.array.members[1].current_channel = 11
        readout._poll()
        update_layout.assert_called_once_with("CH:  1 | CH: 11")
