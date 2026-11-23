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

import errno
import logging

import usb.core

from wifit3.errors import is_device_gone

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
        self.removed = False
        self._io_dmabuf = bytearray(4)   # priv->io_dmabuf: a failed read returns what is left in it

    # ---- register reads -------------------------------------------------
    def read8(self, addr: int, idx: int = 0) -> int:
        return self._read(addr, 1, idx)[0]

    def read16(self, addr: int, idx: int = 0) -> int:
        b = self._read(addr, 2, idx)
        return b[0] | (b[1] << 8)

    def read32(self, addr: int, idx: int = 0) -> int:
        b = self._read(addr, 4, idx)
        return b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)

    def _read(self, addr: int, length: int, idx: int) -> bytes:
        self.usb_control_msg(RTL8187_REQT_READ, RTL8187_REQ_GET_REG, addr, idx & 0x03, length)
        return bytes(self._io_dmabuf[:length])

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

    def _write(self, addr: int, payload, idx: int) -> None:
        self.usb_control_msg(RTL8187_REQT_WRITE, RTL8187_REQ_SET_REG, addr, idx & 0x03, payload)

    def usb_control_msg(self, request_type: int, request: int, value: int, index: int,
                        data_or_length) -> None:
        """usb_control_msg through priv->io_dmabuf, one try, result ignored like every rtl8187
        caller (rtl8225.c:22-113, :183-186). ENODEV marks the device removed and raises."""
        if self.removed:
            raise usb.core.USBError("device removed", errno=errno.ENODEV)
        is_read = isinstance(data_or_length, int)
        if not is_read:
            self._io_dmabuf[:len(data_or_length)] = bytes(data_or_length)
        try:
            data = self.dev.ctrl_transfer(request_type, request, value, index, data_or_length,
                                          self.timeout_ms)
        except usb.core.USBError as e:
            if is_device_gone(e):
                self.removed = True
                raise
            return
        if is_read:
            self._io_dmabuf[:len(data)] = bytes(data)

    # ---- read/modify/write helpers -------------------------------------
    def write8_set(self, addr: int, mask: int, idx: int = 0) -> None:
        self.write8(addr, self.read8(addr, idx) | mask, idx)

    def write8_clr(self, addr: int, mask: int, idx: int = 0) -> None:
        self.write8(addr, self.read8(addr, idx) & ~mask & 0xFF, idx)

    def write32_set(self, addr: int, mask: int, idx: int = 0) -> None:
        self.write32(addr, self.read32(addr, idx) | mask, idx)

    def write32_clr(self, addr: int, mask: int, idx: int = 0) -> None:
        self.write32(addr, self.read32(addr, idx) & (~mask & 0xFFFFFFFF), idx)
