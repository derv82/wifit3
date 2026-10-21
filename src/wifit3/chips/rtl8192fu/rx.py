from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator

import usb.core


@dataclass(frozen=True)
class RxDescriptor:
    packet_length: int
    crc_error: bool
    icv_error: bool
    driver_info_size: int
    shift: int
    phy_stats: bool
    report: bool
    rate: int

    @property
    def frame_offset(self) -> int:
        return 24 + self.driver_info_size + self.shift


def parse_descriptor(buffer: bytes, offset: int = 0) -> RxDescriptor:
    if len(buffer) - offset < 24:
        raise ValueError("RTL8192FU RX descriptor requires 24 bytes")
    word0, _, word2, word3, _, _ = struct.unpack_from("<6I", buffer, offset)
    return RxDescriptor(
        packet_length=word0 & 0x3FFF,
        crc_error=bool(word0 & (1 << 14)),
        icv_error=bool(word0 & (1 << 15)),
        driver_info_size=((word0 >> 16) & 0x0F) * 8,
        shift=(word0 >> 24) & 0x03,
        phy_stats=bool(word0 & (1 << 26)),
        report=bool(word2 & (1 << 28)),
        rate=word3 & 0x7F,
    )


def iter_frames(buffer: bytes) -> Iterator[tuple[bytes, int]]:
    offset = 0
    while offset + 24 <= len(buffer):
        descriptor = parse_descriptor(buffer, offset)
        if descriptor.packet_length == 0:
            return
        total = descriptor.frame_offset + descriptor.packet_length
        if offset + total > len(buffer):
            return
        if not descriptor.report and not descriptor.crc_error and not descriptor.icv_error:
            frame_start = offset + descriptor.frame_offset
            frame = bytes(buffer[frame_start:frame_start + descriptor.packet_length])
            yield frame, -50
        next_offset = offset + ((total + 7) & ~7)
        if next_offset <= offset:
            return
        offset = next_offset


def read_burst(dev: usb.core.Device, endpoint: int, timeout_ms: int = 100) -> bytes | None:
    try:
        return bytes(dev.read(endpoint, 65536, timeout_ms))
    except usb.core.USBError as error:
        errno = getattr(error, "errno", None)
        if errno in (110, 10060) or "timeout" in str(error).lower():
            return None
        raise
