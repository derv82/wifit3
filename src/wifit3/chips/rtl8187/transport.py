"""Vendor control-transfer transport for the RTL8187L.

Mirrors ``rtl818x_iowrite{8,16,32}_idx`` and ``rtl818x_ioread{8,16,32}_idx``
from ``driver_sources/rtl818x-source-v6.18/rtl8187/rtl8225.c:22-113``.

Every register access is a single vendor control transfer:

    bRequest      = 0x05   (RTL8187_REQ_GET_REG / RTL8187_REQ_SET_REG)
    bmRequestType = 0xC0   (vendor IN, read)
                  | 0x40   (vendor OUT, write)
    wValue        = register address (already includes the 0xFF00 CSR base
                    folded in — see :mod:`.constants`)
    wIndex        = idx & 0x03  (page select; 0 for normal regs)

Idx is only ever non-zero for the ``0xFFxx`` magic registers used by
init_hw + 8187B-specific paths. Everything inside ``rtl818x_csr`` uses
idx=0.
"""
from __future__ import annotations

import logging
from typing import Sequence, Union

import usb.core

from .constants import (
    RTL8187_REQ_GET_REG,
    RTL8187_REQ_SET_REG,
    RTL8187_REQT_READ,
    RTL8187_REQT_WRITE,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_MS = 500


class RTL8187Transport:
    """Vendor control-transfer transport for the 8187L.

    Reads + writes always go via control transfer (bRequest=0x05). Bulk
    endpoints (0x81 RX / 0x02 TX) are claimed by the driver layer, not
    here.
    """

    def __init__(self, dev: usb.core.Device, timeout_ms: int = _DEFAULT_TIMEOUT_MS):
        self.dev = dev
        self.timeout_ms = timeout_ms

    # ---- register reads -------------------------------------------------
    def read8(self, addr: int, idx: int = 0) -> int:
        return self._read(addr, 1, idx)[0]

    def read16(self, addr: int, idx: int = 0) -> int:
        b = self._read(addr, 2, idx)
        return b[0] | (b[1] << 8)

    def read32(self, addr: int, idx: int = 0) -> int:
        b = self._read(addr, 4, idx)
        return b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)

    def read_bytes(self, addr: int, length: int, idx: int = 0) -> bytes:
        return self._read(addr, length, idx)

    def _read(self, addr: int, length: int, idx: int) -> bytes:
        data = self.dev.ctrl_transfer(
            RTL8187_REQT_READ,
            RTL8187_REQ_GET_REG,
            addr,
            idx & 0x03,
            length,
            self.timeout_ms,
        )
        return bytes(data)

    # ---- register writes -----------------------------------------------
    def write8(self, addr: int, val: int, idx: int = 0) -> None:
        self._write(addr, [val & 0xFF], idx)

    def write16(self, addr: int, val: int, idx: int = 0) -> None:
        self._write(addr, [val & 0xFF, (val >> 8) & 0xFF], idx)

    def write32(self, addr: int, val: int, idx: int = 0) -> None:
        self._write(
            addr,
            [
                val & 0xFF,
                (val >> 8) & 0xFF,
                (val >> 16) & 0xFF,
                (val >> 24) & 0xFF,
            ],
            idx,
        )

    def write_block(self, addr: int, data: Union[bytes, Sequence[int]], idx: int = 0) -> None:
        self._write(addr, data, idx)

    def _write(self, addr: int, payload, idx: int) -> None:
        sent = self.dev.ctrl_transfer(
            RTL8187_REQT_WRITE,
            RTL8187_REQ_SET_REG,
            addr,
            idx & 0x03,
            payload,
            self.timeout_ms,
        )
        if sent != len(payload):
            raise IOError(
                f"control write short: sent {sent}, expected {len(payload)} "
                f"(addr=0x{addr:04x}, idx={idx})"
            )

    # ---- read/modify/write helpers -------------------------------------
    def write8_set(self, addr: int, mask: int, idx: int = 0) -> None:
        self.write8(addr, self.read8(addr, idx) | mask, idx)

    def write8_clr(self, addr: int, mask: int, idx: int = 0) -> None:
        self.write8(addr, self.read8(addr, idx) & ~mask & 0xFF, idx)

    def write32_set(self, addr: int, mask: int, idx: int = 0) -> None:
        self.write32(addr, self.read32(addr, idx) | mask, idx)

    def write32_clr(self, addr: int, mask: int, idx: int = 0) -> None:
        self.write32(addr, self.read32(addr, idx) & (~mask & 0xFFFFFFFF), idx)
