"""Shared verify_pcap base: the real driver, built on a fake USB device, must reproduce a usbmon capture."""
from __future__ import annotations

import asyncio
import enum
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar, Iterator, Sequence

import pytest
import usb.core
import usb.util

from tests.verify_pcap.capture_replay import WAIVED, CaptureReplay, Divergence, is_bulk_or_interrupt_in
from tests.verify_pcap.fake_usb_device import (
    INTERFACE_CLASS_VENDOR_SPECIFIC, LINUX_ESHUTDOWN, REQUEST_TYPE_CLASS, REQUEST_TYPE_MASK, REQUEST_TYPE_STANDARD,
    FakeUsbDevice, find_chip_descriptors, parse_configuration_descriptor)
from tests.verify_pcap.usb_capture import TransferType, UsbOp, pair_submits_with_completions, read_usbmon_records
from tests.verify_pcap.virtual_clock_loop import (
    MAIN_CALLER, DriverThreadPool, VirtualClockEventLoop)
from wifit3.chips.driver import Driver
from wifit3.models import DeviceID

DUMP_CONTEXT = 10
# A driver task makes its next USB call one loop pass after the last one ends.
IDLE_PASSES_IN_A_ROW = 3

IGNORED_OTHER_DEVICES = "other devices and address 0"
IGNORED_OTHER_INTERFACES = "other interfaces (class requests, endpoints of non-vendor-class interfaces)"
IGNORED_ENUMERATION = "usbcore enumeration (standard control requests)"
IGNORED_OTHER_FUNCTION = "another function's vendor requests (not the driver's request types)"
IGNORED_RX = "RX: IN ops carrying only 802.11 frames"
IGNORED_INCOMPLETE = "submitted, never completed"
IGNORED_KILLED = "killed by the host at teardown (-ESHUTDOWN)"


class WaiverReason(enum.Enum):
    HOST_VALUE = enum.auto()
    CAPTURE_SETUP = enum.auto()
    WIFIT3_ONLY = enum.auto()


class RegisterAccess(enum.Enum):
    READ = enum.auto()
    WRITE = enum.auto()
    SET_BITS = enum.auto()
    CLEAR_BITS = enum.auto()


@dataclass(frozen=True)
class RegisterOp:
    """One register access an op carries; ``value`` is None when the op does not carry it (a read request).
    In a waiver's run, a value of None matches any value."""
    access: RegisterAccess
    address: int
    value: int | None


@dataclass(frozen=True)
class TxOpField:
    """Bits ``shift`` .. ``shift + width - 1`` of little-endian dword ``dword`` of a bulk-OUT TX op's data."""
    dword: int
    shift: int
    width: int


@dataclass(frozen=True)
class Waiver:
    """value_only_register / value_only_tx_op_field: the driver makes the op; that register's written value or that
    field of an injected frame's TX op is not compared. capture_only_run: this run of register ops is in the capture only, waived where the
    replay would stop at its first op. driver_only_run: this run of register ops, in order, is the driver's only.
    capture_paths: the captures it applies to; empty: all."""
    reason: WaiverReason
    c_source: str
    value_only_register: int | None = None
    capture_only_run: tuple[RegisterOp, ...] = ()
    value_only_tx_op_field: TxOpField | None = None
    driver_only_run: tuple[RegisterOp, ...] = ()
    capture_paths: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        forms = [self.value_only_register is not None, bool(self.capture_only_run),
                 self.value_only_tx_op_field is not None, bool(self.driver_only_run)]
        if forms.count(True) != 1:
            raise ValueError("a Waiver takes one of value_only_register, capture_only_run, "
                             "value_only_tx_op_field, driver_only_run")

    def describe(self) -> str:
        field = self.value_only_tx_op_field
        if self.value_only_register is not None:
            ops_text = f"value of 0x{self.value_only_register:04x} not compared"
        elif field is not None:
            ops_text = f"TX op dword {field.dword} bits {field.shift}..{field.shift + field.width - 1} not compared"
        elif self.driver_only_run:
            ops_text = f"driver-only run of {len(self.driver_only_run)} register ops"
        else:
            ops_text = f"capture-only run of {len(self.capture_only_run)} register ops"
        return f"{self.reason.name} {ops_text} [{self.c_source}]"


