"""Geometry contract for the channel-filter modal: the OK/Cancel buttons must
stay fully on-screen at small terminal sizes, even with a full 2.4+5 GHz channel
list that overflows the dialog (the list scrolls; the docked buttons never clip).
Aesthetics stay the human's call; this only guards "the buttons are reachable"."""
import pytest
from textual.app import App
from textual.widgets import Button, SelectionList

from wifit3.ui.screens.channel_filter import ChannelFilterDialog

# More channels than fit any small dialog, so the list must scroll and the
# buttons must not be pushed out.
_ALL_CHANNELS = list(range(1, 15)) + [
    36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124,
    128, 132, 136, 140, 144, 149, 153, 157, 161, 165,
]


class _Host(App):
    """Minimal host: push the modal straight in (no device manager)."""
    def on_mount(self) -> None:
        self.push_screen(ChannelFilterDialog(_ALL_CHANNELS))


@pytest.mark.parametrize("w,h", [(80, 24), (60, 18), (50, 16), (40, 12)])
async def test_buttons_stay_on_screen(w, h):
    app = _Host()
    async with app.run_test(size=(w, h)) as pilot:
        await pilot.pause(0)
        dialog = app.screen
        assert isinstance(dialog, ChannelFilterDialog)
        # The list shrinks/scrolls inside the dialog instead of overflowing it.
        assert dialog.query_one(SelectionList).region.bottom <= h
        for bid in ("#btn-ok", "#btn-cancel"):
            r = dialog.query_one(bid, Button).region
            assert r.width > 0 and r.height > 0, (bid, r)
            assert r.y >= 0 and r.bottom <= h, (bid, r, h)
            assert r.x >= 0 and r.right <= w, (bid, r, w)


async def test_list_not_collapsed_when_roomy():
    """With ample height the list shows many channels (1fr didn't collapse it)
    and the buttons are still on-screen."""
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0)
        dialog = app.screen
        assert dialog.query_one(SelectionList).region.height >= 12
        for bid in ("#btn-ok", "#btn-cancel"):
            r = dialog.query_one(bid, Button).region
            assert r.height > 0 and r.bottom <= 40
