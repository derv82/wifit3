"""RTL8814AU raw USB transport — Realtek vendor control + bulk OUT.

All register access is a single vendor request (0x05): wValue = 16-bit register
offset, wIndex = 0, data stage 1/2/4 bytes little-endian. Firmware packets go out
on bulk EP 0x02. [SRC] os_dep/.../usb_ops_linux.c usbctrl_vendorreq.

The bring-up code in :mod:`firmware` is transport-agnostic (duck-typed
read8/16/32, write8/16/32, bulk_out), so the same routines drive this PyUSB
transport on hardware and the pcap-replay transport in the M1 verifier.
"""
from __future__ import annotations

import struct

import usb.core
import usb.util

from .constants import (
    EP_BULK_OUT_FW,
    REALTEK_VENDOR_REQUEST,
    REQ_TYPE_READ,
    REQ_TYPE_WRITE,
)

_CTRL_TIMEOUT_MS = 1000
_BULK_TIMEOUT_MS = 1000


class Rtl8814auTransport:
    def __init__(self, dev: usb.core.Device):
        self.dev = dev

    # --- register access ---------------------------------------------------
    def _read(self, addr: int, length: int) -> bytes:
        return bytes(self.dev.ctrl_transfer(
            REQ_TYPE_READ, REALTEK_VENDOR_REQUEST, addr, 0, length, _CTRL_TIMEOUT_MS
        ))

    def _write(self, addr: int, data: bytes) -> None:
        self.dev.ctrl_transfer(
            REQ_TYPE_WRITE, REALTEK_VENDOR_REQUEST, addr, 0, data, _CTRL_TIMEOUT_MS
        )

    def read8(self, addr: int) -> int:
        return self._read(addr, 1)[0]

    def read16(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 2), "little")

    def read32(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 4), "little")

    def write8(self, addr: int, value: int) -> None:
        self._write(addr, bytes([value & 0xFF]))

    def write16(self, addr: int, value: int) -> None:
        self._write(addr, struct.pack("<H", value & 0xFFFF))

    def write32(self, addr: int, value: int) -> None:
        self._write(addr, struct.pack("<I", value & 0xFFFFFFFF))

    # --- bulk OUT (firmware packets) --------------------------------------
    def bulk_out(self, data: bytes) -> None:
        self.dev.write(EP_BULK_OUT_FW, data, _BULK_TIMEOUT_MS)

    def close(self) -> None:
        usb.util.dispose_resources(self.dev)
