"""Shared verify_pcap base: the real driver, built on a fake USB device, must reproduce a usbmon capture."""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ClassVar, Iterator, Sequence

import pytest
import usb.core
import usb.util

from tests.verify_pcap.capture_replay import CaptureReplay, Divergence, is_bulk_or_interrupt_in
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


@dataclass(frozen=True)
class Waiver:
    """A named divergence from Linux: captured ops ``matches`` accepts are not reproduced."""
    label: str
    c_source: str
    matches: Callable[[UsbOp], bool]


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
        waiver_label_by_position: dict[int, str] = {}
        for position, op in enumerate(capture.ops):
            for waiver in self.WAIVERS:
                if waiver.matches(op):
                    waiver_label_by_position[position] = waiver.label
                    break

        thread_pool = DriverThreadPool()

        def is_driver_idle() -> bool:
            return thread_pool.busy_thread_count == 0 and not replay.driver_thread_is_working()

        loop = VirtualClockEventLoop(capture.ops[0].timestamp, is_driver_idle)
        loop.set_default_executor(thread_pool)
        replay = CaptureReplay(capture.ops, waiver_label_by_position, loop)
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

        summary = self.format_summary(replay, capture)
        if replay.divergence is not None:
            pytest.fail(self.format_dump(capture_path, replay, replay.divergence) + summary, pytrace=False)
        unreproduced = replay.unreproduced_positions()
        if unreproduced:
            never_reproduced = Divergence(unreproduced[0], None, "-", f"{len(unreproduced)} ops never reproduced")
            pytest.fail(self.format_dump(capture_path, replay, never_reproduced) + summary, pytrace=False)
        used_labels = set(replay.reproduced_by)
        unused = [waiver.label for waiver in self.WAIVERS if waiver.label not in used_labels]
        if unused:
            pytest.fail(f"{capture_path.name}: unused waivers: {unused}\n" + summary, pytrace=False)
        print(summary)

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
                await self.run_driver_until_idle(loop, replay.ops[position].timestamp)
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
            await self.run_driver_until_idle(loop, replay.ops[-1].timestamp)
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
        start_time = ops[0].timestamp
        waiver_labels = {waiver.label for waiver in self.WAIVERS}
        for context_position in range(max(0, position - DUMP_CONTEXT),
                                      min(len(ops), position + DUMP_CONTEXT + 1)):
            op = ops[context_position]
            marker = ">>" if context_position == position else "  "
            reproduced_by = replay.reproduced_by[context_position] or "-"
            if reproduced_by in waiver_labels:
                reproduced_by = "waived"
            lines.append(f"{marker} {context_position:5d} frame {op.frame_number:<6d} "
                         f"{op.timestamp - start_time:8.4f}s {reproduced_by:<8} "
                         f"{self.DECODER.describe(op)}")
        return "\n".join(lines) + "\n"

    def format_summary(self, replay: CaptureReplay, capture: LoadedCapture) -> str:
        reproduced_counts = Counter(replay.reproduced_by)
        waiver_labels = {waiver.label for waiver in self.WAIVERS}
        produced = sum(count for label, count in reproduced_counts.items()
                       if label is not None and label not in waiver_labels)
        lines = [f"reproduced {produced} of {len(replay.ops)} ops"
                 + "".join(f", {caller} {count}" for caller, count in reproduced_counts.items()
                           if caller is not None and caller not in waiver_labels)]
        for waiver in self.WAIVERS:
            lines.append(f"waived {reproduced_counts[waiver.label]:5d}  {waiver.label} [{waiver.c_source}]")
        for label, count in capture.ignored_counts.items():
            lines.append(f"ignored {count:5d}  {label}")
        return "\n".join(lines)
