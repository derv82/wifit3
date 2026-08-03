"""ReplugModal outcome contract: a detected unplug→replug cycle dismisses "replugged"; the Skip
button dismisses "skip". The presence-waiter is stubbed so no hardware is needed."""
import asyncio

from textual.app import App

from wifit3.ui.screens.replug import ReplugModal


async def _fast(present):
    """Both phases resolve immediately → the modal reaches 'replugged'."""
    return True


async def _hang(present):
    """Never resolves → the modal sits in phase 1 until the user skips."""
    await asyncio.Event().wait()


class _Host(App):
    def __init__(self, wait_present):
        super().__init__()
        self._wait_present = wait_present
        self.result = "UNSET"

    def on_mount(self) -> None:
        self.push_screen(ReplugModal("RT5372", self._wait_present),
                         callback=lambda r: setattr(self, "result", r))


async def test_modal_dismisses_replugged_when_cycle_detected():
    app = _Host(_fast)
    async with app.run_test(size=(100, 40)) as pilot:
        for _ in range(6):
            await pilot.pause(0)
    assert app.result == "replugged"


async def test_skip_button_dismisses_skip():
    app = _Host(_hang)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0)
        await pilot.click("#btn-skip")
        await pilot.pause(0)
    assert app.result == "skip"
