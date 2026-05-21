"""Synchronous USB transport for MT76x0U.

Ported from Linux mt76 (kernel v6.18) for wifit3, 2026.
Mirrors the mt76u USB-bus helpers in `data_dumps/mt76-source-v6.18/usb.c`
(mt76u_rr / mt76u_wr / mt76u_single_wr / mt76u_bulk_msg).

For M1 (FW upload → FW_READY ack) we need only:
- `read32(addr)`  — vendor IN  bReq=0x07 MULTI_READ
- `write32(addr, val)` — vendor OUT bReq=0x06 MULTI_WRITE
- `single_wr(reg, val)` — vendor OUT bReq=0x42 WRITE_FCE, 16-bit halves into wValue
- `bulk_out(ep, data)` — bulk OUT on EP 0x08
- `vendor_request_out(bReq, wValue, payload)` — generic for FW_RESET + IVB_TRIGGER
- interface claim / release / dispose

Register encoding (default bus): `wValue = addr >> 16, wIndex = addr & 0xFFFF,
payload = 4 bytes little-endian`. The MT_VEND_WRITE_FCE single_wr is different —
the 32-bit value is split into TWO control transfers, low-half into wValue
with wIndex = reg, then high-half with wIndex = reg + 2. No payload either.

Per [[feedback_prefer_fork_over_base]] this is INTENTIONALLY a sibling of
chips/mt76x2u/transport.py — same protocol, fresh port. Don't `from
..mt76x2u import ...`. If we ever validate that a helper is genuinely shared
across 2+ feature-complete drivers, that's a different conversation.
"""
from __future__ import annotations

import errno
import logging
import struct
import time
from typing import Optional

import usb.core
import usb.util

from .constants import (
    MT_VEND_DEV_MODE,
    MT_VEND_MULTI_READ,
    MT_VEND_MULTI_WRITE,
    MT_VEND_WRITE_FCE,
)

logger = logging.getLogger(__name__)

# Match kernel __mt76u_vendor_request — [SRC] data_dumps/mt76-source-v6.18/usb.c:11-12.
# Without retry, the first vendor xfer after mt76x02u_mcu_fw_reset stalls
# because the chip is temporarily unresponsive (~10-50ms typical) while the
# MCU resets. The kernel retries through it transparently.
_DEFAULT_TIMEOUT_MS = 300       # MT_VEND_REQ_TOUT_MS
_VEND_REQ_MAX_RETRY = 10        # MT_VEND_REQ_MAX_RETRY
_VEND_RETRY_SLEEP_MS = 7.5      # midpoint of kernel usleep_range(5000, 10000)
_BULK_TIMEOUT_MS = 2000


def _is_fatal_usb_error(e: usb.core.USBError) -> bool:
    """Kernel only stops retrying on -ENODEV / -EPROTO (device gone / protocol
    error). Timeout, pipe stall, and others are retried.
    """
    # libusb backend codes (negative): -4 NO_DEVICE, -7 TIMEOUT, -9 PIPE,
    # -1 IO. PyUSB exposes these via e.backend_error_code OR e.errno.
    code = getattr(e, "backend_error_code", None)
    if code is not None and code == -4:   # LIBUSB_ERROR_NO_DEVICE
        return True
    if e.errno == errno.ENODEV:
        return True
    return False


def _vendor_ctrl_with_retry(
    dev: usb.core.Device,
    bmRequestType: int,
    bRequest: int,
    wValue: int,
    wIndex: int,
    data_or_wLength,
    timeout_ms: int,
    label: str,
):
    """Wraps dev.ctrl_transfer with kernel-equivalent retry semantics.

    Mirrors __mt76u_vendor_request (mt76-source-v6.18/usb.c:18-44):
      - up to MT_VEND_REQ_MAX_RETRY=10 attempts
      - each attempt uses MT_VEND_REQ_TOUT_MS=300ms timeout
      - sleep 5-10ms between attempts
      - stop early on -ENODEV / -EPROTO; retry on timeout / pipe / I/O
    """
    last_exc: Optional[usb.core.USBError] = None
    for attempt in range(_VEND_REQ_MAX_RETRY):
        try:
            return dev.ctrl_transfer(
                bmRequestType=bmRequestType,
                bRequest=bRequest,
                wValue=wValue,
                wIndex=wIndex,
                data_or_wLength=data_or_wLength,
                timeout=timeout_ms,
            )
        except usb.core.USBError as e:
            last_exc = e
            if _is_fatal_usb_error(e):
                raise
            if attempt < _VEND_REQ_MAX_RETRY - 1:
                logger.warning("%s: attempt %d/%d failed (%s); retrying",
                               label, attempt + 1, _VEND_REQ_MAX_RETRY, e)
                time.sleep(_VEND_RETRY_SLEEP_MS / 1000)
    assert last_exc is not None
    logger.error("%s: gave up after %d retries", label, _VEND_REQ_MAX_RETRY)
    raise last_exc


