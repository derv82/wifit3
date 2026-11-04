"""The ordered ops a driver must reproduce, reproduced through one cursor per caller."""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable

from tests.verify_pcap.usb_capture import TransferType, UsbOp
from tests.verify_pcap.virtual_clock_loop import (
    KERNEL_HZ, MAIN_CALLER, VirtualClockEventLoop, is_driver_thread, wake_loop)

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
    def __init__(self, ops: list[UsbOp], waived_positions: set[int], is_same_request: Callable[[UsbOp, UsbOp], bool],
                 is_driver_only_request: Callable[[UsbOp], bool], loop: VirtualClockEventLoop) -> None:
        """``is_same_request(captured, request)`` compares a driver request to a captured op;
        ``is_driver_only_request(request)`` accepts a request the capture does not have."""
        self.ops = ops
        self.reproduced_by: list[str | None] = [None] * len(ops)
        for position in waived_positions:
            self.reproduced_by[position] = WAIVED
        self._is_same_request = is_same_request
        self._is_driver_only_request = is_driver_only_request
        self.next_position_by_caller: dict[str, int] = {}
        self.divergence: Divergence | None = None
        self.refused_read_by_caller: dict[str, RefusedRead] = {}
        self.driver_closing = False
        # Per thread the driver started itself (its RX reader): PROCESSING_REPLY, or the endpoint it waits on.
        self._driver_thread_state: dict[threading.Thread, int] = {}
        self._loop = loop
        self._lock = threading.Lock()
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
            start = max(self.next_position_by_caller.get(caller, 0), self._advance_first_unreproduced())
            position = self._next_unreproduced(start)
            if position is None:
                if self.driver_closing:
                    return None
                raise self._diverge(None, request, caller, "request after the last captured op")
            if not self._is_same_request(self.ops[position], request):
                # Pass over unreproduced ops only if a driver task was due before the first of them.
                due_time = self._loop.earliest_due_driver_timer()
                candidate = None
                if due_time is not None and self.ops[position].submit_time >= due_time:
                    candidate = self._next_unreproduced(position + 1)
                    while candidate is not None and not self._is_same_request(self.ops[candidate], request):
                        candidate = self._next_unreproduced(candidate + 1)
                if candidate is None:
                    if self._is_driver_only_request(request):
                        # Replies with the latest earlier capture op of the same request, or zeros.
                        for earlier in range(position - 1, -1, -1):
                            if self.ops[earlier].same_request(request):
                                return self.ops[earlier]
                        return None
                    raise self._diverge(position, request, caller, "request differs from the capture")
                # The clock stays: the ops passed over are not reproduced yet.
                position = candidate
            else:
                self._loop.advance_clock(self.ops[position].completion_time)
            if (self._loop.jiffy_zero_time is None and caller != MAIN_CALLER
                    and self._loop.earliest_due_driver_timer() is not None):
                # The kernel's jiffy phase: a woken driver task's first op is made in the jiffy after its timer fired.
                self._loop.jiffy_zero_time = self.ops[position].submit_time - 1 / KERNEL_HZ
            self.reproduced_by[position] = caller
            self.next_position_by_caller[caller] = position + 1
            return self.ops[position]

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
            return captured

    def driver_thread_is_working(self) -> bool:
        """A thread the driver started (its RX reader) is processing a reply, or waits on an endpoint that
        has a readable reply now."""
        with self._lock:
            for thread, state in self._driver_thread_state.items():
                if not thread.is_alive():
                    continue
                if state == PROCESSING_REPLY:
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
