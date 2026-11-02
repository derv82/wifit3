import time
from typing import Callable

from textual.pilot import Pilot


async def wait_until(pilot: Pilot, condition: Callable[[], bool]) -> None:
    """Pause until ``condition()`` holds; fail after 5 s instead of hanging. ``pilot.pause()``
    waits for CPU idle, which costs ~50 ms a call on Windows."""
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, "condition never held"
        await pilot.pause(0)
