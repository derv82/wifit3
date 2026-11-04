"""Event loop whose time() is the capture's clock, and the caller bookkeeping the replay needs."""
from __future__ import annotations

import asyncio
import selectors
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

MAIN_CALLER = "main"
class PoolThreadCaller(threading.local):
    def __init__(self) -> None:
        self.caller: str | None = None


_pool_thread_caller = PoolThreadCaller()


class ClockJumpSelector(selectors.SelectSelector):
    """Never waits on the capture's clock: with the driver idle, the clock jumps to the next timer.
    With the driver busy, waits until a thread wakes the loop (thread pool completion, a driver thread's read)."""

    def __init__(self, loop: VirtualClockEventLoop) -> None:
        super().__init__()
        self.loop = loop

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        if timeout == 0:
            return super().select(0)
        if self.loop.is_driver_idle():
            if timeout is not None:
                self.loop.advance_clock(self.loop.time() + timeout)
            return super().select(0)
        return super().select(None)


class VirtualClockEventLoop(asyncio.SelectorEventLoop):
    def __init__(self, start_time: float, is_driver_idle: Callable[[], bool]) -> None:
        super().__init__(ClockJumpSelector(self))
        self.virtual_time = start_time
        self.is_driver_idle = is_driver_idle
        self.latest_timer_by_task: dict[asyncio.Task[Any], asyncio.TimerHandle] = {}
        self._timer_lock = threading.Lock()
        self.pending_executor_call_count = 0

    def time(self) -> float:
        return self.virtual_time

    def advance_clock(self, timestamp: float) -> None:
        if timestamp > self.virtual_time:
            self.virtual_time = timestamp

    def run_in_executor(self, executor: Any, func: Callable[..., Any], *args: Any) -> asyncio.Future[Any]:
        """Pending until the result is delivered on the loop, not just until the pool thread returns."""
        self.pending_executor_call_count += 1
        future = super().run_in_executor(executor, func, *args)

        def count_delivered(done: asyncio.Future[Any]) -> None:
            self.pending_executor_call_count -= 1

        future.add_done_callback(count_delivered)
        return future

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

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        caller = current_caller_name()
        return super().submit(self._run_as_caller, caller, fn, *args, **kwargs)

    def _run_as_caller(self, caller: str, fn: Callable[..., Any], *args: Any,
                         **kwargs: Any) -> Any:
        _pool_thread_caller.caller = caller
        try:
            return fn(*args, **kwargs)
        finally:
            _pool_thread_caller.caller = None


def is_driver_thread() -> bool:
    """A thread the driver started itself (e.g. its RX reader): not the event loop, not the thread pool."""
    if _pool_thread_caller.caller is not None:
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return True
    return False


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
