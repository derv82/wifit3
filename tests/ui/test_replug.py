"""ReplugModal outcome contract: a detected unplug→replug cycle dismisses "replugged"; the Skip
button dismisses "skip". The bus poll is stubbed so no hardware is needed."""
import asyncio

from textual.app import App

from wifit3.ui.screens.replug import ReplugModal


class _FastDM:
    """Both phases resolve immediately → the modal reaches 'replugged'."""
    async def linux_wait_for_presence(self, vid, pid, *, present):
        return True


class _HangDM:
    """Never resolves → the modal sits in phase 1 until the user skips."""
    async def linux_wait_for_presence(self, vid, pid, *, present):
        await asyncio.Event().wait()


class _Host(App):
    def __init__(self, dm):
        super().__init__()
        self._dm = dm
        self.result = "UNSET"

    def on_mount(self) -> None:
        self.push_screen(ReplugModal(self._dm, 0x1, 0x2, "RT5372"),
                         callback=lambda r: setattr(self, "result", r))


async def test_modal_dismisses_replugged_when_cycle_detected():
    app = _Host(_FastDM())
    async with app.run_test(size=(100, 40)) as pilot:
        for _ in range(6):
            await pilot.pause()
    assert app.result == "replugged"


async def test_skip_button_dismisses_skip():
    app = _Host(_HangDM())
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#btn-skip")
        await pilot.pause()
    assert app.result == "skip"
