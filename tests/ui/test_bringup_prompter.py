import signal
from contextlib import contextmanager

from textual.app import SuspendNotSupported

from wifit3.ui.bringup_prompter import BringupPrompter


class _App:
    def __init__(self, can_suspend: bool) -> None:
        self.can_suspend = can_suspend
        self.suspended = False

    @contextmanager
    def suspend(self):
        if not self.can_suspend:
            raise SuspendNotSupported("no")
        self.suspended = True
        yield
        self.suspended = False


async def test_run_with_terminal_runs_inside_suspend_with_ctrl_c_raising():
    app = _App(can_suspend=True)
    previous = signal.getsignal(signal.SIGINT)
    seen = {}

    def _run() -> int:
        seen.update(suspended=app.suspended, sigint=signal.getsignal(signal.SIGINT))
        return 0

    assert await BringupPrompter(app).run_with_terminal(_run) == 0
    assert seen == {"suspended": True, "sigint": signal.default_int_handler}
    assert signal.getsignal(signal.SIGINT) is previous and not app.suspended


async def test_run_with_terminal_suspend_not_supported_returns_none():
    ran = []
    assert await BringupPrompter(_App(can_suspend=False)).run_with_terminal(lambda: ran.append(1) or 0) is None
    assert not ran
