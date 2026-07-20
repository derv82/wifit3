"""Synchronous USB transport for MT76x2U.

SPDX-License-Identifier: GPL-2.0-or-later
Ported from Linux mt76 (kernel v6.18) by wifit3, 2026.

Mirrors the mt76u USB-bus helpers in
``data_dumps/mt76-source-v6.18/usb.c`` (mt76u_rr / mt76u_wr / mt76u_single_wr).

Register access is virtual-addressed: the top 2 bits select a bus
(`MT_VEND_TYPE_EEPROM` / `MT_VEND_TYPE_CFG` / default), which in turn picks
the vendor bRequest:

    default    -> bReq = 0x07 read / 0x06 write   (MT_VEND_MULTI_*)
    CFG bus    -> bReq = 0x47 read / 0x46 write   (MT_VEND_*_CFG)
    EEPROM bus -> bReq = 0x09 read only           (MT_VEND_READ_EEPROM)

After stripping the bus marker, the 32-bit address is encoded as
wValue = addr >> 16, wIndex = addr & 0xFFFF. Read/write payload is 4 bytes
little-endian.

The FW upload uses ``single_wr`` — a 2-control-transfer split where val
itself is encoded into wValue (low 16 bits, then high 16 bits) and there is
NO data payload. See ``mt76u_single_wr`` in usb.c:215.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Optional

import usb.core

from .constants import (
    EP_IN_CMD_RESP,
    EP_IN_PKT_RX,
    EP_OUT_INBAND_CMD,
    MT_VEND_MULTI_READ,
    MT_VEND_MULTI_WRITE,
    MT_VEND_READ_CFG,
    MT_VEND_READ_EEPROM,
    MT_VEND_TYPE_CFG,
    MT_VEND_TYPE_EEPROM,
    MT_VEND_TYPE_MASK,
    MT_VEND_WRITE_CFG,
    MT_VEND_WRITE_FCE,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_MS = 500
_BULK_TIMEOUT_MS = 2000


class MT76x2UTransport:
    """USB transport for MT76x2U.

    Owns the PyUSB Device handle, vendor-control encoding, and bulk
    read/write primitives. Bring-up code (firmware.py, mac.py, ...)
    layers on top of this.
    """

    def __init__(self, dev: usb.core.Device, timeout_ms: int = _DEFAULT_TIMEOUT_MS):
        self.dev = dev
        self.timeout_ms = timeout_ms
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------
    # Register read/write — virtual-address-aware routing.
    # ------------------------------------------------------------------
    def read32(self, addr: int) -> int:
        """Read a 32-bit register. Top 2 bits of `addr` select the bus."""
        bus = addr & MT_VEND_TYPE_MASK
        plain_addr = addr & ~MT_VEND_TYPE_MASK
        if bus == MT_VEND_TYPE_EEPROM:
            req = MT_VEND_READ_EEPROM
        elif bus == MT_VEND_TYPE_CFG:
            req = MT_VEND_READ_CFG
        else:
            req = MT_VEND_MULTI_READ
        data = self._ctrl_in(req, plain_addr >> 16, plain_addr & 0xFFFF, 4)
        if len(data) < 4:
            return 0
        return struct.unpack("<I", bytes(data))[0]

    def write32(self, addr: int, val: int) -> None:
        """Write a 32-bit register. Top 2 bits of `addr` select the bus."""
        bus = addr & MT_VEND_TYPE_MASK
        plain_addr = addr & ~MT_VEND_TYPE_MASK
        if bus == MT_VEND_TYPE_CFG:
            req = MT_VEND_WRITE_CFG
        else:
            req = MT_VEND_MULTI_WRITE
        payload = struct.pack("<I", val & 0xFFFFFFFF)
        self._ctrl_out(req, plain_addr >> 16, plain_addr & 0xFFFF, payload)

    def rmw32(self, addr: int, mask: int, val: int) -> int:
        cur = self.read32(addr)
        new = (cur & ~mask) | (val & mask)
        self.write32(addr, new)
        return new

    def write_copy(self, addr: int, data: bytes) -> None:
        """Copy a byte buffer to consecutive registers in ONE MULTI_WRITE.

        Mirrors ``mt76_wr_copy`` -> ``mt76u_copy`` (usb.c:158): the kernel writes
        structs (the 8-byte WCID address, 16-byte shared keys) as a single vendor
        MULTI_WRITE of the whole payload, NOT one ``write32`` per word. Length is
        rounded up to 4 bytes; every caller here copies <= 16 bytes and
        ``usb->data_len`` is >= 32 (usb.c:1083-1085), so it is always one transfer.
        """
        plain_addr = addr & ~MT_VEND_TYPE_MASK     # copies only hit the default bus
        payload = bytes(data)
        if len(payload) % 4:
            payload += b"\x00" * (4 - len(payload) % 4)
        self._ctrl_out(MT_VEND_MULTI_WRITE, plain_addr >> 16, plain_addr & 0xFFFF, payload)

    # ------------------------------------------------------------------
    # FW upload helpers.
    # ------------------------------------------------------------------
    def single_wr_fce(self, addr: int, val: int) -> None:
        """Write a 32-bit value to an FCE register as TWO control transfers.

        Per ``mt76u_single_wr`` (data_dumps/mt76-source-v6.18/usb.c:215),
        FCE register programming during FW upload uses a peculiar encoding:
        the 32-bit value is split across two control transfers, each with
        the value-half in wValue and NO data payload.

            xfer 1: wValue = val & 0xFFFF,  wIndex = addr+0
            xfer 2: wValue = val >> 16,     wIndex = addr+2

        This is the ONLY caller of MT_VEND_WRITE_FCE — used solely to
        program MT_FCE_DMA_ADDR (0x0230) and MT_FCE_DMA_LEN (0x0234) for
        each ROM-patch / ILM / DLM chunk.
        """
        self._ctrl_out_no_payload(MT_VEND_WRITE_FCE, val & 0xFFFF, addr)
        self._ctrl_out_no_payload(MT_VEND_WRITE_FCE, (val >> 16) & 0xFFFF, addr + 2)

    def vendor_dev_mode(self, wvalue: int) -> None:
        """Issue a vendor MT_VEND_DEV_MODE (bReq=0x01) with no payload.

        Used for FW reset (wValue=0x0001) and IVB trigger (wValue=0x0012).
        """
        self._ctrl_out_no_payload(0x01, wvalue, 0)

    # ------------------------------------------------------------------
    # Bulk OUT / IN.
    # ------------------------------------------------------------------
    def write_bulk(self, ep: int, data: bytes,
                   timeout_ms: int = _BULK_TIMEOUT_MS) -> int:
        return self.dev.write(ep, data, timeout=timeout_ms)

    def read_bulk(self, ep: int, length: int,
                  timeout_ms: int = _BULK_TIMEOUT_MS) -> bytes:
        return bytes(self.dev.read(ep, length, timeout=timeout_ms))

    # ------------------------------------------------------------------
    # Async wrappers (for use from asyncio code paths).
    # ------------------------------------------------------------------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    async def async_write_bulk(self, ep: int, data: bytes,
                               timeout_ms: int = _BULK_TIMEOUT_MS) -> int:
        loop = self._ensure_loop()
        return await loop.run_in_executor(
            None, lambda: self.write_bulk(ep, data, timeout_ms)
        )

    async def async_read_bulk(self, ep: int, length: int,
                              timeout_ms: int = _BULK_TIMEOUT_MS) -> bytes:
        loop = self._ensure_loop()
        return await loop.run_in_executor(
            None, lambda: self.read_bulk(ep, length, timeout_ms)
        )

    # ------------------------------------------------------------------
    # Internal control-transfer helpers.
    # ------------------------------------------------------------------
    def _ctrl_in(self, bReq: int, wValue: int, wIndex: int, wLength: int) -> bytes:
        return bytes(self.dev.ctrl_transfer(
            bmRequestType=0xC0,   # vendor IN
            bRequest=bReq,
            wValue=wValue,
            wIndex=wIndex,
            data_or_wLength=wLength,
            timeout=self.timeout_ms,
        ))

    def _ctrl_out(self, bReq: int, wValue: int, wIndex: int, payload: bytes) -> None:
        self.dev.ctrl_transfer(
            bmRequestType=0x40,   # vendor OUT
            bRequest=bReq,
            wValue=wValue,
            wIndex=wIndex,
            data_or_wLength=payload,
            timeout=self.timeout_ms,
        )

    def _ctrl_out_no_payload(self, bReq: int, wValue: int, wIndex: int) -> None:
        self.dev.ctrl_transfer(
            bmRequestType=0x40,
            bRequest=bReq,
            wValue=wValue,
            wIndex=wIndex,
            data_or_wLength=b"",
            timeout=self.timeout_ms,
        )

    # ------------------------------------------------------------------
    # Endpoint discovery sanity check (matches kernel's mt76u_set_endpoints).
    # ------------------------------------------------------------------
    def assert_expected_endpoints(self) -> None:
        """Confirm the wireless-mode endpoint layout before any bulk I/O.

        Cold-boot MT7612U enumerates as USB Mass Storage with EPs 0x81/0x02.
        After Windows opens the device (which triggers usb_reset_device on
        Linux probe), it flips to wireless mode with 2 bulk-IN + 6 bulk-OUT.

        If we see the mass-storage layout, the device is mid-mode-switch
        and bulk I/O on EP 0x08 will stall.
        """
        cfg = self.dev.get_active_configuration()
        intf = cfg[(0, 0)]
        eps = {ep.bEndpointAddress for ep in intf}
        expected = {EP_IN_PKT_RX, EP_IN_CMD_RESP, EP_OUT_INBAND_CMD}
        missing = expected - eps
        if missing:
            raise RuntimeError(
                f"MT7612U not in wireless mode (missing EPs: "
                f"{', '.join(f'0x{ep:02x}' for ep in sorted(missing))}). "
                f"Try unplug+replug; if it persists, Zadig may need re-binding."
            )
