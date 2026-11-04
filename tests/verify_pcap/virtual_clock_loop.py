"""Event loop whose time() is the capture's clock, and the caller bookkeeping the replay needs."""
from __future__ import annotations

import asyncio
import selectors
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

MAIN_CALLER = "main"
SELECT_TIMEOUT_SECONDS = 0.005
IDLE_SELECTS_BEFORE_CLOCK_JUMP = 2


class PoolThreadCaller(threading.local):
    def __init__(self) -> None:
        self.caller: str | None = None


_pool_thread_caller = PoolThreadCaller()


class ClockJumpSelector(selectors.SelectSelector):
    """Waits at most SELECT_TIMEOUT_SECONDS of real time (``timeout`` is virtual seconds to the next timer).
    After IDLE_SELECTS_BEFORE_CLOCK_JUMP empty waits with the driver idle, the clock jumps to that timer."""

    def __init__(self, loop: VirtualClockEventLoop) -> None:
        super().__init__()
        self.loop = loop
        self.idle_select_count = 0

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        if timeout is None:
            self.idle_select_count = 0
            return super().select(SELECT_TIMEOUT_SECONDS)
        next_timer_time = self.loop.time() + timeout
        events = super().select(min(timeout, SELECT_TIMEOUT_SECONDS))
        if events or timeout == 0:
            self.idle_select_count = 0
            return events
        self.idle_select_count += 1
        if self.idle_select_count >= IDLE_SELECTS_BEFORE_CLOCK_JUMP and self.loop.is_driver_idle():
            self.idle_select_count = 0
            self.loop.advance_clock(next_timer_time)
        return events


class VirtualClockEventLoop(asyncio.SelectorEventLoop):
    def __init__(self, start_time: float, is_driver_idle: Callable[[], bool]) -> None:
        super().__init__(ClockJumpSelector(self))
        self.virtual_time = start_time
        self.is_driver_idle = is_driver_idle
        self.latest_timer_by_task: dict[asyncio.Task[Any], asyncio.TimerHandle] = {}
        self._timer_lock = threading.Lock()

    def time(self) -> float:
        return self.virtual_time

    def advance_clock(self, timestamp: float) -> None:
        if timestamp > self.virtual_time:
            self.virtual_time = timestamp

    def call_at(self, when: float, callback: Callable[..., object], *args: Any,
                context: Any = None) -> asyncio.TimerHandle:
        handle = super().call_at(when, callback, *args, context=context)
        task = asyncio.current_task(self)
        if task is not None and task.get_name() != MAIN_CALLER:
            with self._timer_lock:
                self.latest_timer_by_task[task] = handle
        return handle

    def earliest_due_driver_timer(self) -> float | None:
        """When the earliest driver task that is awake (its last timer has fired) became due."""
        earliest: float | None = None
        with self._timer_lock:
            timers = list(self.latest_timer_by_task.items())
        for task, handle in timers:
            if task.done() or handle.cancelled() or handle.when() > self.virtual_time:
                continue
            if earliest is None or handle.when() < earliest:
                earliest = handle.when()
        return earliest


class DriverThreadPool(ThreadPoolExecutor):
    """run_in_executor work runs as the asyncio task that submitted it."""

    def __init__(self) -> None:
        super().__init__(thread_name_prefix="verify-capture-thread_pool")
        self.busy_thread_count = 0
        self._count_lock = threading.Lock()

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        caller = current_caller_name()
        with self._count_lock:
            self.busy_thread_count += 1
        return super().submit(self._run_as_caller, caller, fn, *args, **kwargs)

    def _run_as_caller(self, caller: str, fn: Callable[..., Any], *args: Any,
                         **kwargs: Any) -> Any:
        _pool_thread_caller.caller = caller
        try:
            return fn(*args, **kwargs)
        finally:
            _pool_thread_caller.caller = None
            with self._count_lock:
                self.busy_thread_count -= 1


def current_caller_name() -> str:
    """The asyncio task (main, or a driver task by coroutine name) or plain thread making the call."""
    if _pool_thread_caller.caller is not None:
        return _pool_thread_caller.caller
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is None:
        return threading.current_thread().name
    if task.get_name() == MAIN_CALLER:
        return MAIN_CALLER
    return task.get_coro().__qualname__
