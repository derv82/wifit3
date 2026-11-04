"""Event loop whose time() is the capture's clock, and the caller bookkeeping the replay needs."""
from __future__ import annotations

import asyncio
import math
import selectors
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

MAIN_CALLER = "main"
KERNEL_HZ = 250
TIMER_WHEEL_LEVEL_SHIFT = 3
TIMER_WHEEL_LEVEL_SIZE = 64
TIMER_WHEEL_LEVEL_DEPTH = 9
# A fire time, bucket_expiry / KERNEL_HZ, converts back to the same jiffy despite float error.
JIFFY_TOLERANCE = 0.01


def timer_wheel_expiry(jiffy_zero_time: float, now: float, when: float) -> float:
    """When a kernel timer armed at ``now`` for ``when`` fires: msecs_to_jiffies rounds up, then the expiry
    rounds up to its timer wheel level's granularity (kernel/time/timer.c calc_wheel_index, calc_index)."""
    now_jiffies = math.floor((now - jiffy_zero_time) * KERNEL_HZ + JIFFY_TOLERANCE)
    delta = math.ceil((when - now) * KERNEL_HZ - JIFFY_TOLERANCE)
    level = 0
    while level < TIMER_WHEEL_LEVEL_DEPTH - 1 and delta >= (TIMER_WHEEL_LEVEL_SIZE - 1) << (level * TIMER_WHEEL_LEVEL_SHIFT):
        level += 1
    shift = level * TIMER_WHEEL_LEVEL_SHIFT
    bucket_expiry = (((now_jiffies + delta) >> shift) + 1) << shift
    return jiffy_zero_time + bucket_expiry / KERNEL_HZ


def wake_loop() -> None:
    """Scheduled from another thread so the loop re-checks its timers and whether the driver is idle."""


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
        self.fired_timers: set[asyncio.TimerHandle] = set()
        self._timer_lock = threading.Lock()
        self.pending_executor_call_count = 0
        # Until CaptureReplay sets it, driver task timers fire after their jiffies exactly.
        self.jiffy_zero_time: float | None = None

    def time(self) -> float:
        return self.virtual_time

    def advance_clock(self, timestamp: float) -> None:
        """From another thread, also wakes the loop: a driver task timer may now be due."""
        if timestamp <= self.virtual_time:
            return
        self.virtual_time = timestamp
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is not self:
            self.call_soon_threadsafe(wake_loop)

    def run_in_executor(self, executor: Any, func: Callable[..., Any], *args: Any) -> asyncio.Future[Any]:
        """Pending until the result is delivered on the loop, not just until the pool thread returns.
        The pool thread starts once the callbacks scheduled before this call have run."""
        self.pending_executor_call_count += 1
        earlier_callbacks_ran = threading.Event()
        self.call_soon(earlier_callbacks_ran.set)

        def run_after_earlier_callbacks(*func_args: Any) -> Any:
            earlier_callbacks_ran.wait()
            return func(*func_args)

        future = super().run_in_executor(executor, run_after_earlier_callbacks, *args)

        def count_delivered(done: asyncio.Future[Any]) -> None:
            self.pending_executor_call_count -= 1

        future.add_done_callback(count_delivered)
        return future

    def call_at(self, when: float, callback: Callable[..., object], *args: Any,
                context: Any = None) -> asyncio.TimerHandle:
        task = asyncio.current_task(self)
        if task is None or task.get_name() == MAIN_CALLER:
            return super().call_at(when, callback, *args, context=context)
        if self.jiffy_zero_time is None:
            when = self.time() + math.ceil((when - self.time()) * KERNEL_HZ - JIFFY_TOLERANCE) / KERNEL_HZ
        else:
            when = timer_wheel_expiry(self.jiffy_zero_time, self.time(), when)

        def record_fired_and_run(*callback_args: Any) -> None:
            with self._timer_lock:
                self.fired_timers.add(handle)
            callback(*callback_args)

        handle = super().call_at(when, record_fired_and_run, *args, context=context)
        with self._timer_lock:
            previous_handle = self.latest_timer_by_task.get(task)
            if previous_handle is not None:
                self.fired_timers.discard(previous_handle)
            self.latest_timer_by_task[task] = handle
        return handle

    def earliest_due_driver_timer(self) -> float | None:
        """When the earliest driver task that is awake (its last timer has fired) became due."""
        earliest: float | None = None
        with self._timer_lock:
            timers = list(self.latest_timer_by_task.items())
        for task, handle in timers:
            if task.done() or handle not in self.fired_timers:
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
