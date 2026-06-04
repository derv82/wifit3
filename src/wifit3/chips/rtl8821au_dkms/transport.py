"""RTL8821AU (DKMS) USB transport — Realtek rtw88-family vendor control transfers.

Every register access is one bRequest 0x05 vendor control transfer: read uses
bmRequestType 0xC0, write 0x40, with the 16-bit register address in wValue and
wIndex 0. The firmware-download page writes ride this same control path in
≤196-byte payloads (`writeN`), not bulk-OUT — bulk endpoints are only used for
RX/TX later. [SRC] include/usb_ops.h:19-31, hal/hal_hci/hal_usb.c:338-540.

The read*/write*/writeN surface matches `scripts/rtw88_pcap_replay.ReplayTransport`
so the bring-up code runs unchanged against either real hardware or the pcap.
"""
from __future__ import annotations

import usb.core

from .constants import (
    MAX_VENDOR_REQ_CMD_SIZE,
    REALTEK_USB_VENQT_CMD_IDX,
    REALTEK_USB_VENQT_CMD_REQ,
    REALTEK_USB_VENQT_READ,
    REALTEK_USB_VENQT_WRITE,
)

CTRL_TIMEOUT_MS = 500  # [SRC] include/usb_ops_linux.h:22


class RTL8821AUDkmsTransport:
    def __init__(self, dev: usb.core.Device):
        self.dev = dev

    def _read(self, addr: int, length: int) -> bytes:
        return bytes(self.dev.ctrl_transfer(
            REALTEK_USB_VENQT_READ, REALTEK_USB_VENQT_CMD_REQ,
            addr & 0xFFFF, REALTEK_USB_VENQT_CMD_IDX, length, CTRL_TIMEOUT_MS))

    def read8(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 1), "little")

    def read16(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 2), "little")

    def read32(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 4), "little")

    def writeN(self, addr: int, data: bytes) -> None:
        data = bytes(data)
        if len(data) > MAX_VENDOR_REQ_CMD_SIZE:
            raise ValueError(f"vendor write {len(data)} > {MAX_VENDOR_REQ_CMD_SIZE} B")
        self.dev.ctrl_transfer(
            REALTEK_USB_VENQT_WRITE, REALTEK_USB_VENQT_CMD_REQ,
            addr & 0xFFFF, REALTEK_USB_VENQT_CMD_IDX, data, CTRL_TIMEOUT_MS)

    def write8(self, addr: int, val: int) -> None:
        self.writeN(addr, (val & 0xFF).to_bytes(1, "little"))

    def write16(self, addr: int, val: int) -> None:
        self.writeN(addr, (val & 0xFFFF).to_bytes(2, "little"))

    def write32(self, addr: int, val: int) -> None:
        self.writeN(addr, (val & 0xFFFFFFFF).to_bytes(4, "little"))
