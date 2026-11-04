"""The ordered ops a driver must reproduce, reproduced through one cursor per caller."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from tests.verify_pcap.usb_capture import TransferType, UsbOp
from tests.verify_pcap.virtual_clock_loop import VirtualClockEventLoop

POLLED_ENDPOINT_SECONDS = 0.05


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
    def __init__(self, ops: list[UsbOp], waiver_label_by_position: dict[int, str],
                 loop: VirtualClockEventLoop) -> None:
        self.ops = ops
        self.reproduced_by: list[str | None] = [None] * len(ops)
        for position, label in waiver_label_by_position.items():
            self.reproduced_by[position] = label
        self.next_position_by_caller: dict[str, int] = {}
        self.divergence: Divergence | None = None
        self.refused_read_by_caller: dict[str, RefusedRead] = {}
        self.driver_closing = False
        self.requests_after_capture_end = 0
        self._last_read_time_by_endpoint: dict[int, float] = {}
        self._last_served_read_time = float("-inf")
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
        None for a request close() makes after the last captured op: not compared."""
        with self._lock:
            if self.divergence is not None:
                raise CaptureDivergence()
            start = max(self.next_position_by_caller.get(caller, 0), self._advance_first_unreproduced())
            position = self._next_unreproduced(start)
            if position is None:
                if self.driver_closing:
                    self.requests_after_capture_end += 1
                    return None
                raise self._diverge(None, request, caller, "request after the last captured op")
            if not self.ops[position].same_request(request):
                # Pass over unreproduced ops only if a driver task was due before the first of them.
                due_time = self._loop.earliest_due_driver_timer()
                candidate = None
                if due_time is not None and self.ops[position].timestamp >= due_time:
                    candidate = self._next_unreproduced(position + 1)
                    while candidate is not None and not self.ops[candidate].same_request(request):
                        candidate = self._next_unreproduced(candidate + 1)
                if candidate is None:
                    raise self._diverge(position, request, caller, "request differs from the capture")
                position = candidate
            self.reproduced_by[position] = caller
            self.next_position_by_caller[caller] = position + 1
            captured = self.ops[position]
            self._loop.advance_clock(captured.timestamp)
            return captured

    def read(self, endpoint: int, caller: str) -> UsbOp | None:
        """The next captured IN op on ``endpoint``, or None while the replay has not reached it."""
        with self._lock:
            if self.divergence is not None:
                raise CaptureDivergence()
            self._last_read_time_by_endpoint[endpoint] = time.monotonic()
            queue = self._in_positions_by_endpoint.get(endpoint)
            if queue is None:
                return None
            while queue and self.reproduced_by[queue[0]] is not None:
                queue.popleft()
            if not queue:
                return None
            position = queue[0]
            if not self._is_readable(position):
                self.refused_read_by_caller[caller] = RefusedRead(
                    endpoint, position, self._gate_by_position[position])
                return None
            queue.popleft()
            self._last_served_read_time = time.monotonic()
            self.refused_read_by_caller.pop(caller, None)
            self.reproduced_by[position] = caller
            captured = self.ops[position]
            self._loop.advance_clock(captured.timestamp)
            return captured

    def has_in_transfer_in_flight(self) -> bool:
        """In the last POLLED_ENDPOINT_SECONDS (real time) an IN op was served, or an endpoint read then
        has one readable now."""
        with self._lock:
            now = time.monotonic()
            if now - self._last_served_read_time <= POLLED_ENDPOINT_SECONDS:
                return True
            for endpoint, read_time in self._last_read_time_by_endpoint.items():
                if now - read_time > POLLED_ENDPOINT_SECONDS:
                    continue
                for position in self._in_positions_by_endpoint.get(endpoint, ()):
                    if self.reproduced_by[position] is None:
                        if self._is_readable(position):
                            return True
                        break
            return False

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
