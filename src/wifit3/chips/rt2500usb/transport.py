"""Vendor control-transfer transport for rt2500usb (RT2570).

Mirrors the kernel rt2x00usb register-access helpers as used by
rt2500usb (rt2500usb.c:47-94, rt2x00usb.c ``rt2x00usb_vendor_request``).

Wire format for a 2-byte (16-bit) CSR read/write:

    bmRequestType = 0xC0 IN / 0x40 OUT
    bRequest      = 7 (USB_MULTI_READ) / 6 (USB_MULTI_WRITE)
    wValue        = 0
    wIndex        = register address     ← address goes HERE, not wValue
    wLength       = 2 (or longer for multi-byte writes)
    data          = u16 LE register value

This is the *same* vendor-request scheme as rt2800usb — only the
register width differs (RT2570 CSRs are 16-bit; rt2800 are 32-bit).
Verified against usb_dumps/captures_rt2500usb (capture-2/3): the
cold-boot probe issues req=7 reads + req=6 writes with wLength=2 and
the address in wIndex.

EEPROM (USB_EEPROM_READ = 9) is one-shot: a single transfer with
wValue=wIndex=0 streams the whole EEPROM from byte 0 (verified —
capture-2 frame 149/150: wLength=110 (=EEPROM_SIZE), 110 data bytes).

BBP / RF registers are not addressable directly; they are reached
through the PHY_CSR busy-poll registers. Those indirect helpers land in
M2 (init) where they can be pcap-diffed against the cold-boot sequence.
"""
from __future__ import annotations

import logging
import time
from typing import Sequence, Union

import usb.core

from .constants import (
    CSR_CACHE_SIZE,
    EEPROM_SIZE,
    EEPROM_TIMEOUT,
    REGISTER_BUSY_DELAY,
    REGISTER_TIMEOUT,
    REGISTER_USB_BUSY_COUNT,
    USB_EEPROM_READ,
    USB_MULTI_READ,
    USB_MULTI_WRITE,
    USB_VENDOR_REQUEST_IN,
    USB_VENDOR_REQUEST_OUT,
)

logger = logging.getLogger(__name__)


# ---- rt2x00_{get,set}_field16 helpers -------------------------------------
# The kernel stores bitfields as a mask; the shift is the position of the
# lowest set bit. These mirror rt2x00_get_field16 / rt2x00_set_field16.
def get_field16(reg: int, mask: int) -> int:
    shift = (mask & -mask).bit_length() - 1
    return (reg & mask) >> shift


def set_field16(reg: int, mask: int, value: int) -> int:
    shift = (mask & -mask).bit_length() - 1
    return (reg & ~mask) | ((value << shift) & mask)


