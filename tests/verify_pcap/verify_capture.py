"""Shared verify_pcap base: the real driver, built on a fake USB device, must reproduce a usbmon capture."""
from __future__ import annotations

import asyncio
import struct
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, ClassVar, Iterator, Sequence

import pytest
import usb.core
import usb.util

from tests.verify_pcap.capture_walk import CaptureWalk, Divergence, is_endpoint_queued
from tests.verify_pcap.fake_usb_device import (
    LINUX_ESHUTDOWN, REQUEST_TYPE_MASK, REQUEST_TYPE_STANDARD, FakeUsbDevice)
from tests.verify_pcap.usb_capture import TransferType, UsbTransfer, pair_transfers, read_usbmon_records
from tests.verify_pcap.virtual_clock_loop import (
    MAIN_PRODUCER, SELECT_TIMEOUT_SECONDS, ProducerTrackingExecutor, VirtualClockEventLoop)
from wifit3.chips.driver import Driver
from wifit3.models import DeviceID

DUMP_CONTEXT = 10
QUIET_ROUNDS = 3
GET_DESCRIPTOR = 6
DESCRIPTOR_TYPE_DEVICE = 1
DESCRIPTOR_TYPE_CONFIGURATION = 2

EXCLUDED_OTHER_DEVICES = "other devices and address 0"
EXCLUDED_ENUMERATION = "usbcore enumeration (standard control requests)"
EXCLUDED_RX = "RX: IN transfers carrying only 802.11 frames"
EXCLUDED_INCOMPLETE = "submitted, never completed"
EXCLUDED_KILLED = "killed by the host at teardown (-ESHUTDOWN)"


@dataclass(frozen=True)
class SetChannelTrigger:
    channel: int


@dataclass(frozen=True)
class InjectFrameTrigger:
    frame: bytes


Trigger = SetChannelTrigger | InjectFrameTrigger


@dataclass(frozen=True)
class Waiver:
    """A named divergence from Linux: captured transfers ``matches`` accepts are not reproduced."""
    label: str
    c_source: str
    matches: Callable[[UsbTransfer], bool]


class FamilyDecoder(ABC):
    @abstractmethod
    def describe(self, transfer: UsbTransfer) -> str: ...

    @abstractmethod
    def find_trigger(self, transfers: Sequence[UsbTransfer], position: int) -> Trigger | None:
        """The entry point the capture's transfer at ``position`` starts, or None."""

    @abstractmethod
    def carries_only_80211_frames(self, transfer: UsbTransfer) -> bool: ...


@dataclass(frozen=True)
class LoadedCapture:
    walk_transfers: list[UsbTransfer]
    device_descriptor: bytes
    configuration_descriptor: bytes
    id_entry: DeviceID
    excluded_counts: Counter[str]