def register_ops_match(register_ops: Sequence[RegisterOp], run: Sequence[RegisterOp]) -> bool:
    """Same accesses and addresses; a run op's value is compared unless it is None."""
    if len(register_ops) != len(run):
        return False
    for register_op, run_op in zip(register_ops, run):
        if register_op.access != run_op.access or register_op.address != run_op.address:
            return False
        if run_op.value is not None and register_op.value != run_op.value:
            return False
    return True


class FamilyDecoder(ABC):
    @abstractmethod
    def describe(self, op: UsbOp) -> str: ...

    @abstractmethod
    def channel_change_at(self, ops: Sequence[UsbOp], position: int) -> int | None:
        """The channel, if the op at ``position`` starts Linux's channel change."""

    def channel_change_is_scan_at(self, ops: Sequence[UsbOp], position: int) -> bool:
        """Whether the channel change at ``position`` is an off-channel hop, made as ``set_channel(scan=True)``."""
        return False

    @abstractmethod
    def injected_frame_at(self, ops: Sequence[UsbOp], position: int) -> bytes | None:
        """The 802.11 frame, if the op at ``position`` sends one."""

    @abstractmethod
    def carries_only_80211_frames(self, op: UsbOp) -> bool: ...

    def is_driver_vendor_request(self, op: UsbOp) -> bool:
        """Whether a vendor control request is the WLAN driver's, not another function's on the same device."""
        return True

    def register_ops(self, op: UsbOp) -> list[RegisterOp]:
        """The register accesses ``op`` carries, in wire order; WAIVERS match on these."""
        raise NotImplementedError(f"{type(self).__name__} does not list register ops")

    def with_tx_op_fields(self, op: UsbOp, fields: list[TxOpField], source: UsbOp) -> UsbOp:
        """``op`` with ``source``'s value in each of ``fields``; WAIVERS compare through this."""
        raise NotImplementedError(f"{type(self).__name__} does not edit TX ops")

    def skip_capture_only_ops(self, ops: list[UsbOp], positions: set[int]) -> tuple[list[UsbOp], set[int]]:
        """The ops as if the capture-only ops at ``positions`` were never made, and the positions to waive."""
        return ops, positions


@dataclass(frozen=True)
class LoadedCapture:
    ops: list[UsbOp]
    device_descriptor: bytes
    configuration_descriptor: bytes
    device_id: DeviceID
    ignored_counts: Counter[str]


