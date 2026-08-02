"""Synchronous USB transport for any rtw88-family USB chip.

Mirrors `rtw_usb_read/write` from `driver_sources/rtw88-source-v6.18/usb.c` —
every register access is a single vendor control transfer:

    bRequest      = 0x05    (RTW_USB_CMD_REQ)
    bmRequestType = 0x40    (vendor OUT, write)
                  | 0xC0    (vendor IN,  read)
    wValue        = register address (16-bit)
    wIndex        = 0x00    (RTW_USB_VENQT_CMD_IDX)

Used for register reads/writes and (on the *legacy* MCUFWDL path only) for
firmware-page upload. The modern iDDMA FW upload path used by 8822b/c and
8814a goes out via the TX bulk-OUT data path — see each chip's
`firmware.py` for that.
"""

from __future__ import annotations

import logging
from typing import Sequence

import usb.core

logger = logging.getLogger(__name__)

USB_CMD_REQ = 0x05
USB_REQTYPE_READ = 0xC0
USB_REQTYPE_WRITE = 0x40
USB_VENQT_CMD_IDX = 0x00

_DEFAULT_TIMEOUT_MS = 500


class Rtw88Transport:
    """Vendor control-transfer transport, shared across rtw88 USB chips."""

    def __init__(self, dev: usb.core.Device, timeout_ms: int = _DEFAULT_TIMEOUT_MS):
        self.dev = dev
        self.timeout_ms = timeout_ms

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
        """Multi-byte payload to a 16-bit register address.

        Used for legacy-MCUFWDL firmware-page chunks (8821a/8812a/8723d).
        """
        self._write(addr, data)

    def _write(self, addr: int, payload) -> None:
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

    # ---- helpers --------------------------------------------------------
    def write8_set(self, addr: int, mask: int) -> None:
        self.write8(addr, self.read8(addr) | mask)

    def write8_clr(self, addr: int, mask: int) -> None:
        self.write8(addr, self.read8(addr) & ~mask & 0xFF)

    def write32_set(self, addr: int, mask: int) -> None:
        self.write32(addr, self.read32(addr) | mask)

    def write32_clr(self, addr: int, mask: int) -> None:
        self.write32(addr, self.read32(addr) & (~mask & 0xFFFFFFFF))

    def write32_mask(self, addr: int, mask: int, value: int) -> None:
        """Replace bits in `mask` of `addr` with the corresponding bits of
        `value` (shifted to land in `mask`).

        Mirrors `rtw_write32_mask(addr, mask, value)` from rtw88 phy.c.
        """
        cur = self.read32(addr)
        shift = (mask & -mask).bit_length() - 1
        new = (cur & ~mask) | ((value << shift) & mask)
        self.write32(addr, new & 0xFFFFFFFF)
