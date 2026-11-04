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


@dataclass(frozen=True)
class RegisterOp:
    """One register access an op carries; ``value`` is None when the op does not carry it (a read request)."""
    access: RegisterAccess
    address: int
    value: int | None


@dataclass(frozen=True)
class Waiver:
    """value_only_register: the driver makes the op; that register's written value is not compared.
    capture_only_run: this exact contiguous run of register ops is in the capture only; not reproduced."""
    reason: WaiverReason
    c_source: str
    value_only_register: int | None = None
    capture_only_run: tuple[RegisterOp, ...] = ()

    def __post_init__(self) -> None:
        if self.value_only_register is None and not self.capture_only_run:
            raise ValueError("a Waiver needs value_only_register or capture_only_run")
        if self.value_only_register is not None and self.capture_only_run:
            raise ValueError("a Waiver takes value_only_register or capture_only_run, not both")

    def describe(self) -> str:
        if self.value_only_register is not None:
            ops_text = f"value of 0x{self.value_only_register:04x} not compared"
        else:
            ops_text = f"capture-only run of {len(self.capture_only_run)} register ops"
        return f"{self.reason.name} {ops_text} [{self.c_source}]"


class FamilyDecoder(ABC):
    @abstractmethod
    def describe(self, op: UsbOp) -> str: ...

    @abstractmethod
    def channel_change_at(self, ops: Sequence[UsbOp], position: int) -> int | None:
        """The channel, if the op at ``position`` starts Linux's channel change."""

    @abstractmethod
    def injected_frame_at(self, ops: Sequence[UsbOp], position: int) -> bytes | None:
        """The 802.11 frame, if the op at ``position`` sends one."""

    @abstractmethod
    def carries_only_80211_frames(self, op: UsbOp) -> bool: ...

    def register_ops(self, op: UsbOp) -> list[RegisterOp]:
        """The register accesses ``op`` carries, in wire order; WAIVERS match on these."""
        raise NotImplementedError(f"{type(self).__name__} does not list register ops")


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
        waived_positions: set[int] = set()
        positions_by_waiver: dict[Waiver, list[int]] = {}
        for waiver in self.WAIVERS:
            if waiver.value_only_register is not None:
                positions = self.register_write_positions(capture.ops, waiver.value_only_register)
            else:
                positions = self.capture_only_run_positions(capture.ops, waiver.capture_only_run)
                waived_positions.update(positions)
            positions_by_waiver[waiver] = positions

        thread_pool = DriverThreadPool()

        def is_driver_idle() -> bool:
            return loop.pending_executor_call_count == 0 and not replay.driver_thread_is_working()

        loop = VirtualClockEventLoop(capture.ops[0].submit_time, is_driver_idle)
        loop.set_default_executor(thread_pool)
        replay = CaptureReplay(capture.ops, waived_positions, self.is_same_request_except_waived_values, loop)
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

        waived_counts: dict[Waiver, int] = {}
        for waiver, positions in positions_by_waiver.items():
            count = 0
            for position in positions:
                if replay.reproduced_by[position] is not None:
                    count += 1
            waived_counts[waiver] = count
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

    def is_same_request_except_waived_values(self, captured: UsbOp, request: UsbOp) -> bool:
        """``captured.same_request(request)``, except a value_only_register's value: each differing byte, put
        alone into the captured op, must change only value_only_register write values in its register ops."""
        if captured.same_request(request):
            return True
        value_only_registers: set[int] = set()
        for waiver in self.WAIVERS:
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

    def register_write_positions(self, ops: list[UsbOp], address: int) -> list[int]:
        positions: list[int] = []
        for position, op in enumerate(ops):
            for register_op in self.DECODER.register_ops(op):
                if register_op.access == RegisterAccess.WRITE and register_op.address == address:
                    positions.append(position)
                    break
        return positions

    def capture_only_run_positions(self, ops: list[UsbOp], run: tuple[RegisterOp, ...]) -> list[int]:
        """Positions of every occurrence of ``run``: consecutive control / OUT ops whose register ops, joined,
        are exactly ``run``; bulk / interrupt IN ops between them are not part of it."""
        request_positions = [position for position, op in enumerate(ops) if not is_bulk_or_interrupt_in(op)]
        matched_positions: list[int] = []
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
            if tuple(run_register_ops) == run:
                matched_positions.extend(run_positions)
                start = end
            else:
                start += 1
        return matched_positions

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
                    called = f"set_channel({channel})"
                    await driver.set_channel(channel)
                elif frame is not None:
                    called = f"inject_frame({len(frame)}B)"
                    await driver.inject_frame(frame)
                else:
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
        """Move the clock to ``timestamp``; return once the driver is idle IDLE_PASSES_IN_A_ROW loop passes in a row."""
        loop.advance_clock(timestamp)
        idle_passes = 0
        while idle_passes < IDLE_PASSES_IN_A_ROW:
            await asyncio.sleep(0)
            if loop.is_driver_idle():
                idle_passes += 1
            else:
                idle_passes = 0

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