class VerifyCapture:
    """Subclass as ``TestChip(VerifyCapture)``; each path in CAPTURE_PATHS is one test case."""
    CAPTURE_PATHS: ClassVar[list[Path]]
    SUPPORTED_IDS: ClassVar[list[DeviceID]]
    DRIVER_CLASS: ClassVar[type[Driver]]
    DECODER: ClassVar[FamilyDecoder]
    WAIVERS: ClassVar[list[Waiver]] = []

    def test_capture(self, capture_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        capture = self.load_capture(capture_path)
        waivers: list[Waiver] = []
        for waiver in self.WAIVERS:
            if not waiver.capture_paths or capture_path in waiver.capture_paths:
                waivers.append(waiver)
        masked_counts: dict[Waiver, int] = {}
        occurrences_by_waiver: dict[Waiver, list[list[int]]] = {}
        capture_only_run_by_start: dict[int, list[int]] = {}
        driver_only_progress: dict[Waiver, int] = {}
        for waiver in waivers:
            if waiver.driver_only_run:
                driver_only_progress[waiver] = 0
            elif waiver.capture_only_run:
                occurrences = self.capture_only_run_occurrences(capture.ops, waiver.capture_only_run)
                occurrences_by_waiver[waiver] = occurrences
                for occurrence in occurrences:
                    capture_only_run_by_start[occurrence[0]] = occurrence
            else:
                masked_counts[waiver] = 0

        thread_pool = DriverThreadPool()

        def is_driver_idle() -> bool:
            return loop.pending_executor_call_count == 0 and not replay.driver_thread_is_working()

        loop = VirtualClockEventLoop(capture.ops[0].submit_time, is_driver_idle)
        loop.set_default_executor(thread_pool)
        def is_same_request(captured: UsbOp, request: UsbOp) -> bool:
            return self.is_same_request_counting_masked(captured, request, waivers, masked_counts)

        def is_driver_only_request(request: UsbOp, cursor_differs: bool) -> bool:
            return self.is_driver_only_request(request, cursor_differs, driver_only_progress)

        def skip_capture_only_run_at(ops: list[UsbOp], position: int) -> tuple[list[UsbOp], set[int]] | None:
            occurrence = capture_only_run_by_start.get(position)
            if occurrence is None:
                return None
            return self.DECODER.skip_capture_only_ops(ops, set(occurrence))

        replay = CaptureReplay(capture.ops, is_same_request, is_driver_only_request, skip_capture_only_run_at, loop)
        device = FakeUsbDevice(replay, capture.device_descriptor, capture.configuration_descriptor,
                               capture.device_id.bus or 0, capture.device_id.address or 0)

        def find_fake_device(find_all: bool = False, backend: Any = None,
                             custom_match: Any = None, **match: Any) -> FakeUsbDevice | None | Iterator[FakeUsbDevice]:
            matches = (match.get("idVendor", device.idVendor) == device.idVendor
                       and match.get("idProduct", device.idProduct) == device.idProduct)
            if find_all:
                return iter([device] if matches else [])
            return device if matches else None

        def do_nothing(*args: Any) -> None:
            return None

        monkeypatch.setattr(usb.core, "find", find_fake_device)
        monkeypatch.setattr(usb.util, "claim_interface", do_nothing)
        monkeypatch.setattr(usb.util, "release_interface", do_nothing)
        monkeypatch.setattr(usb.util, "dispose_resources", do_nothing)
        try:
            main_task = loop.create_task(self.run_driver(replay, device, capture.device_id),
                                         name=MAIN_CALLER)
            loop.run_until_complete(main_task)
        finally:
            for task in asyncio.all_tasks(loop):
                task.cancel()
            loop.run_until_complete(asyncio.sleep(0))
            loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()

        waived_counts = dict(masked_counts)
        for waiver, occurrences in occurrences_by_waiver.items():
            count = 0
            for occurrence in occurrences:
                for position in occurrence:
                    if replay.reproduced_by[position] == WAIVED:
                        count += 1
            waived_counts[waiver] = count
        waived_counts.update(driver_only_progress)
        summary = self.format_summary(replay, capture, waived_counts)
        if replay.divergence is not None:
            pytest.fail(self.format_dump(capture_path, replay, replay.divergence) + summary, pytrace=False)
        unreproduced = replay.unreproduced_positions()
        if unreproduced:
            never_reproduced = Divergence(unreproduced[0], None, "-", f"{len(unreproduced)} ops never reproduced")
            pytest.fail(self.format_dump(capture_path, replay, never_reproduced) + summary, pytrace=False)
        unused = [waiver.describe() for waiver, count in waived_counts.items() if count == 0]
        if unused:
            pytest.fail(f"{capture_path.name}: unused waivers: {unused}\n" + summary, pytrace=False)
        print(summary)

    def is_same_request_counting_masked(self, captured: UsbOp, request: UsbOp, waivers: list[Waiver],
                                        masked_counts: dict[Waiver, int]) -> bool:
        """``is_same_request_except_waived_values``; counts, per waiver in ``masked_counts``, the matches that differ
        without it."""
        if captured.same_request(request):
            return True
        if not self.is_same_request_except_waived_values(captured, request, waivers):
            return False
        for waiver in masked_counts:
            other_waivers = [other for other in waivers if other is not waiver]
            if not self.is_same_request_except_waived_values(captured, request, other_waivers):
                masked_counts[waiver] += 1
        return True

    def is_same_request_except_waived_values(self, captured: UsbOp, request: UsbOp, waivers: list[Waiver]) -> bool:
        """``captured.same_request(request)``, except a value_only_register's value: each differing byte, put
        alone into the captured op, must change only value_only_register write values in its register ops."""
        if captured.same_request(request):
            return True
        tx_op_fields: list[TxOpField] = []
        for waiver in waivers:
            if waiver.value_only_tx_op_field is not None:
                tx_op_fields.append(waiver.value_only_tx_op_field)
        if (tx_op_fields and self.DECODER.injected_frame_at([captured], 0) is not None
                and self.DECODER.with_tx_op_fields(captured, tx_op_fields, request).same_request(request)):
            return True
        value_only_registers: set[int] = set()
        for waiver in waivers:
            if waiver.value_only_register is not None:
                value_only_registers.add(waiver.value_only_register)
        if not value_only_registers:
            return False
        if (captured.transfer_type != request.transfer_type or captured.endpoint != request.endpoint
                or captured.setup != request.setup or len(captured.data) != len(request.data)):
            return False
        captured_register_ops = self.DECODER.register_ops(captured)
        for byte_position in range(len(captured.data)):
            if captured.data[byte_position] == request.data[byte_position]:
                continue
            one_byte_changed = replace(captured, data=captured.data[:byte_position]
                                       + request.data[byte_position:byte_position + 1]
                                       + captured.data[byte_position + 1:])
            changed_register_ops = self.DECODER.register_ops(one_byte_changed)
            if changed_register_ops == captured_register_ops:
                return False
            if len(changed_register_ops) != len(captured_register_ops):
                return False
            for captured_op, changed_op in zip(captured_register_ops, changed_register_ops):
                if captured_op == changed_op:
                    continue
                if (captured_op.access != RegisterAccess.WRITE or changed_op.access != RegisterAccess.WRITE
                        or captured_op.address != changed_op.address
                        or captured_op.address not in value_only_registers):
                    return False
        return True

    def is_driver_only_request(self, request: UsbOp, cursor_differs: bool,
                               driver_only_progress: dict[Waiver, int]) -> bool:
        """``request``'s register ops are the next ones of a driver_only_run; advances its progress. A run starts
        only where the capture differs; once started, it claims the driver's next requests first."""
        if not driver_only_progress:
            return False
        register_ops = self.DECODER.register_ops(request)
        if not register_ops:
            return False
        for waiver, progress in driver_only_progress.items():
            if progress == 0 and not cursor_differs:
                continue
            if register_ops_match(register_ops, waiver.driver_only_run[progress:progress + len(register_ops)]):
                driver_only_progress[waiver] = progress + len(register_ops)
                return True
        return False

    def capture_only_run_occurrences(self, ops: list[UsbOp], run: tuple[RegisterOp, ...]) -> list[list[int]]:
        """The positions of each occurrence of ``run``: consecutive control / OUT ops whose register ops, joined,
        are exactly ``run``; bulk / interrupt IN ops between them are not part of it."""
        request_positions = [position for position, op in enumerate(ops) if not is_bulk_or_interrupt_in(op)]
        occurrences: list[list[int]] = []
        start = 0
        while start < len(request_positions):
            run_positions: list[int] = []
            run_register_ops: list[RegisterOp] = []
            end = start
            while len(run_register_ops) < len(run) and end < len(request_positions):
                register_ops = self.DECODER.register_ops(ops[request_positions[end]])
                if not register_ops:
                    break
                run_register_ops.extend(register_ops)
                run_positions.append(request_positions[end])
                end += 1
            if register_ops_match(run_register_ops, run):
                occurrences.append(run_positions)
                start = end
            else:
                start += 1
        return occurrences

    async def run_driver(self, replay: CaptureReplay, device: FakeUsbDevice,
                         device_id: DeviceID) -> None:
        loop = asyncio.get_running_loop()
        assert isinstance(loop, VirtualClockEventLoop)
        driver = self.DRIVER_CLASS.from_usb_device(device, device_id)  # type: ignore[arg-type]
        try:
            await driver.connect()
            while replay.divergence is None:
                position = replay.next_unreproduced_position()
                if position is None:
                    break
                await self.run_driver_until_idle(loop, replay.ops[position].submit_time)
                if replay.next_unreproduced_position() != position:
                    continue
                channel = self.DECODER.channel_change_at(replay.ops, position)
                frame = self.DECODER.injected_frame_at(replay.ops, position)
                if channel is not None:
                    scan = self.DECODER.channel_change_is_scan_at(replay.ops, position)
                    called = f"set_channel({channel}, scan={scan})"
                    await driver.set_channel(channel, scan)
                elif frame is not None:
                    called = f"inject_frame({len(frame)}B)"
                    await driver.inject_frame(frame)
                else:
                    due_time = loop.pending_driver_timer_due_within_tolerance_of(replay.ops[position].submit_time)
                    if due_time is not None:
                        await self.run_driver_until_idle(loop, due_time)
                        continue
                    replay.waive_capture_only_run_at(position)
                    if replay.next_unreproduced_position() != position:
                        continue
                    replay.record_divergence(position, MAIN_CALLER,
                                             "the driver did not make this op, and it starts no channel change or TX")
                    break
                if replay.divergence is None and replay.reproduced_by[position] is None:
                    replay.record_divergence(position, MAIN_CALLER,
                                             f"called {called}; the driver did not make this op")
            await self.run_driver_until_idle(loop, replay.ops[-1].completion_time)
        except Exception as error:  # noqa: BLE001
            reason = f"driver raised {type(error).__name__}: {error}"
            for caller, refused_read in list(replay.refused_read_by_caller.items()):
                reason += (f"; {caller} read 0x{refused_read.endpoint:02x} refused: replay {refused_read.position}"
                           f" is readable only after replay {refused_read.gate_position}")
            replay.record_divergence(replay.next_unreproduced_position(), MAIN_CALLER, reason)
        finally:
            replay.driver_closing = True
            try:
                await driver.close()
            except Exception as error:  # noqa: BLE001
                replay.record_divergence(None, MAIN_CALLER, f"close() raised {type(error).__name__}: {error}")

    @staticmethod
    async def run_driver_until_idle(loop: VirtualClockEventLoop, timestamp: float) -> None:
        """Idle, sleep to ``timestamp`` (driver task timers due before it fire at their own time), idle: idle is
        IDLE_PASSES_IN_A_ROW loop passes in a row."""
        async def wait_until_idle() -> None:
            idle_passes = 0
            while idle_passes < IDLE_PASSES_IN_A_ROW:
                await asyncio.sleep(0)
                if loop.is_driver_idle():
                    idle_passes += 1
                else:
                    idle_passes = 0

        await wait_until_idle()
        if timestamp > loop.time():
            await asyncio.sleep(timestamp - loop.time())
        await wait_until_idle()

    def load_capture(self, capture_path: Path) -> LoadedCapture:
        all_ops, incomplete_count = pair_submits_with_completions(read_usbmon_records(capture_path))
        found = find_chip_descriptors(all_ops, self.SUPPORTED_IDS)
        if found is None:
            pytest.fail(f"{capture_path.name}: no plug-in of a SUPPORTED_IDS device "
                        "(device + configuration descriptor) in the capture", pytrace=False)
        device_id, device_descriptor, configuration_descriptor = found
        other_interface_endpoints: set[int] = set()
        for interface in parse_configuration_descriptor(configuration_descriptor):
            if interface.bInterfaceClass != INTERFACE_CLASS_VENDOR_SPECIFIC:
                for endpoint in interface:
                    other_interface_endpoints.add(endpoint.bEndpointAddress)

        ignored_counts: Counter[str] = Counter()
        ignored_counts[IGNORED_INCOMPLETE] = incomplete_count
        driver_ops: list[UsbOp] = []
        for op in all_ops:
            if (op.bus, op.device_address) != (device_id.bus, device_id.address):
                ignored_counts[IGNORED_OTHER_DEVICES] += 1
            elif (op.endpoint in other_interface_endpoints
                  or (op.transfer_type == TransferType.CONTROL
                      and op.request_type & REQUEST_TYPE_MASK == REQUEST_TYPE_CLASS)):
                ignored_counts[IGNORED_OTHER_INTERFACES] += 1
            elif op.status == -LINUX_ESHUTDOWN:
                ignored_counts[IGNORED_KILLED] += 1
            elif (op.transfer_type == TransferType.CONTROL
                  and op.request_type & REQUEST_TYPE_MASK == REQUEST_TYPE_STANDARD):
                ignored_counts[IGNORED_ENUMERATION] += 1
            elif op.transfer_type == TransferType.CONTROL and not self.DECODER.is_driver_vendor_request(op):
                ignored_counts[IGNORED_OTHER_FUNCTION] += 1
            elif is_bulk_or_interrupt_in(op) and self.DECODER.carries_only_80211_frames(op):
                ignored_counts[IGNORED_RX] += 1
            else:
                driver_ops.append(op)
        return LoadedCapture(driver_ops, device_descriptor, configuration_descriptor, device_id,
                             ignored_counts)

    def format_dump(self, capture_path: Path, replay: CaptureReplay, divergence: Divergence) -> str:
        ops = replay.ops
        lines = [f"{capture_path.name}: {divergence.reason} (caller {divergence.caller})"]
        if divergence.request is not None:
            lines.append(f"driver sent: {self.DECODER.describe(divergence.request)}")
        if divergence.position is None:
            position = len(ops)
        else:
            position = divergence.position
            lines.append(f"capture has: {self.DECODER.describe(ops[position])}")
        start_time = ops[0].submit_time
        for context_position in range(max(0, position - DUMP_CONTEXT),
                                      min(len(ops), position + DUMP_CONTEXT + 1)):
            op = ops[context_position]
            marker = ">>" if context_position == position else "  "
            reproduced_by = replay.reproduced_by[context_position] or "-"
            lines.append(f"{marker} {context_position:5d} frame {op.frame_number:<6d} "
                         f"S {op.submit_time - start_time:8.4f}s C {op.completion_time - start_time:8.4f}s {reproduced_by:<8} "
                         f"{self.DECODER.describe(op)}")
        return "\n".join(lines) + "\n"

    def format_summary(self, replay: CaptureReplay, capture: LoadedCapture,
                       waived_counts: dict[Waiver, int]) -> str:
        reproduced_counts = Counter(replay.reproduced_by)
        produced = sum(count for caller, count in reproduced_counts.items()
                       if caller is not None and caller != WAIVED)
        lines = [f"reproduced {produced} of {len(replay.ops)} ops"
                 + "".join(f", {caller} {count}" for caller, count in reproduced_counts.items()
                           if caller is not None and caller != WAIVED)]
        for waiver, count in waived_counts.items():
            lines.append(f"waived {count:5d}  {waiver.describe()}")
        for label, count in capture.ignored_counts.items():
            lines.append(f"ignored {count:5d}  {label}")
        return "\n".join(lines)
