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


def timer_wheel_level_start(level: int) -> int:
    return (TIMER_WHEEL_LEVEL_SIZE - 1) << ((level - 1) * TIMER_WHEEL_LEVEL_SHIFT)


TIMER_WHEEL_TIMEOUT_CUTOFF = timer_wheel_level_start(TIMER_WHEEL_LEVEL_DEPTH)
TIMER_WHEEL_TIMEOUT_MAX = TIMER_WHEEL_TIMEOUT_CUTOFF - (1 << ((TIMER_WHEEL_LEVEL_DEPTH - 1) * TIMER_WHEEL_LEVEL_SHIFT))


def timer_wheel_level(delta: int) -> int:
    level = 0
    while level < TIMER_WHEEL_LEVEL_DEPTH - 1 and delta >= timer_wheel_level_start(level + 1):
        level += 1
    return level


def timer_wheel_tolerance(delta: int) -> float:
    """How far a modeled fire time can be from the kernel's: its level's bucket (alignment unknown), a jiffy each for
    the arm and the fire time (the jiffy phase comes from one woken op), and a jiffy for the kernel worker's latency."""
    return ((1 << (timer_wheel_level(delta) * TIMER_WHEEL_LEVEL_SHIFT)) + 3) / KERNEL_HZ


def timer_wheel_expiry(jiffy_zero_time: float, now: float, when: float) -> float:
    """When a kernel timer armed at ``now`` for ``when`` fires: msecs_to_jiffies rounds up, then every level, level 0
    too, rounds the expiry up to its next bucket (linux-timer-v6.19.14 kernel/time/timer.c:524-577)."""
    delta = math.ceil((when - now) * KERNEL_HZ - JIFFY_TOLERANCE)
    now_jiffies = math.floor((now - jiffy_zero_time) * KERNEL_HZ + JIFFY_TOLERANCE)
    expires = now_jiffies + delta
    if delta >= TIMER_WHEEL_TIMEOUT_CUTOFF:
        expires = now_jiffies + TIMER_WHEEL_TIMEOUT_MAX
    shift = timer_wheel_level(delta) * TIMER_WHEEL_LEVEL_SHIFT
    bucket_expiry = ((expires >> shift) + 1) << shift
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
        if self.loop.is_driver_idle() and self.loop.pending_thread_callback_count == 0:
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
        self.completion_time_by_caller: dict[str, float] = {}
        self._timer_lock = threading.Lock()
        self.pending_executor_call_count = 0
        self.pending_thread_callback_count = 0
        self._thread_callback_lock = threading.Lock()
        # A driver thread holds the replies it read until it schedules a callback (RxReaderThread hands over its batch).
        self.driver_threads_holding_reply: set[threading.Thread] = set()
        # Until CaptureReplay sets it, driver task timers fire after their jiffies exactly.
        self.jiffy_zero_time: float | None = None
        self.tolerance_by_driver_timer: dict[asyncio.TimerHandle, float] = {}
        # The driver tasks whose timer fired and that have made no request since, with that timer's tolerance.
        self.timer_tolerance_by_woken_caller: dict[str, float] = {}

    def time(self) -> float:
        """In a driver task, the later of its last fired timer's due time and its last op's completion,
        never past the capture's clock: not the clock another task's pass-over pushed ahead."""
        try:
            task = asyncio.current_task()
        except RuntimeError:
            return self.virtual_time
        if task is None or task.get_name() == MAIN_CALLER:
            return self.virtual_time
        task_time = self.completion_time_by_caller.get(task.get_coro().__qualname__)
        handle = self.latest_timer_by_task.get(task)
        if handle is not None and handle in self.fired_timers:
            if task_time is None or handle.when() > task_time:
                task_time = handle.when()
        if task_time is None or task_time > self.virtual_time:
            return self.virtual_time
        return task_time

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

    def call_soon_threadsafe(self, callback: Callable[..., object], *args: Any,
                             context: Any = None) -> asyncio.Handle:
        """Pending until the callback runs: asyncio may have computed the select timeout before it arrived."""
        with self._thread_callback_lock:
            self.pending_thread_callback_count += 1
        if callback is not wake_loop:
            self.driver_threads_holding_reply.discard(threading.current_thread())

        def count_ran_and_run(*callback_args: Any) -> None:
            with self._thread_callback_lock:
                self.pending_thread_callback_count -= 1
            callback(*callback_args)

        return super().call_soon_threadsafe(count_ran_and_run, *args, context=context)

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
        delta = math.ceil((when - self.time()) * KERNEL_HZ - JIFFY_TOLERANCE)
        if self.jiffy_zero_time is None:
            when = self.time() + delta / KERNEL_HZ
        else:
            when = timer_wheel_expiry(self.jiffy_zero_time, self.time(), when)

        def record_fired_and_run(*callback_args: Any) -> None:
            with self._timer_lock:
                self.fired_timers.add(handle)
                self.timer_tolerance_by_woken_caller[task.get_coro().__qualname__] = (
                    self.tolerance_by_driver_timer[handle])
            callback(*callback_args)

        handle = super().call_at(when, record_fired_and_run, *args, context=context)
        with self._timer_lock:
            self.tolerance_by_driver_timer[handle] = timer_wheel_tolerance(delta)
            previous_handle = self.latest_timer_by_task.get(task)
            if previous_handle is not None:
                self.fired_timers.discard(previous_handle)
            self.latest_timer_by_task[task] = handle
        return handle

    def pending_driver_timer_due_within_tolerance_of(self, timestamp: float) -> float | None:
        """The earliest due time of a driver task timer not fired yet that is due within its tolerance after ``timestamp``."""
        with self._timer_lock:
            timers = list(self.latest_timer_by_task.items())
        due_times: list[float] = []
        for task, handle in timers:
            if task.done() or handle.cancelled() or handle in self.fired_timers:
                continue
            if handle.when() - self.tolerance_by_driver_timer[handle] <= timestamp:
                due_times.append(handle.when())
        return min(due_times, default=None)

    def earliest_due_driver_timer(self) -> float | None:
        """The earliest time the kernel timer of a driver task that is awake (its last timer has fired) can have
        fired: its due time less its tolerance."""
        earliest: float | None = None
        with self._timer_lock:
            timers = list(self.latest_timer_by_task.items())
        for task, handle in timers:
            if task.done() or handle not in self.fired_timers:
                continue
            due_time = handle.when() - self.tolerance_by_driver_timer[handle]
            if earliest is None or due_time < earliest:
                earliest = due_time
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
