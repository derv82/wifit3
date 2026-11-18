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
Verified against driver_captures/captures_rt2500usb (capture-2/3): the
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

import errno
import logging
import time

import usb.core

from wifit3.errors import is_device_gone

from .constants import (
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

    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self.device_present = True   # DEVICE_STATE_PRESENT
        self.device_started = False  # DEVICE_STATE_STARTED, set once bring-up is done
        self.num_proto_errs = 0

    def rt2x00usb_check_usb_error(self, error: usb.core.USBError) -> bool:
        """[SRC] rt2x00usb.c:23-40. libusb has no kernel -ENOENT (its NOT_FOUND is a transient
        Windows stale handle) and reports -EPROTO as EIO."""
        if is_device_gone(error):
            return True
        if not self.device_started:
            return False
        if getattr(error, "errno", None) in (errno.EIO, errno.ETIMEDOUT):
            self.num_proto_errs += 1
        else:
            self.num_proto_errs = 0
        return self.num_proto_errs > 3

    def _vendor_request(self, requesttype, request, value, index, data_or_length,
                        timeout_ms: int = REGISTER_TIMEOUT):
        """rt2x00usb_vendor_request: retries until ``timeout_ms`` passes; a failure is logged and
        returns None, or raises ENODEV once the device is gone. [SRC] rt2x00usb.c:45-80."""
        if not self.device_present:
            raise usb.core.USBError("device not present", errno=errno.ENODEV)
        expire = time.monotonic() + timeout_ms / 1000
        while True:
            try:
                return self.dev.ctrl_transfer(requesttype, request, value, index,
                                              data_or_length, timeout_ms // 2)
            except usb.core.USBError as e:
                error = e
                if self.rt2x00usb_check_usb_error(e):
                    self.device_present = False
                    break
            if time.monotonic() >= expire:
                break
        logger.error("Vendor Request 0x%02x failed for offset 0x%04x with error %d",
                     request, index, -(error.errno or errno.EIO))
        if not self.device_present:
            raise usb.core.USBError("device not present", errno=errno.ENODEV) from error
        return None

    # ---- 2-byte (16-bit) CSR access -------------------------------------
    # wValue=0, wIndex=addr. See module docstring for the wValue/wIndex
    # convention (address in wIndex, not wValue).
    def read16(self, addr: int) -> int:
        data = self._vendor_request(USB_VENDOR_REQUEST_IN, USB_MULTI_READ, 0, addr, 2)
        if data is None:
            return 0          # rt2500usb.c:47-55 returns its uninitialised reg; 0 here
        return int.from_bytes(bytes(data), "little")

    def write16(self, addr: int, val: int) -> None:
        payload = bytes((val & 0xFF, (val >> 8) & 0xFF))
        self._vendor_request(USB_VENDOR_REQUEST_OUT, USB_MULTI_WRITE, 0, addr, payload)

    # ---- single-command writes (rt2x00usb_vendor_request_sw) ------------
    # No data phase: ``offset`` → wIndex, ``value`` → wValue. Used for
    # USB_DEVICE_MODE (set device mode) and USB_SINGLE_WRITE (1-byte CSR
    # write where the byte rides in wValue). Verified on the wire
    # (capture-2 frame 203: req=1 wValue=4 wIndex=1; frame 205: req=2
    # wValue=0xf0 wIndex=0x308).
    def vendor_request_sw(self, request: int, offset: int, value: int) -> None:
        self._vendor_request(USB_VENDOR_REQUEST_OUT, request, value, offset, b"")

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
        logger.error("Indirect register access failed: offset=0x%.08x, value=0x%.08x", addr, reg)
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
        data = self._vendor_request(USB_VENDOR_REQUEST_IN, USB_EEPROM_READ,
                                    0, 0, length, EEPROM_TIMEOUT)
        return bytes(length) if data is None else bytes(data)
