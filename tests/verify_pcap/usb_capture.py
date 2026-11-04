"""usbmon capture reader: pcap / pcapng file -> USB ops (submit and completion paired)."""
from __future__ import annotations

import enum
import struct
from dataclasses import dataclass
from pathlib import Path

PCAPNG_SECTION_HEADER = 0x0A0D0D0A
PCAPNG_INTERFACE_DESCRIPTION = 0x00000001
PCAPNG_SIMPLE_PACKET = 0x00000003
PCAPNG_ENHANCED_PACKET = 0x00000006
PCAP_MAGIC_MICROSECONDS = 0xA1B2C3D4
PCAP_MAGIC_NANOSECONDS = 0xA1B23C4D

LINKTYPE_USB_LINUX = 189
LINKTYPE_USB_LINUX_MMAPPED = 220

USBMON_HEADER = struct.Struct("<QBBBBHbbqiiII8s")
USBMON_SECONDS_OFFSET = 16
USBMON_MMAPPED_TAIL = struct.Struct("<iiII")
ISO_DESCRIPTOR_SIZE = 16


class TransferType(enum.IntEnum):
    ISOCHRONOUS = 0
    INTERRUPT = 1
    CONTROL = 2
    BULK = 3


@dataclass(frozen=True)
class UsbOp:
    """One URB. ``data`` is the OUT payload (from the submit) or the IN payload (from the completion).
    Times are seconds since the capture's first record."""
    frame_number: int
    bus: int
    device_address: int
    endpoint: int
    transfer_type: TransferType
    setup: bytes | None
    data: bytes
    status: int
    submit_time: float
    completion_time: float

    @property
    def is_in(self) -> bool:
        return bool(self.endpoint & 0x80)

    @property
    def request_type(self) -> int:
        assert self.setup is not None
        return self.setup[0]

    @property
    def request(self) -> int:
        assert self.setup is not None
        return self.setup[1]

    @property
    def value(self) -> int:
        assert self.setup is not None
        return int.from_bytes(self.setup[2:4], "little")

    @property
    def index(self) -> int:
        assert self.setup is not None
        return int.from_bytes(self.setup[4:6], "little")

    @property
    def length(self) -> int:
        assert self.setup is not None
        return int.from_bytes(self.setup[6:8], "little")

    def same_request(self, other: UsbOp) -> bool:
        """Same endpoint, setup packet and OUT payload; IN payloads and status are not compared."""
        if self.transfer_type != other.transfer_type or self.endpoint != other.endpoint:
            return False
        if self.setup != other.setup:
            return False
        if self.is_in:
            return True
        return self.data == other.data


@dataclass(frozen=True)
class UsbmonRecord:
    frame_number: int
    urb_id: int
    event_type: str
    transfer_type: TransferType
    endpoint: int
    device_address: int
    bus: int
    setup: bytes | None
    data: bytes
    status: int
    timestamp: float


def read_usbmon_records(path: Path) -> list[UsbmonRecord]:
    raw = path.read_bytes()
    magic = struct.unpack_from("<I", raw, 0)[0]
    if magic == PCAPNG_SECTION_HEADER:
        packets = read_pcapng_packets(raw)
    else:
        packets = read_pcap_packets(raw)
    records: list[UsbmonRecord] = []
    if not packets:
        return records
    start_seconds = struct.unpack_from("<q", packets[0][1], USBMON_SECONDS_OFFSET)[0]
    for frame_number, (linktype, packet) in enumerate(packets, start=1):
        records.append(parse_usbmon_packet(frame_number, linktype, packet, start_seconds))
    return records


def read_pcap_packets(raw: bytes) -> list[tuple[int, bytes]]:
    magic_le = struct.unpack_from("<I", raw, 0)[0]
    if magic_le in (PCAP_MAGIC_MICROSECONDS, PCAP_MAGIC_NANOSECONDS):
        endian = "<"
    else:
        endian = ">"
        magic_be = struct.unpack_from(">I", raw, 0)[0]
        if magic_be not in (PCAP_MAGIC_MICROSECONDS, PCAP_MAGIC_NANOSECONDS):
            raise ValueError(f"not a pcap or pcapng file (magic 0x{magic_le:08x})")
    linktype = struct.unpack_from(endian + "I", raw, 20)[0]
    packets: list[tuple[int, bytes]] = []
    offset = 24
    while offset + 16 <= len(raw):
        captured_length = struct.unpack_from(endian + "I", raw, offset + 8)[0]
        start = offset + 16
        packets.append((linktype, raw[start:start + captured_length]))
        offset = start + captured_length
    return packets