class RT2500USBTransport:
    """Vendor control-transfer transport. Bulk endpoints (EP1 OUT / EP1
    IN) are claimed by the rx/tx modules, not here."""

    def __init__(self, dev: usb.core.Device, timeout_ms: int = REGISTER_TIMEOUT):
        self.dev = dev
        self.timeout_ms = timeout_ms

    # ---- 2-byte (16-bit) CSR access -------------------------------------
    # wValue=0, wIndex=addr. See module docstring for the wValue/wIndex
    # convention (address in wIndex, not wValue).
    def read16(self, addr: int) -> int:
        data = self.dev.ctrl_transfer(
            USB_VENDOR_REQUEST_IN, USB_MULTI_READ,
            0, addr, 2, self.timeout_ms,
        )
        b = bytes(data)
        return b[0] | (b[1] << 8)

    def write16(self, addr: int, val: int) -> None:
        payload = bytes((val & 0xFF, (val >> 8) & 0xFF))
        sent = self.dev.ctrl_transfer(
            USB_VENDOR_REQUEST_OUT, USB_MULTI_WRITE,
            0, addr, payload, self.timeout_ms,
        )
        if sent != 2:
            raise IOError(f"write16(0x{addr:04x}) short: {sent}/2")

    # ---- multi-byte read/write (rt2500usb_register_multiwrite) ----------
    # Chunked to CSR_CACHE_SIZE per transfer, matching the kernel
    # rt2x00usb_vendor_request_buff loop.
    def read_multi(self, addr: int, length: int) -> bytes:
        chunks: list[bytes] = []
        remaining = length
        off = addr
        while remaining > 0:
            bsize = min(CSR_CACHE_SIZE, remaining)
            data = self.dev.ctrl_transfer(
                USB_VENDOR_REQUEST_IN, USB_MULTI_READ,
                0, off, bsize, self.timeout_ms,
            )
            chunks.append(bytes(data))
            off += bsize
            remaining -= bsize
        return b"".join(chunks)

    def write_multi(self, addr: int, payload: Union[bytes, Sequence[int]]) -> None:
        payload = bytes(payload)
        remaining = len(payload)
        off = addr
        pos = 0
        while remaining > 0:
            bsize = min(CSR_CACHE_SIZE, remaining)
            sent = self.dev.ctrl_transfer(
                USB_VENDOR_REQUEST_OUT, USB_MULTI_WRITE,
                0, off, payload[pos:pos + bsize], self.timeout_ms,
            )
            if sent != bsize:
                raise IOError(
                    f"write_multi(0x{off:04x}) short chunk: {sent}/{bsize}"
                )
            off += bsize
            pos += bsize
            remaining -= bsize

    # ---- single-command writes (rt2x00usb_vendor_request_sw) ------------
    # No data phase: ``offset`` → wIndex, ``value`` → wValue. Used for
    # USB_DEVICE_MODE (set device mode) and USB_SINGLE_WRITE (1-byte CSR
    # write where the byte rides in wValue). Verified on the wire
    # (capture-2 frame 203: req=1 wValue=4 wIndex=1; frame 205: req=2
    # wValue=0xf0 wIndex=0x308).
    def vendor_request_sw(self, request: int, offset: int, value: int) -> None:
        self.dev.ctrl_transfer(
            USB_VENDOR_REQUEST_OUT, request,
            value, offset, b"", self.timeout_ms,
        )

    # ---- indirect-register busy poll (rt2500usb_regbusy_read) -----------
    def regbusy_read(self, addr: int, busy_mask: int) -> tuple[bool, int]:
        """Poll CSR ``addr`` until ``busy_mask`` clears.

        Returns ``(available, reg)``. On timeout returns ``(False, 0xFFFF)``
        — matching the kernel which sets ``*reg = ~0`` so an indirect read
        degrades to 0xff. Mirrors rt2500usb.c:96-115.
        """
        for _ in range(REGISTER_USB_BUSY_COUNT):
            reg = self.read16(addr)
            if not (reg & busy_mask):
                return True, reg
            time.sleep(REGISTER_BUSY_DELAY / 1_000_000)   # udelay(us)
        return False, 0xFFFF

    # ---- read/modify/write convenience ----------------------------------
    def write16_mask(self, addr: int, mask: int, value: int) -> None:
        """Set the bits in ``mask`` of CSR ``addr`` to ``value`` (shifted
        into ``mask``). Mirrors read → rt2x00_set_field16 → write."""
        cur = self.read16(addr)
        self.write16(addr, set_field16(cur, mask, value) & 0xFFFF)

    # ---- EEPROM (one-shot 93Cx6) ----------------------------------------
    def read_eeprom(self, length: int = EEPROM_SIZE) -> bytes:
        """Read ``length`` bytes from the EEPROM in one transfer.

        Kernel ``rt2x00usb_eeprom_read`` issues a single IN transfer with
        wValue=wIndex=0; the chip streams the whole EEPROM from byte 0.
        There is no per-word addressing.
        """
        data = self.dev.ctrl_transfer(
            USB_VENDOR_REQUEST_IN, USB_EEPROM_READ,
            0, 0, length, EEPROM_TIMEOUT,
        )
        return bytes(data)
