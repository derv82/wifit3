"""Vendor control-transfer transport for rt2800usb chips.

Mirrors the rt2x00usb register-access helpers (rt2x00usb.c:45-80,
``rt2x00usb_vendor_request``).

Wire format for a 4-byte register read/write:

    bmRequestType = 0xC0 IN / 0x40 OUT
    bRequest      = 7 (USB_MULTI_READ) / 6 (USB_MULTI_WRITE)
    wValue        = 0
    wIndex        = register address    ← address goes HERE, not in wValue
    wLength       = 4 (or longer for multi-byte writes)
    data          = u32 LE register value

**Subtle gotcha**: the kernel `rt2x00usb_vendor_request` takes
``(const u16 offset, const u16 value)`` as the 4th/5th args, but its
internal ``usb_control_msg`` call passes ``value`` to ``wValue`` and
``offset`` to ``wIndex`` — i.e. the addresses goes in **wIndex** even
though the wrapper parameter is named ``offset``. For register access,
``value`` is always 0. Verified empirically: any other ordering returns
the same stale word (e.g. 0x00020208) for every address.

EEPROM (USB_EEPROM_READ = 9) is one-shot — the kernel reads the whole
EEPROM in a single transfer with wValue=wIndex=0; the chip just
streams bytes from offset 0.

USB_DEVICE_MODE = 1 carries the "MCU boot" + "reset" signals where
the *value* goes in wValue and *mode* in wIndex (no data payload).
"""
from __future__ import annotations

import logging
from typing import Sequence, Union

import usb.core

from .constants import (
    USB_DEVICE_MODE,
    USB_EEPROM_READ,
    USB_MULTI_READ,
    USB_MULTI_WRITE,
    USB_SINGLE_READ,
    USB_SINGLE_WRITE,
    USB_VENDOR_REQUEST_IN,
    USB_VENDOR_REQUEST_OUT,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_MS = 1000   # kernel rt2x00usb default is 1000 ms

# Kernel CSR_CACHE_SIZE (rt2x00usb.h:37). Every multi-byte register
# access (incl. the 4-KB firmware blob upload) must be chunked into
# transfers no larger than this — the chip's USB controller silently
# fails or stalls on larger control transfers. Verified empirically:
# a single 4096-byte FW upload caused USB_MODE_FIRMWARE to either time
# out or pipe-stall depending on the chip's prior state.
CSR_CACHE_SIZE = 64


class RT2800USBTransport:
    """Vendor control-transfer transport. Bulk endpoints are claimed by
    the rx/tx modules, not here."""

    def __init__(self, dev: usb.core.Device, timeout_ms: int = _DEFAULT_TIMEOUT_MS):
        self.dev = dev
        self.timeout_ms = timeout_ms

    # ---- 4-byte (32-bit) register access --------------------------------
    # wValue=0, wIndex=addr — see module docstring for the wValue/wIndex
    # gotcha. Verified empirically (any other ordering returns the same
    # 0x00020208 stale word for every address).
    def read32(self, addr: int) -> int:
        data = self.dev.ctrl_transfer(
            USB_VENDOR_REQUEST_IN, USB_MULTI_READ,
            0, addr, 4, self.timeout_ms,
        )
        b = bytes(data)
        return b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)

    def write32(self, addr: int, val: int) -> None:
        payload = bytes((
            val & 0xFF,
            (val >> 8) & 0xFF,
            (val >> 16) & 0xFF,
            (val >> 24) & 0xFF,
        ))
        sent = self.dev.ctrl_transfer(
            USB_VENDOR_REQUEST_OUT, USB_MULTI_WRITE,
            0, addr, payload, self.timeout_ms,
        )
        if sent != 4:
            raise IOError(f"write32(0x{addr:04x}) short: {sent}/4")

    # ---- multi-byte read/write (kernel rt2x00usb_register_multiread) ----
    # Both are chunked to CSR_CACHE_SIZE (64 B) per transfer — see the
    # CSR_CACHE_SIZE comment above. Kernel does the same in
    # rt2x00usb_vendor_request_buff (rt2x00usb.c:114-143).
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

    # ---- 1-byte register access (USB_SINGLE_*) --------------------------
    def read8(self, addr: int) -> int:
        data = self.dev.ctrl_transfer(
            USB_VENDOR_REQUEST_IN, USB_SINGLE_READ,
            0, addr, 1, self.timeout_ms,
        )
        return bytes(data)[0]

    def write8(self, addr: int, val: int) -> None:
        self.dev.ctrl_transfer(
            USB_VENDOR_REQUEST_OUT, USB_SINGLE_WRITE,
            0, addr, [val & 0xFF], self.timeout_ms,
        )

    # ---- EEPROM (93C66 on older chips, EFUSE-shadowed on RT5390/5592) --
    def read_eeprom(self, length: int) -> bytes:
        """Read ``length`` bytes from the EEPROM in one shot.

        Kernel `rt2x00usb_eeprom_read` (rt2x00usb.h:170-176) always
        issues a single transfer with ``wValue=0, wIndex=0`` and
        ``wLength=length`` — the chip streams the whole EEPROM
        contents from byte 0. There's no per-word addressing.
        """
        data = self.dev.ctrl_transfer(
            USB_VENDOR_REQUEST_IN, USB_EEPROM_READ,
            0, 0, length, self.timeout_ms,
        )
        return bytes(data)

    # ---- USB_DEVICE_MODE (FW boot signal + USB resets) ------------------
    def autorun_detect(self) -> int:
        """1 if the NIC is in AutoRun mode (skip FW upload), else 0. USB_DEVICE_MODE
        IN read with the magic USB_MODE_AUTORUN (0x11) in wValue — a distinct request
        from register_read, so it can't be expressed through read32.
        [SRC] rt2800usb.c:176-203 rt2800usb_autorun_detect."""
        from .constants import USB_MODE_AUTORUN
        data = self.dev.ctrl_transfer(
            USB_VENDOR_REQUEST_IN, USB_DEVICE_MODE, USB_MODE_AUTORUN, 0, 4, self.timeout_ms,
        )
        fw_mode = int.from_bytes(bytes(data), "little")
        return 1 if (fw_mode & 0x00000003) == 2 else 0

    def set_device_mode(self, mode: int, value: int) -> None:
        """USB_DEVICE_MODE vendor request with no data phase.

        Kernel uses this for:
          * USB_MODE_RESET  (req=1, val=1)
          * MCU boot signal (req=1, val=0x08)
          * USB_MODE_FIRMWARE (val=USB_MODE_FIRMWARE)
        """
        self.dev.ctrl_transfer(
            USB_VENDOR_REQUEST_OUT, USB_DEVICE_MODE,
            value, mode, b"", self.timeout_ms,
        )

    # ---- read/modify/write convenience ----------------------------------
    def write32_set(self, addr: int, mask: int) -> None:
        self.write32(addr, self.read32(addr) | mask)

    def write32_clr(self, addr: int, mask: int) -> None:
        self.write32(addr, self.read32(addr) & (~mask & 0xFFFFFFFF))

    def write32_mask(self, addr: int, mask: int, value: int) -> None:
        """Set the bits in ``mask`` of ``addr`` to the corresponding
        bits of ``value`` (shifted to land in ``mask``).  Mirrors the
        kernel ``rt2x00_set_field32`` helper."""
        cur = self.read32(addr)
        shift = (mask & -mask).bit_length() - 1
        new = (cur & ~mask) | ((value << shift) & mask)
        self.write32(addr, new & 0xFFFFFFFF)
