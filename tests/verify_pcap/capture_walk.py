"""The ordered transfers a driver must reproduce, claimed through one cursor per producer."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from tests.verify_pcap.usb_capture import TransferType, UsbTransfer
from tests.verify_pcap.virtual_clock_loop import VirtualClockEventLoop

POLLED_ENDPOINT_SECONDS = 0.05


class CaptureDivergence(Exception):
    """Raised into the driver once the walk has diverged; the test reports ``CaptureWalk.divergence``."""


@dataclass(frozen=True)
class Divergence:
    position: int | None
    request: UsbTransfer | None
    producer: str
    reason: str


@dataclass(frozen=True)
class RefusedRead:
    """A read of ``endpoint`` before the walk claimed ``gate_position``, the OUT before ``position``."""
    endpoint: int
    position: int
    gate_position: int


def is_endpoint_queued(transfer: UsbTransfer) -> bool:
    """Bulk / interrupt IN: served from a per-endpoint queue instead of through a cursor."""
    return transfer.is_in and transfer.transfer_type != TransferType.CONTROL


class CaptureWalk:
    def __init__(self, transfers: list[UsbTransfer], waiver_label_by_position: dict[int, str],
                 loop: VirtualClockEventLoop) -> None:
        self.transfers = transfers
        self.claimed_by: list[str | None] = [None] * len(transfers)
        for position, label in waiver_label_by_position.items():
            self.claimed_by[position] = label
        self.cursor_by_producer: dict[str, int] = {}
        self.divergence: Divergence | None = None
        self.refused_read_by_producer: dict[str, RefusedRead] = {}
        self.driver_closing = False
        self.requests_after_capture_end = 0
        self._last_read_time_by_endpoint: dict[int, float] = {}
        self._last_served_read_time = float("-inf")
        self._loop = loop
        self._lock = threading.Lock()
        self._first_unclaimed = 0
        self._in_positions_by_endpoint: dict[int, deque[int]] = {}
        self._gate_by_position: dict[int, int] = {}
        gate = -1
        for position, transfer in enumerate(transfers):
            if is_endpoint_queued(transfer):
                self._in_positions_by_endpoint.setdefault(transfer.endpoint, deque()).append(position)
                self._gate_by_position[position] = gate
            else:
                gate = position

    def claim(self, request: UsbTransfer, producer: str) -> UsbTransfer | None:
        """The captured transfer matching ``request`` (a control or OUT transfer); raises on divergence.
        None for a request close() makes after the last captured transfer: not compared."""
        with self._lock:
            if self.divergence is not None:
                raise CaptureDivergence()
            start = max(self.cursor_by_producer.get(producer, 0), self._advance_first_unclaimed())
            position = self._next_unclaimed(start)
            if position is None:
                if self.driver_closing:
                    self.requests_after_capture_end += 1
                    return None
                raise self._diverge(None, request, producer, "request after the last captured transfer")
            if not self.transfers[position].same_request(request):
                # Pass over unclaimed ops only if a driver task was due before the first of them.
                due_time = self._loop.earliest_due_driver_timer()
                candidate = None
                if due_time is not None and self.transfers[position].timestamp >= due_time:
                    candidate = self._next_unclaimed(position + 1)
                    while candidate is not None and not self.transfers[candidate].same_request(request):
                        candidate = self._next_unclaimed(candidate + 1)
                if candidate is None:
                    raise self._diverge(position, request, producer, "request differs from the capture")
                position = candidate
            self.claimed_by[position] = producer
            self.cursor_by_producer[producer] = position + 1
            captured = self.transfers[position]
            self._loop.advance_clock(captured.timestamp)
            return captured

    def read(self, endpoint: int, producer: str) -> UsbTransfer | None:
        """The next captured IN transfer on ``endpoint``, or None while the walk has not reached it."""
        with self._lock:
            if self.divergence is not None:
                raise CaptureDivergence()
            self._last_read_time_by_endpoint[endpoint] = time.monotonic()
            queue = self._in_positions_by_endpoint.get(endpoint)
            if queue is None:
                return None
            while queue and self.claimed_by[queue[0]] is not None:
                queue.popleft()
            if not queue:
                return None
            position = queue[0]
            if not self._is_readable(position):
                self.refused_read_by_producer[producer] = RefusedRead(
                    endpoint, position, self._gate_by_position[position])
                return None
            queue.popleft()
            self._last_served_read_time = time.monotonic()
            self.refused_read_by_producer.pop(producer, None)
            self.claimed_by[position] = producer
            captured = self.transfers[position]
            self._loop.advance_clock(captured.timestamp)
            return captured

    def has_in_transfer_in_flight(self) -> bool:
        """In the last POLLED_ENDPOINT_SECONDS (real time) an IN transfer was served, or an endpoint read then
        has one readable now."""
        with self._lock:
            now = time.monotonic()
            if now - self._last_served_read_time <= POLLED_ENDPOINT_SECONDS:
                return True
            for endpoint, read_time in self._last_read_time_by_endpoint.items():
                if now - read_time > POLLED_ENDPOINT_SECONDS:
                    continue
                for position in self._in_positions_by_endpoint.get(endpoint, ()):
                    if self.claimed_by[position] is None:
                        if self._is_readable(position):
                            return True
                        break
            return False

    def first_unclaimed_cursor_position(self) -> int | None:
        with self._lock:
            return self._next_unclaimed(self._advance_first_unclaimed())

    def record_divergence(self, position: int | None, producer: str, reason: str) -> None:
        with self._lock:
            if self.divergence is None:
                self.divergence = Divergence(position, None, producer, reason)

    def unclaimed_positions(self) -> list[int]:
        with self._lock:
            return [position for position, claimed in enumerate(self.claimed_by) if claimed is None]

    def _is_readable(self, queued_position: int) -> bool:
        gate = self._gate_by_position[queued_position]
        return gate < 0 or self.claimed_by[gate] is not None

    def _advance_first_unclaimed(self) -> int:
        while (self._first_unclaimed < len(self.transfers)
               and self.claimed_by[self._first_unclaimed] is not None):
            self._first_unclaimed += 1
        return self._first_unclaimed

    def _next_unclaimed(self, start: int) -> int | None:
        for position in range(start, len(self.transfers)):
            if self.claimed_by[position] is None and not is_endpoint_queued(self.transfers[position]):
                return position
        return None

    def _diverge(self, position: int | None, request: UsbTransfer, producer: str,
                 reason: str) -> CaptureDivergence:
        self.divergence = Divergence(position, request, producer, reason)
        return CaptureDivergence()
