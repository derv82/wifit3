"""The Scanner keeps DFS channels out of the channel picker and the hop unless the DFS pref is on."""
import pytest

from wifit3.persist.config import Config
from wifit3.ui.screens.scanner import ScannerView
from tests.ui.test_scanner_filter import _FakeArray, _ScannerHost

_CHANNELS = [1, 6, 36, 52, 100, 149]


class _RecordingArray(_FakeArray):
    def __init__(self, aps, supported):
        super().__init__(aps, supported)
        self.hop_args = []

    async def start_hopping(self, channels=None, interval=0.25, include_dfs_channels=True):
        self.hop_args.append((channels, include_dfs_channels))


@pytest.fixture
def dfs_pref():
    saved = Config.enable_dfs_channels
    yield
    Config.enable_dfs_channels = saved


def test_selectable_channels_leave_out_dfs_channels_when_the_pref_is_off(dfs_pref):
    Config.enable_dfs_channels = False
    assert ScannerView._selectable_channels(_FakeArray([], _CHANNELS)) == [1, 6, 36, 149]
    Config.enable_dfs_channels = True
    assert ScannerView._selectable_channels(_FakeArray([], _CHANNELS)) == _CHANNELS


@pytest.mark.asyncio
async def test_picking_every_non_dfs_channel_with_the_pref_off_is_the_full_band(dfs_pref):
    Config.enable_dfs_channels = False
    app = _ScannerHost(_RecordingArray([], _CHANNELS))
    async with app.run_test() as pilot:
        await pilot.pause(0)
        scanner = app.screen
        await scanner._apply_channel_filter([1, 6, 36, 149])
        assert scanner._channel_filter is None             # hotplug keeps re-spreading the full band
        assert app.array.hop_args[-1] == (None, False)
