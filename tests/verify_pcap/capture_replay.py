"""The ordered ops a driver must reproduce, reproduced through one cursor per caller."""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Sequence

from tests.verify_pcap.usb_capture import TransferType, UsbOp
from tests.verify_pcap.virtual_clock_loop import (
    KERNEL_HZ, MAIN_CALLER, HrtimerDelay, VirtualClockEventLoop, is_driver_thread, wake_loop)

PROCESSING_REPLY = -1
WAIVED = "waived"


class CaptureDivergence(Exception):
    """Raised into the driver once the replay has diverged; the test reports ``CaptureReplay.divergence``."""


@dataclass(frozen=True)
class Divergence:
    position: int | None
    request: UsbOp | None
    caller: str
    reason: str


@dataclass(frozen=True)
class RefusedRead:
    """A read of ``endpoint`` before the replay reproduced ``gate_position``, the OUT before ``position``."""
    endpoint: int
    position: int
    gate_position: int


def is_bulk_or_interrupt_in(op: UsbOp) -> bool:
    """Bulk / interrupt IN: served from a per-endpoint queue instead of through a cursor."""
    return op.is_in and op.transfer_type != TransferType.CONTROL


class CaptureReplay:
    def __init__(self, ops: list[UsbOp], is_same_request: Callable[[UsbOp, UsbOp], bool],
                 is_driver_only_request: Callable[[UsbOp, bool], bool],
                 skip_capture_only_run_at: Callable[[list[UsbOp], int], tuple[list[UsbOp], set[int]] | None],
                 is_racing_extra_request: Callable[[Sequence[UsbOp], UsbOp], bool],
                 loop: VirtualClockEventLoop) -> None:
        """``is_same_request(captured, request)`` compares a driver request to a captured op;
        ``is_driver_only_request(request, cursor_differs)`` accepts a request the capture does not have;
        ``skip_capture_only_run_at(ops, position)``: the ops without the capture-only run there, and the positions to waive;
        ``is_racing_extra_request(caller_ops, request)``: Linux makes ``request`` after an hrtimer task's last two ops
        or not, by a race; not making it, Linux re-armed its hrtimer on an event inside the request's delay."""
        self.ops = ops
        self.reproduced_by: list[str | None] = [None] * len(ops)
        self._is_same_request = is_same_request
        self._is_driver_only_request = is_driver_only_request
        self._skip_capture_only_run_at = skip_capture_only_run_at
        self._is_racing_extra_request = is_racing_extra_request
        # Per hrtimer task, the position of its op after which it made a racing extra request.
        self._racing_extra_request_after: dict[str, int] = {}
        self.racing_extra_request_count = 0
        self.next_position_by_caller: dict[str, int] = {}
        self.divergence: Divergence | None = None
        self.refused_read_by_caller: dict[str, RefusedRead] = {}
        self.driver_closing = False
        # The request the capture ends in, never completed: from it on, requests are past the capture, not compared.
        self.truncated_request: UsbOp | None = None
        self.past_capture_end = False
        # Replies, in order, to the driver's probe requests the capture starts after: not compared.
        self.probe_replies: list[UsbOp] = []
        # Per thread the driver started itself (its RX reader): PROCESSING_REPLY, or the endpoint it waits on.
        self._driver_thread_state: dict[threading.Thread, int] = {}
        self._loop = loop
        self._lock = threading.RLock()
        self._first_unreproduced = 0
        self._in_positions_by_endpoint: dict[int, deque[int]] = {}
        self._gate_by_position: dict[int, int] = {}
        gate = -1
        for position, op in enumerate(ops):
            if is_bulk_or_interrupt_in(op):
                self._in_positions_by_endpoint.setdefault(op.endpoint, deque()).append(position)
                self._gate_by_position[position] = gate
            else:
                gate = position

    def match_request(self, request: UsbOp, caller: str) -> UsbOp | None:
        """The captured op matching ``request`` (a control or OUT op); raises on divergence.
        None for a request close() makes after the last captured op, or a driver-only one: not compared."""
        with self._lock:
            if self.divergence is not None:
                raise CaptureDivergence()
            if is_driver_thread():
                self._driver_thread_state[threading.current_thread()] = PROCESSING_REPLY
            if (self.probe_replies and self.probe_replies[0].same_request(request)
                    and all(reproduced is None for reproduced in self.reproduced_by)):
                return self.probe_replies.pop(0)
            request_time = self._loop.virtual_time
            start = max(self.next_position_by_caller.get(caller, 0), self._advance_first_unreproduced())
            passed_over = False
            while True:
                position = self._next_unreproduced(start)
                if position is None:
                    if self.driver_closing or self.past_capture_end:
                        return None
                    if self.truncated_request is not None and self._is_same_request(self.truncated_request, request):
                        self.past_capture_end = True
                        return None
                    racing_reply = self._racing_extra_reply(request, caller, request_time)
                    if racing_reply is not None:
                        return racing_reply
                    raise self._diverge(None, request, caller, "request after the last captured op")
                cursor_differs = not self._is_same_request(self.ops[position], request)
                if self._is_driver_only_request(request, cursor_differs):
                    # Replies with the latest earlier capture op of the same request, or zeros.
                    for earlier in range(position - 1, -1, -1):
                        if self.ops[earlier].same_request(request):
                            return self.ops[earlier]
                    return None
                if not cursor_differs:
                    break
                # Pass over unreproduced ops only if a driver task was due before the first of them.
                due_time = self._loop.earliest_due_driver_timer()
                candidate = None
                if due_time is not None and self.ops[position].submit_time >= due_time:
                    candidate = self._next_unreproduced(position + 1)
                    while candidate is not None and not self._is_same_request(self.ops[candidate], request):
                        candidate = self._next_unreproduced(candidate + 1)
                if candidate is not None:
                    position = candidate
                    passed_over = True
                    break
                self.waive_capture_only_run_at(position)
                if self.reproduced_by[position] is None:
                    racing_reply = self._racing_extra_reply(request, caller, request_time)
                    if racing_reply is not None:
                        return racing_reply
                    raise self._diverge(position, request, caller, "request differs from the capture")
            timer_tolerance = self._loop.timer_tolerance_by_woken_caller.pop(caller, None)
            if timer_tolerance is not None:
                # The kernel's work may have waited on the other callers' ops between its previous op and this one;
                # an hrtimer submits its URB from the interrupt, waiting on none.
                captured_time = self.ops[position].submit_time
                earlier = position - 1
                while (earlier >= 0 and self.reproduced_by[earlier] != caller
                       and caller not in self._loop.hrtimer_callers):
                    if not is_bulk_or_interrupt_in(self.ops[earlier]):
                        captured_time = self.ops[earlier].submit_time
                    earlier -= 1
                if captured_time - request_time > timer_tolerance:
                    racing_reply = self._racing_extra_reply(request, caller, request_time)
                    if racing_reply is not None:
                        return racing_reply
                    raise self._diverge(position, request, caller, f"the driver task's timer fired "
                                        f"{(captured_time - request_time) * 1000:.1f} ms before the capture's op")
            if not passed_over:
                # Passing over, the clock stays: the ops passed over are not reproduced yet.
                self._loop.advance_clock(self.ops[position].completion_time)
            if (self._loop.jiffy_zero_time is None and caller != MAIN_CALLER
                    and caller not in self._loop.hrtimer_callers
                    and self._loop.earliest_due_driver_timer() is not None):
                # The kernel's jiffy phase: a woken driver task's first op is made in the jiffy after its timer fired.
                self._loop.jiffy_zero_time = self.ops[position].submit_time - 1 / KERNEL_HZ
            self.reproduced_by[position] = caller
            self.next_position_by_caller[caller] = position + 1
            self._loop.completion_time_by_caller[caller] = self.ops[position].completion_time
            return self.ops[position]

    def _racing_extra_reply(self, request: UsbOp, caller: str, request_time: float) -> UsbOp | None:
        """For a racing extra request the capture does not have, made once after the caller's last op: that op.
        The caller's next hrtimer fires up to the request's delay later, as Linux's re-armed one did."""
        if caller not in self._loop.hrtimer_callers:
            return None
        caller_positions: list[int] = []
        earlier = self.next_position_by_caller.get(caller, 0) - 1
        while earlier >= 0 and len(caller_positions) < 2:
            if self.reproduced_by[earlier] == caller:
                caller_positions.insert(0, earlier)
            earlier -= 1
        if not caller_positions or self._racing_extra_request_after.get(caller) == caller_positions[-1]:
            return None
        if not self._is_racing_extra_request([self.ops[position] for position in caller_positions], request):
            return None
        self._racing_extra_request_after[caller] = caller_positions[-1]
        self.racing_extra_request_count += 1
        next_position = self._next_unreproduced(caller_positions[-1] + 1)
        while next_position is not None and not self.ops[next_position].same_request(request):
            next_position = self._next_unreproduced(next_position + 1)
        if next_position is not None:
            self._loop.hrtimer_delay_by_caller[caller] = HrtimerDelay(
                request_time - self.ops[caller_positions[-1]].completion_time, self.ops[next_position].submit_time)
        return self.ops[caller_positions[-1]]

    def read(self, endpoint: int, caller: str) -> UsbOp | None:
        """The next captured IN op on ``endpoint``, or None while the replay has not reached it."""
        with self._lock:
            if self.divergence is not None:
                raise CaptureDivergence()
            position = self._next_readable_position(endpoint, caller)
            if is_driver_thread():
                if position is None:
                    self._driver_thread_state[threading.current_thread()] = endpoint
                else:
                    self._driver_thread_state[threading.current_thread()] = PROCESSING_REPLY
            if position is None:
                self._loop.call_soon_threadsafe(wake_loop)
                return None
            self._in_positions_by_endpoint[endpoint].popleft()
            self.refused_read_by_caller.pop(caller, None)
            self.reproduced_by[position] = caller
            captured = self.ops[position]
            if not is_driver_thread():
                self._loop.advance_clock(captured.completion_time)
            elif captured.status == 0 and captured.data:
                self._loop.driver_threads_holding_reply.add(threading.current_thread())
            return captured

    def driver_thread_is_working(self) -> bool:
        """A thread the driver started (its RX reader) is processing a reply, holds one it has not handed to the
        loop, or waits on an endpoint that has a readable reply now."""
        with self._lock:
            for thread, state in self._driver_thread_state.items():
                if not thread.is_alive():
                    continue
                if state == PROCESSING_REPLY or thread in self._loop.driver_threads_holding_reply:
                    return True
                waiting_position = self._first_queued_position(state)
                if waiting_position is not None and self._is_readable(waiting_position):
                    return True
            return False

    def _first_queued_position(self, endpoint: int) -> int | None:
        queue = self._in_positions_by_endpoint.get(endpoint)
        if queue is None:
            return None
        while queue and self.reproduced_by[queue[0]] is not None:
            queue.popleft()
        if not queue:
            return None
        return queue[0]

    def _next_readable_position(self, endpoint: int, caller: str) -> int | None:
        position = self._first_queued_position(endpoint)
        if position is None:
            return None
        if not self._is_readable(position):
            self.refused_read_by_caller[caller] = RefusedRead(endpoint, position, self._gate_by_position[position])
            return None
        return position

    def next_unreproduced_position(self) -> int | None:
        with self._lock:
            return self._next_unreproduced(self._advance_first_unreproduced())

    def waive_capture_only_run_at(self, position: int) -> None:
        """Called where the replay would stop at ``position`` because the driver did not make it."""
        with self._lock:
            skipped = self._skip_capture_only_run_at(self.ops, position)
            if skipped is None:
                return
            self.ops, waived_positions = skipped
            for waived_position in waived_positions:
                self.reproduced_by[waived_position] = WAIVED

    def waive_capture_only_ops(self, skip_capture_only_ops: Callable[[list[UsbOp], set[int]],
                                                                     tuple[list[UsbOp], set[int]]],
                               positions: set[int]) -> None:
        """Waives the unreproduced control / OUT ops at ``positions``, through the decoder's skip_capture_only_ops."""
        with self._lock:
            unreproduced = {position for position in positions
                            if self.reproduced_by[position] is None and not is_bulk_or_interrupt_in(self.ops[position])}
            self.ops, waived_positions = skip_capture_only_ops(self.ops, unreproduced)
            for waived_position in waived_positions:
                self.reproduced_by[waived_position] = WAIVED

    def record_divergence(self, position: int | None, caller: str, reason: str) -> None:
        with self._lock:
            if self.divergence is None:
                self.divergence = Divergence(position, None, caller, reason)

    def unreproduced_positions(self) -> list[int]:
        with self._lock:
            return [position for position, reproduced in enumerate(self.reproduced_by) if reproduced is None]

    def _is_readable(self, queued_position: int) -> bool:
        gate = self._gate_by_position[queued_position]
        return gate < 0 or self.reproduced_by[gate] is not None

    def _advance_first_unreproduced(self) -> int:
        while (self._first_unreproduced < len(self.ops)
               and self.reproduced_by[self._first_unreproduced] is not None):
            self._first_unreproduced += 1
        return self._first_unreproduced

    def _next_unreproduced(self, start: int) -> int | None:
        for position in range(start, len(self.ops)):
            if self.reproduced_by[position] is None and not is_bulk_or_interrupt_in(self.ops[position]):
                return position
        return None

    def _diverge(self, position: int | None, request: UsbOp, caller: str,
                 reason: str) -> CaptureDivergence:
        self.divergence = Divergence(position, request, caller, reason)
        return CaptureDivergence()
