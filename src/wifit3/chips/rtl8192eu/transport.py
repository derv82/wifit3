"""RTL8192EU USB transport — vendor-control register reads/writes.

Mirrors `rtl8xxxu_read8/16/32` and `rtl8xxxu_write8/16/32` from
`driver_sources/rtl8xxxu-source-v6.19/core.c` (lines 611-826). All MAC
register access is a single vendor control transfer:

    bRequest      = 0x05   (REALTEK_USB_CMD_REQ)
    bmRequestType = 0x40   (vendor OUT, write)
                  | 0xC0   (vendor IN,  read)
    wValue        = register address (16-bit)
    wIndex        = 0x00

This wire format predates the kernel `rtw88` family but is identical to
it; we intentionally do NOT import from ``chips/rtw88_base`` so the
8188e (rtl8xxxu) family stays self-contained.
"""
from __future__ import annotations

import logging
import threading
from typing import Sequence

import usb.core

from .constants import (
    USB_CMD_REQ,
    USB_CONTROL_TIMEOUT_MS,
    USB_REQTYPE_READ,
    USB_REQTYPE_WRITE,
    USB_VENQT_CMD_IDX,
)

logger = logging.getLogger(__name__)


class RTL8192EUTransport:
    """Vendor control-transfer transport for the RTL8192EU.

    Each individual `ctrl_transfer` is serialised by an instance lock, so
    concurrent callers never interleave transfers. Compound read-modify-write
    helpers (`write8_set`, `write16_clr`, ...) are not atomic: the lock is
    released between their read and their write.
    """

    def __init__(self, dev: usb.core.Device, timeout_ms: int = USB_CONTROL_TIMEOUT_MS):
        self.dev = dev
        self.timeout_ms = timeout_ms
        self._lock = threading.RLock()

    # ---- register reads -------------------------------------------------
    def read8(self, addr: int) -> int:
        return self._read(addr, 1)[0]

    def read16(self, addr: int) -> int:
        b = self._read(addr, 2)
        return b[0] | (b[1] << 8)

    def read32(self, addr: int) -> int:
        b = self._read(addr, 4)
        return b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)

    def _read(self, addr: int, length: int) -> bytes:
        with self._lock:
            data = self.dev.ctrl_transfer(
                USB_REQTYPE_READ,
                USB_CMD_REQ,
                addr,
                USB_VENQT_CMD_IDX,
                length,
                self.timeout_ms,
            )
        return bytes(data)

    # ---- register writes -----------------------------------------------
    def write8(self, addr: int, val: int) -> None:
        self._write(addr, [val & 0xFF])

    def write16(self, addr: int, val: int) -> None:
        self._write(addr, [val & 0xFF, (val >> 8) & 0xFF])

    def write32(self, addr: int, val: int) -> None:
        self._write(
            addr,
            [
                val & 0xFF,
                (val >> 8) & 0xFF,
                (val >> 16) & 0xFF,
                (val >> 24) & 0xFF,
            ],
        )

    def write_block(self, addr: int, data: bytes | Sequence[int]) -> None:
        """Multi-byte payload at a single 16-bit register address.

        Used for FW page chunks (`rtl8xxxu_writeN`, core.c:826).
        """
        self._write(addr, data)

    def _write(self, addr: int, payload) -> None:
        with self._lock:
            sent = self.dev.ctrl_transfer(
                USB_REQTYPE_WRITE,
                USB_CMD_REQ,
                addr,
                USB_VENQT_CMD_IDX,
                payload,
                self.timeout_ms,
            )
        if sent != len(payload):
            raise IOError(
                f"control write short: sent {sent}, expected {len(payload)} "
                f"(addr=0x{addr:04x})"
            )

    # ---- read-modify-write helpers --------------------------------------
    def write8_set(self, addr: int, mask: int) -> None:
        self.write8(addr, self.read8(addr) | mask)

    def write8_clr(self, addr: int, mask: int) -> None:
        self.write8(addr, self.read8(addr) & ~mask & 0xFF)

    def write16_set(self, addr: int, mask: int) -> None:
        self.write16(addr, self.read16(addr) | mask)

    def write16_clr(self, addr: int, mask: int) -> None:
        self.write16(addr, self.read16(addr) & ~mask & 0xFFFF)

    def write32_set(self, addr: int, mask: int) -> None:
        self.write32(addr, self.read32(addr) | mask)

    def write32_clr(self, addr: int, mask: int) -> None:
        self.write32(addr, self.read32(addr) & (~mask & 0xFFFFFFFF))