def read_pcapng_packets(raw: bytes) -> list[tuple[int, bytes]]:
    endian = "<"
    linktypes: list[int] = []
    packets: list[tuple[int, bytes]] = []
    offset = 0
    while offset + 12 <= len(raw):
        block_type = struct.unpack_from(endian + "I", raw, offset)[0]
        if block_type == PCAPNG_SECTION_HEADER:
            byte_order_magic = struct.unpack_from("<I", raw, offset + 8)[0]
            endian = "<" if byte_order_magic == 0x1A2B3C4D else ">"
            linktypes = []
        block_length = struct.unpack_from(endian + "I", raw, offset + 4)[0]
        body = offset + 8
        if block_type == PCAPNG_INTERFACE_DESCRIPTION:
            linktypes.append(struct.unpack_from(endian + "H", raw, body)[0])
        elif block_type == PCAPNG_ENHANCED_PACKET:
            interface_id, _, _, captured_length, _ = struct.unpack_from(endian + "IIIII", raw, body)
            start = body + 20
            packets.append((linktypes[interface_id], raw[start:start + captured_length]))
        elif block_type == PCAPNG_SIMPLE_PACKET:
            original_length = struct.unpack_from(endian + "I", raw, body)[0]
            captured_length = min(original_length, block_length - 16)
            packets.append((linktypes[0], raw[body + 4:body + 4 + captured_length]))
        offset += block_length
    return packets


def parse_usbmon_packet(frame_number: int, linktype: int, packet: bytes, start_seconds: int) -> UsbmonRecord:
    """usbmon header as written by a little-endian host (every capture so far is x86)."""
    if linktype not in (LINKTYPE_USB_LINUX, LINKTYPE_USB_LINUX_MMAPPED):
        raise ValueError(f"frame {frame_number}: linktype {linktype} is not usbmon")
    (urb_id, event_type, transfer_type, endpoint, device_address, bus, setup_flag, _data_flag,
     seconds, microseconds, status, _urb_length, _captured_length, setup) = (
        USBMON_HEADER.unpack_from(packet, 0))
    data_offset = USBMON_HEADER.size
    if linktype == LINKTYPE_USB_LINUX_MMAPPED:
        descriptor_count = USBMON_MMAPPED_TAIL.unpack_from(packet, USBMON_HEADER.size)[3]
        data_offset += USBMON_MMAPPED_TAIL.size
        if transfer_type == TransferType.ISOCHRONOUS:
            data_offset += descriptor_count * ISO_DESCRIPTOR_SIZE
    return UsbmonRecord(
        frame_number=frame_number,
        urb_id=urb_id,
        event_type=chr(event_type),
        transfer_type=TransferType(transfer_type),
        endpoint=endpoint,
        device_address=device_address,
        bus=bus,
        setup=setup if setup_flag == 0 else None,
        data=packet[data_offset:],
        status=status,
        timestamp=(seconds - start_seconds) + microseconds / 1e6,
    )


def pair_submits_with_completions(records: list[UsbmonRecord]) -> tuple[list[UsbOp], list[UsbOp]]:
    """Control and OUT transfers at submit, bulk / interrupt IN at completion (their URBs are queued ahead),
    and the submits that never completed, at their submit (no IN data)."""
    submits: dict[tuple[int, int], UsbmonRecord] = {}
    ops_by_order: list[tuple[float, int, UsbOp]] = []
    for record in records:
        key = (record.bus, record.urb_id)
        if record.event_type == "S":
            submits[key] = record
            continue
        submit = submits.pop(key, None)
        if submit is None:
            continue
        if submit.endpoint & 0x80:
            data = record.data
        else:
            data = submit.data
        op = UsbOp(
            frame_number=record.frame_number,
            bus=record.bus,
            device_address=record.device_address,
            endpoint=submit.endpoint,
            transfer_type=submit.transfer_type,
            setup=submit.setup,
            data=data,
            status=record.status,
            submit_time=submit.timestamp,
            completion_time=record.timestamp,
        )
        if submit.endpoint & 0x80 and submit.transfer_type != TransferType.CONTROL:
            ops_by_order.append((record.timestamp, record.frame_number, op))
        else:
            ops_by_order.append((submit.timestamp, submit.frame_number, op))
    ops_by_order.sort(key=lambda entry: (entry[0], entry[1]))
    ops = [op for _, _, op in ops_by_order]
    incomplete_submits: list[UsbOp] = []
    for submit in submits.values():
        incomplete_submits.append(UsbOp(
            frame_number=submit.frame_number, bus=submit.bus, device_address=submit.device_address,
            endpoint=submit.endpoint, transfer_type=submit.transfer_type, setup=submit.setup,
            data=b"" if submit.endpoint & 0x80 else submit.data, status=0, submit_time=submit.timestamp,
            completion_time=submit.timestamp))
    return ops, incomplete_submits