class VerifyCapture:
    """Subclass as ``TestChip(VerifyCapture)``; each path in CAPTURE_PATHS is one test case."""
    CAPTURE_PATHS: ClassVar[list[Path]]
    SUPPORTED_IDS: ClassVar[list[DeviceID]]
    DRIVER_CLASS: ClassVar[type[Driver]]
    DECODER: ClassVar[FamilyDecoder]
    WAIVERS: ClassVar[list[Waiver]] = []

    def test_capture(self, capture_path: Path) -> None:
        capture = self.load_capture(capture_path)
        waiver_label_by_position: dict[int, str] = {}
        for position, transfer in enumerate(capture.walk_transfers):
            for waiver in self.WAIVERS:
                if waiver.matches(transfer):
                    waiver_label_by_position[position] = waiver.label
                    break

        executor = ProducerTrackingExecutor()

        def is_driver_idle() -> bool:
            return executor.running_count == 0 and not walk.has_in_transfer_in_flight()

        loop = VirtualClockEventLoop(capture.walk_transfers[0].timestamp, is_driver_idle)
        loop.set_default_executor(executor)
        walk = CaptureWalk(capture.walk_transfers, waiver_label_by_position, loop)
        device = FakeUsbDevice(walk, capture.device_descriptor, capture.configuration_descriptor,
                               capture.id_entry.bus or 0, capture.id_entry.address or 0)

        def find_fake_device(find_all: bool = False, backend: Any = None,
                             custom_match: Any = None, **match: Any) -> FakeUsbDevice | None | Iterator[FakeUsbDevice]:
            matches = (match.get("idVendor", device.idVendor) == device.idVendor
                       and match.get("idProduct", device.idProduct) == device.idProduct)
            if find_all:
                return iter([device] if matches else [])
            return device if matches else None

        def accept_claim_or_release(device: FakeUsbDevice, interface: Any) -> None:
            return None

        def accept_dispose_resources(device: FakeUsbDevice) -> None:
            return None

        original_find = usb.core.find
        original_claim_interface = usb.util.claim_interface
        original_release_interface = usb.util.release_interface
        original_dispose_resources = usb.util.dispose_resources
        usb.core.find = find_fake_device
        usb.util.claim_interface = accept_claim_or_release
        usb.util.release_interface = accept_claim_or_release
        usb.util.dispose_resources = accept_dispose_resources
        try:
            main_task = loop.create_task(self.drive_driver(walk, device, executor, capture.id_entry),
                                         name=MAIN_PRODUCER)
            loop.run_until_complete(main_task)
        finally:
            usb.core.find = original_find
            usb.util.claim_interface = original_claim_interface
            usb.util.release_interface = original_release_interface
            usb.util.dispose_resources = original_dispose_resources
            for task in asyncio.all_tasks(loop):
                task.cancel()
            loop.run_until_complete(asyncio.sleep(0))
            loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()

        summary = self.format_summary(walk, capture)
        if walk.divergence is not None:
            pytest.fail(self.format_dump(capture_path, walk, walk.divergence) + summary, pytrace=False)
        unclaimed = walk.unclaimed_positions()
        if unclaimed:
            never_claimed = Divergence(unclaimed[0], None, "-", f"{len(unclaimed)} transfers never claimed")
            pytest.fail(self.format_dump(capture_path, walk, never_claimed) + summary, pytrace=False)
        used_labels = set(walk.claimed_by)
        unused = [waiver.label for waiver in self.WAIVERS if waiver.label not in used_labels]
        if unused:
            pytest.fail(f"{capture_path.name}: unused waivers: {unused}\n" + summary, pytrace=False)
        print(summary)

    async def drive_driver(self, walk: CaptureWalk, device: FakeUsbDevice,
                           executor: ProducerTrackingExecutor, id_entry: DeviceID) -> None:
        loop = asyncio.get_running_loop()
        assert isinstance(loop, VirtualClockEventLoop)
        driver = self.DRIVER_CLASS.from_usb_device(device, id_entry)  # type: ignore[arg-type]
        try:
            await driver.connect()
            while walk.divergence is None:
                position = walk.first_unclaimed_cursor_position()
                if position is None:
                    break
                await self.run_due_driver_tasks(loop, executor, walk.transfers[position].timestamp)
                if walk.first_unclaimed_cursor_position() != position:
                    continue
                trigger = self.DECODER.find_trigger(walk.transfers, position)
                if trigger is None:
                    walk.record_divergence(position, MAIN_PRODUCER,
                                           "no producer claimed this transfer and it starts no trigger")
                    break
                if isinstance(trigger, SetChannelTrigger):
                    await driver.set_channel(trigger.channel)
                else:
                    await driver.inject_frame(trigger.frame)
                if walk.divergence is None and walk.claimed_by[position] is None:
                    walk.record_divergence(position, MAIN_PRODUCER,
                                           f"{trigger} fired; the driver did not produce this transfer")
            await self.run_due_driver_tasks(loop, executor, walk.transfers[-1].timestamp)
            while not loop.is_driver_idle():
                await asyncio.sleep(SELECT_TIMEOUT_SECONDS)
        except Exception as error:  # noqa: BLE001
            reason = f"driver raised {type(error).__name__}: {error}"
            for producer, refused_read in list(walk.refused_read_by_producer.items()):
                reason += (f"; {producer} read 0x{refused_read.endpoint:02x} refused: walk {refused_read.position}"
                           f" is readable only after walk {refused_read.gate_position}")
            walk.record_divergence(walk.first_unclaimed_cursor_position(), MAIN_PRODUCER, reason)
        finally:
            walk.driver_closing = True
            try:
                await driver.close()
            except Exception as error:  # noqa: BLE001
                if walk.requests_after_capture_end == 0:
                    walk.record_divergence(None, MAIN_PRODUCER,
                                           f"close() raised {type(error).__name__}: {error}")

    @staticmethod
    async def run_due_driver_tasks(loop: VirtualClockEventLoop, executor: ProducerTrackingExecutor,
                                   timestamp: float) -> None:
        """Move the clock to ``timestamp`` and yield until no driver work is running."""
        loop.advance_clock(timestamp)
        quiet_rounds = 0
        while quiet_rounds < QUIET_ROUNDS:
            await asyncio.sleep(0)
            if executor.running_count == 0:
                quiet_rounds += 1
            else:
                quiet_rounds = 0

    def load_capture(self, capture_path: Path) -> LoadedCapture:
        transfers, incomplete_count = pair_transfers(read_usbmon_records(capture_path))
        id_entry: DeviceID | None = None
        device_descriptor = b""
        configuration_descriptor = b""
        for transfer in transfers:
            if transfer.transfer_type != TransferType.CONTROL or transfer.setup is None:
                continue
            if transfer.request != GET_DESCRIPTOR or not transfer.is_in or transfer.device_address == 0:
                continue
            descriptor_type = transfer.value >> 8
            if descriptor_type == DESCRIPTOR_TYPE_DEVICE and len(transfer.data) >= 18:
                vid, pid = struct.unpack_from("<HH", transfer.data, 8)
                for supported in self.SUPPORTED_IDS:
                    if (supported.vid, supported.pid) == (vid, pid):
                        id_entry = replace(supported, bus=transfer.bus, address=transfer.device_address)
                        device_descriptor = transfer.data
            elif (descriptor_type == DESCRIPTOR_TYPE_CONFIGURATION and id_entry is not None
                  and (transfer.bus, transfer.device_address) == (id_entry.bus, id_entry.address)
                  and len(transfer.data) == struct.unpack_from("<H", transfer.data, 2)[0]):
                configuration_descriptor = transfer.data
        if id_entry is None or not configuration_descriptor:
            pytest.fail(f"{capture_path.name}: no enumeration of a SUPPORTED_IDS device "
                        "(device + configuration descriptor) in the capture", pytrace=False)

        excluded_counts: Counter[str] = Counter()
        excluded_counts[EXCLUDED_INCOMPLETE] = incomplete_count
        walk_transfers: list[UsbTransfer] = []
        for transfer in transfers:
            if (transfer.bus, transfer.device_address) != (id_entry.bus, id_entry.address):
                excluded_counts[EXCLUDED_OTHER_DEVICES] += 1
            elif transfer.status == -LINUX_ESHUTDOWN:
                excluded_counts[EXCLUDED_KILLED] += 1
            elif (transfer.transfer_type == TransferType.CONTROL
                  and transfer.request_type & REQUEST_TYPE_MASK == REQUEST_TYPE_STANDARD):
                excluded_counts[EXCLUDED_ENUMERATION] += 1
            elif is_endpoint_queued(transfer) and self.DECODER.carries_only_80211_frames(transfer):
                excluded_counts[EXCLUDED_RX] += 1
            else:
                walk_transfers.append(transfer)
        return LoadedCapture(walk_transfers, device_descriptor, configuration_descriptor, id_entry,
                             excluded_counts)

    def format_dump(self, capture_path: Path, walk: CaptureWalk, divergence: Divergence) -> str:
        transfers = walk.transfers
        lines = [f"{capture_path.name}: {divergence.reason} (producer {divergence.producer})"]
        if divergence.request is not None:
            lines.append(f"driver sent: {self.DECODER.describe(divergence.request)}")
        if divergence.position is None:
            position = len(transfers)
        else:
            position = divergence.position
            lines.append(f"capture has: {self.DECODER.describe(transfers[position])}")
        start_time = transfers[0].timestamp
        waiver_labels = {waiver.label for waiver in self.WAIVERS}
        for context_position in range(max(0, position - DUMP_CONTEXT),
                                      min(len(transfers), position + DUMP_CONTEXT + 1)):
            transfer = transfers[context_position]
            marker = ">>" if context_position == position else "  "
            claimed_by = walk.claimed_by[context_position] or "-"
            if claimed_by in waiver_labels:
                claimed_by = "waived"
            lines.append(f"{marker} {context_position:5d} frame {transfer.frame_number:<6d} "
                         f"{transfer.timestamp - start_time:8.4f}s {claimed_by:<8} "
                         f"{self.DECODER.describe(transfer)}")
        return "\n".join(lines) + "\n"

    def format_summary(self, walk: CaptureWalk, capture: LoadedCapture) -> str:
        claimed_counts = Counter(walk.claimed_by)
        waiver_labels = {waiver.label for waiver in self.WAIVERS}
        produced = sum(count for label, count in claimed_counts.items()
                       if label is not None and label not in waiver_labels)
        lines = [f"claimed {produced} of {len(walk.transfers)} transfers"
                 + "".join(f", {producer} {count}" for producer, count in claimed_counts.items()
                           if producer is not None and producer not in waiver_labels)]
        for waiver in self.WAIVERS:
            lines.append(f"waived {claimed_counts[waiver.label]:5d}  {waiver.label} [{waiver.c_source}]")
        for label, count in capture.excluded_counts.items():
            lines.append(f"excluded {count:5d}  {label}")
        if walk.requests_after_capture_end:
            lines.append(f"close(): {walk.requests_after_capture_end} requests after the last captured transfer, "
                         "not compared")
        return "\n".join(lines)