class MT76x0UTransport:
    """Synchronous USB transport for MT76x0U.

    Owns the PyUSB Device handle, vendor-control encoding, and bulk
    write primitives. Bring-up code (firmware.py) layers on top of this.
    """

    def __init__(self, dev: usb.core.Device, timeout_ms: int = _DEFAULT_TIMEOUT_MS):
        self.dev = dev
        self.timeout_ms = timeout_ms
        self._interface_claimed = False

    # ------------------------------------------------------------------
    # Interface lifecycle.
    # ------------------------------------------------------------------
    def claim(self) -> None:
        """Set configuration + claim interface 0. Idempotent."""
        if self._interface_claimed:
            return
        # On Windows + WinUSB the configuration is already set by Zadig.
        # Skipping set_configuration here matches the mt76x2u sibling and
        # avoids a known PyUSB pitfall on WinUSB ("device or resource busy"
        # when the bound config matches the requested one).
        try:
            cfg = self.dev.get_active_configuration()
            intf = cfg[(0, 0)]
            usb.util.claim_interface(self.dev, intf.bInterfaceNumber)
            self._interface_claimed = True
            logger.debug("Claimed interface %d.%d",
                         intf.bInterfaceNumber, intf.bAlternateSetting)
        except usb.core.USBError as e:
            raise RuntimeError(f"Failed to claim interface: {e}") from e

    def release(self) -> None:
        if not self._interface_claimed:
            return
        try:
            usb.util.release_interface(self.dev, 0)
        except usb.core.USBError as e:
            logger.warning("release_interface failed (ignored): %s", e)
        self._interface_claimed = False

    def dispose(self) -> None:
        self.release()
        try:
            usb.util.dispose_resources(self.dev)
        except Exception as e:
            logger.warning("dispose_resources failed (ignored): %s", e)

    # ------------------------------------------------------------------
    # Register read/write — default bus only (mt76x0u doesn't use the
    # virtual-address CFG / EEPROM buses in M1; add when M2+ needs them).
    # ------------------------------------------------------------------
    def read32(self, addr: int) -> int:
        """Read a 32-bit register via MT_VEND_MULTI_READ (bReq=0x07)."""
        wValue = (addr >> 16) & 0xFFFF
        wIndex = addr & 0xFFFF
        data = _vendor_ctrl_with_retry(
            self.dev,
            bmRequestType=0xC0,            # vendor IN
            bRequest=MT_VEND_MULTI_READ,
            wValue=wValue,
            wIndex=wIndex,
            data_or_wLength=4,
            timeout_ms=self.timeout_ms,
            label=f"read32(0x{addr:08x})",
        )
        if len(data) != 4:
            raise RuntimeError(f"read32(0x{addr:08x}): got {len(data)} bytes, expected 4")
        return struct.unpack("<I", bytes(data))[0]

    def write32(self, addr: int, val: int) -> None:
        """Write a 32-bit register via MT_VEND_MULTI_WRITE (bReq=0x06)."""
        wValue = (addr >> 16) & 0xFFFF
        wIndex = addr & 0xFFFF
        payload = struct.pack("<I", val & 0xFFFFFFFF)
        n = _vendor_ctrl_with_retry(
            self.dev,
            bmRequestType=0x40,            # vendor OUT
            bRequest=MT_VEND_MULTI_WRITE,
            wValue=wValue,
            wIndex=wIndex,
            data_or_wLength=payload,
            timeout_ms=self.timeout_ms,
            label=f"write32(0x{addr:08x}, 0x{val:08x})",
        )
        if n != 4:
            raise RuntimeError(
                f"write32(0x{addr:08x}, 0x{val:08x}): wrote {n} bytes, expected 4"
            )

    def set_bits(self, addr: int, mask: int) -> int:
        """`mt76_set(reg, mask)` equivalent — RMW, returns the new value."""
        val = self.read32(addr)
        new_val = val | mask
        self.write32(addr, new_val)
        return new_val

    def clear_bits(self, addr: int, mask: int) -> int:
        """`mt76_clear(reg, mask)` equivalent."""
        val = self.read32(addr)
        new_val = val & ~mask
        self.write32(addr, new_val)
        return new_val

    # ------------------------------------------------------------------
    # MT_VEND_WRITE_FCE single_wr — 32-bit value split into two control
    # transfers, NO payload either time.
    # [SRC] data_dumps/mt76-source-v6.18/usb.c:215 (mt76u_single_wr).
    # ------------------------------------------------------------------
    def single_wr(self, reg: int, val: int) -> None:
        """Encode a 32-bit value via 2 control transfers (low half, then high)."""
        for offset, half in ((0, val & 0xFFFF), (2, (val >> 16) & 0xFFFF)):
            n = _vendor_ctrl_with_retry(
                self.dev,
                bmRequestType=0x40,            # vendor OUT
                bRequest=MT_VEND_WRITE_FCE,
                wValue=half,
                wIndex=reg + offset,
                data_or_wLength=None,          # no payload either half
                timeout_ms=self.timeout_ms,
                label=f"single_wr(0x{reg:04x}+{offset}, 0x{half:04x})",
            )
            # ctrl_transfer with data=None returns 0 on success.
            if n != 0:
                raise RuntimeError(
                    f"single_wr(reg=0x{reg:04x}+{offset}, half=0x{half:04x}) "
                    f"returned {n}"
                )

    # ------------------------------------------------------------------
    # MT_VEND_DEV_MODE generic — FW reset and IVB trigger share this
    # bRequest; difference is wValue + payload size.
    # ------------------------------------------------------------------
    def vendor_dev_mode(self, wValue: int, payload: Optional[bytes] = None) -> None:
        data = payload if payload is not None else b""
        wLen = len(data)
        n = _vendor_ctrl_with_retry(
            self.dev,
            bmRequestType=0x40,
            bRequest=MT_VEND_DEV_MODE,
            wValue=wValue,
            wIndex=0,
            data_or_wLength=data if wLen else None,
            timeout_ms=self.timeout_ms,
            label=f"vendor_dev_mode(wVal=0x{wValue:04x}, wLen={wLen})",
        )
        # When payload supplied, n is bytes written; when None it's 0.
        expected = wLen
        if n != expected:
            raise RuntimeError(
                f"vendor_dev_mode(wVal=0x{wValue:04x}, wLen=0x{wLen:04x}) "
                f"transferred {n} bytes, expected {expected}"
            )

    # ------------------------------------------------------------------
    # Bulk OUT (FW chunks on EP 0x08).
    # ------------------------------------------------------------------
    def bulk_out(self, ep: int, data: bytes, timeout_ms: int = _BULK_TIMEOUT_MS) -> int:
        n = self.dev.write(ep, data, timeout=timeout_ms)
        if n != len(data):
            raise RuntimeError(
                f"bulk_out(ep=0x{ep:02x}, len={len(data)}): wrote {n} bytes"
            )
        return n

    def bulk_in(self, ep: int, max_len: int, timeout_ms: int = _BULK_TIMEOUT_MS) -> bytes:
        """Read up to `max_len` bytes from a bulk-IN endpoint.

        On a timeout this raises usb.core.USBError (errno 10060 / Win, 110 / Lin).
        Callers handle that case explicitly — MCU `wait_resp` retries on timeout
        per kernel mt76x02u_mcu_wait_resp (5 attempts).
        """
        data = self.dev.read(ep, max_len, timeout=timeout_ms)
        return bytes(data)
