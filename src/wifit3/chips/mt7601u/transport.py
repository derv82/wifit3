"""USB transport for MT7601U: vendor control encoding + bulk pipes.

Ported from driver_sources/mt7601u-source-v7.2/mt7601u/usb.c and usb.h (tag v7.2).

MT7601U is NOT an mt76-USB part: it has no `mt76_usb` shim, no MMIO window, and no
scatter-gather. Every register read is one vendor IN control transfer that returns
4 raw bytes, and every 32-bit register write is TWO OUT control transfers (low
half at `offset`, high half at `offset + 2`) -- see __mt7601u_vendor_single_wr.
Those are the only two shapes the register file uses, so `rr`/`wr` below are the
entire register API. Firmware image chunks and MCU inband commands are the only
bulk-OUT traffic.

Endpoint numbers come from mt7601u_assign_pipes (usb.c:228), which fills them
positionally from the interface descriptor in endpoint order -- not by fixed
address -- so assign_pipes() reads the real descriptors instead of hardcoding.
"""
from __future__ import annotations

import errno
import logging
import time
from array import array
from typing import Optional

import usb.core
import usb.util

from .constants import (
    MT_EP_IN_CMD_RESP,
    MT_EP_IN_PKT_RX,
    MT_EP_OUT_INBAND_CMD,
    MT_VEND_BUF,
    MT_VEND_DEV_MODE,
    MT_VEND_DEV_MODE_RESET,
    MT_VEND_MULTI_READ,
    MT_VEND_REQ_MAX_RETRY,
    MT_VEND_REQ_TOUT_MS,
    MT_VEND_WRITE,
    MT_VEND_WRITE_FCE,
)

logger = logging.getLogger(__name__)

USB_DIR_OUT = 0x00
USB_DIR_IN = 0x80
USB_TYPE_VENDOR = 0x40
USB_RECIP_DEVICE = 0x00

_VEND_RETRY_SLEEP_MS = 5      # usb.c:110 msleep(5) between vendor-request retries
_BULK_TIMEOUT_MS = 500       # mcu.c:137 usb_bulk_msg timeout
_FW_BULK_TIMEOUT_MS = 1000   # mcu.c:317 firmware-upload completion timeout
_RX_TIMEOUT_MS = 200         # must stay well under RxReaderThread.stop()'s 1.5s join timeout


class DeviceGone(Exception):
    """The card was unplugged (the kernel's -ENODEV path, usb.c:105)."""


def _is_device_gone(e: usb.core.USBError) -> bool:
    """True for -ENODEV, the one error mt7601u_vendor_request does not retry."""
    if getattr(e, "backend_error_code", None) == -4:      # LIBUSB_ERROR_NO_DEVICE
        return True
    return e.errno == errno.ENODEV


class MT7601UTransport:
    """Owns the PyUSB handle and the vendor-control encoding usb.c implements."""

    def __init__(self, dev: usb.core.Device, timeout_ms: int = MT_VEND_REQ_TOUT_MS):
        self.dev = dev
        self.timeout_ms = timeout_ms
        self._interface_claimed = False
        self.in_eps: list[int] = []
        self.in_max_packet: int = 0
        self.out_eps: list[int] = []
        self._in_buf = bytearray(MT_VEND_BUF)   # vend_buf; rr() reads back from here

    # ------------------------------------------------------------------
    # Interface lifecycle (usb.c:265 mt7601u_probe -> usb_reset_device)
    # ------------------------------------------------------------------

    def claim(self) -> None:
        """Detach the kernel driver (Linux), then claim interface 0. Idempotent."""
        if self._interface_claimed:
            return
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError) as e:
            logger.debug("kernel-driver detach skipped: %s", e)
        usb.util.claim_interface(self.dev, 0)
        self._interface_claimed = True

    def assign_pipes(self) -> None:
        """Fill in_eps/out_eps from the interface descriptor (usb.c:228).

        Positional, as upstream: bulk-IN endpoints take in_eps in descriptor
        order, bulk-OUT take out_eps. Upstream requires exactly 2 IN and 6 OUT.
        """
        cfg = self.dev.get_active_configuration()
        interface = cfg[(0, 0)]
        self.in_eps.clear()
        self.out_eps.clear()
        # usb.c:244 assigns in_max_packet on every bulk-IN it walks, so the LAST one
        # wins; init.c:108 then enables RX aggregation only when it is a full 512.
        # Keeping the first instead silently flips that branch on any device whose two
        # bulk-IN endpoints disagree.
        self.in_max_packet = 0
        for endpoint in interface:
            if endpoint.bEndpointAddress & usb.util.ENDPOINT_IN:
                self.in_max_packet = endpoint.wMaxPacketSize
                self.in_eps.append(endpoint.bEndpointAddress)
            else:
                self.out_eps.append(endpoint.bEndpointAddress)
        if len(self.in_eps) != 2 or len(self.out_eps) != 6:
            raise ValueError(
                f"wrong pipe number in:{len(self.in_eps)} out:{len(self.out_eps)}")

    def reset(self) -> None:
        """mt7601u_vendor_reset: put the chip into device mode (usb.c:119)."""
        self.vendor_request(MT_VEND_DEV_MODE, USB_DIR_OUT,
                            MT_VEND_DEV_MODE_RESET, 0, None, 0)

    def release(self) -> None:
        if not self._interface_claimed:
            return
        try:
            usb.util.release_interface(self.dev, 0)
        except usb.core.USBError as e:
            logger.debug("release_interface failed: %s", e)
        self._interface_claimed = False

    def dispose(self) -> None:
        self.release()
        try:
            usb.util.dispose_resources(self.dev)
        except Exception as e:                     # pragma: no cover - teardown best effort
            logger.debug("dispose_resources failed: %s", e)

    # ------------------------------------------------------------------
    # Vendor control requests (usb.c:88 mt7601u_vendor_request)
    # ------------------------------------------------------------------

    def vendor_request(self, req: int, direction: int, val: int, offset: int,
                       data: Optional[bytes] = None, buflen: int = 0) -> int:
        """One vendor control transfer with upstream's retry semantics.

        mt7601u_vendor_request retries up to MT_VEND_REQ_MAX_RETRY times, sleeping
        5 ms between attempts, and returns immediately when the transfer succeeds
        or the card is gone (-ENODEV) -- the latter is what sets
        MT7601U_STATE_REMOVED upstream. Returns the byte count read, or 0 for the
        zero-length writes the register file uses.
        """
        req_type = direction | USB_TYPE_VENDOR | USB_RECIP_DEVICE
        last_exc: Optional[usb.core.USBError] = None
        for attempt in range(MT_VEND_REQ_MAX_RETRY):
            try:
                if data is not None:
                    payload: object = data
                elif direction == USB_DIR_IN:
                    payload = self._in_buf = bytearray(buflen or MT_VEND_BUF)
                else:
                    payload = buflen
                got = self.dev.ctrl_transfer(
                    bmRequestType=req_type,
                    bRequest=req,
                    wValue=val,
                    wIndex=offset,
                    data_or_wLength=payload,
                    timeout=self.timeout_ms,
                )
                # PyUSB returns the bytes themselves for an IN transfer and a
                # length for an OUT one. Callers all want the count.
                if isinstance(got, (bytes, bytearray, array)):
                    self._in_buf = bytearray(got)
                    return len(got)
                return int(got)
            except usb.core.USBError as e:
                last_exc = e
                if _is_device_gone(e):
                    raise DeviceGone(f"vendor req {req:#04x} off {offset:#06x}") from e
                if attempt < MT_VEND_REQ_MAX_RETRY - 1:
                    logger.warning("vendor req %02x off %04x attempt %d/%d: %s",
                                   req, offset, attempt + 1, MT_VEND_REQ_MAX_RETRY, e)
                    time.sleep(_VEND_RETRY_SLEEP_MS / 1000)
        logger.error("Vendor request req:%02x off:%04x failed:%s", req, offset, last_exc)
        raise last_exc                                   # type: ignore[misc]

    # ------------------------------------------------------------------
    # Register access (usb.c:126 __mt7601u_rr / usb.c:157 __mt7601u_vendor_single_wr)
    # ------------------------------------------------------------------

    def rr(self, offset: int) -> int:
        """Read one 32-bit register. ~0 marks a short/failed read (usb.c:137)."""
        val = 0xFFFFFFFF
        got = self.vendor_request(MT_VEND_MULTI_READ, USB_DIR_IN,
                                  0, offset, buflen=MT_VEND_BUF)
        if got == MT_VEND_BUF:
            val = int.from_bytes(bytes(self._in_buf), "little")
        elif got > 0:
            logger.error("Error: wrong size read:%d off:%08x", got, offset)
        return val

    def vendor_single_wr(self, req: int, offset: int, val: int) -> None:
        """Split one 32-bit write into two OUT transfers (usb.c:157).

        Low half into wValue at `offset`, high half into wValue at `offset + 2`.
        MT_VEND_WRITE_FCE takes this same shape.
        """
        self.vendor_request(req, USB_DIR_OUT, val & 0xFFFF, offset, None, 0)
        self.vendor_request(req, USB_DIR_OUT, (val >> 16) & 0xFFFF, offset + 2, None, 0)

    def wr(self, offset: int, val: int) -> None:
        """Write one 32-bit register (mt7601u_wr)."""
        self.vendor_single_wr(MT_VEND_WRITE, offset, val & 0xFFFFFFFF)

    def fce_wr(self, offset: int, val: int) -> None:
        """One 32-bit MT_VEND_WRITE_FCE write -- the firmware-download path."""
        self.vendor_single_wr(MT_VEND_WRITE_FCE, offset, val & 0xFFFFFFFF)

    def rmw(self, offset: int, mask: int, val: int) -> int:
        """Read-modify-write that always writes (mt7601u_rmw, usb.c:188)."""
        val |= self.rr(offset) & ~mask & 0xFFFFFFFF
        self.wr(offset, val)
        return val

    def rmc(self, offset: int, mask: int, val: int) -> int:
        """Read-modify-write that skips the write when nothing changes (usb.c:198)."""
        reg = self.rr(offset)
        val |= reg & ~mask & 0xFFFFFFFF
        if reg != val:
            self.wr(offset, val)
        return val

    def addr_wr(self, offset: int, addr: bytes) -> None:
        """Write a 6-byte MAC address as the kernel splits it (usb.c:222)."""
        self.wr(offset, int.from_bytes(addr[0:4], "little"))
        self.wr(offset + 4, addr[4] | (addr[5] << 8))

    # ------------------------------------------------------------------
    # Bulk pipes
    # ------------------------------------------------------------------

    def bulk_out(self, data: bytes, timeout_ms: int = _BULK_TIMEOUT_MS) -> int:
        """Write to the inband-command endpoint (MT_EP_OUT_INBAND_CMD)."""
        ep = self.out_eps[MT_EP_OUT_INBAND_CMD]
        return self.dev.write(ep, data, timeout=timeout_ms)

    def bulk_out_tx(self, data: bytes, queue: int, timeout_ms: int = _BULK_TIMEOUT_MS) -> int:
        """Write one 802.11 frame to the OUT endpoint for ``queue`` (dma.c:310).

        The kernel sends each queue on ``dev->out_eps[queue]``, so the queue index
        selects the endpoint. It is not optional: ``bulk_out`` targets
        ``MT_EP_OUT_INBAND_CMD``, which the MCU firmware consumes as a command
        stream rather than a frame. Writing frames there reported TX success while
        nothing was modulated, and the injected bytes confused the MCU badly enough
        to stop the packet stream on the receive endpoint.
        """
        ep = self.out_eps[queue]
        return self.dev.write(ep, data, timeout=timeout_ms)

    def bulk_out_inband_fw(self, data: bytes, timeout_ms: int = _FW_BULK_TIMEOUT_MS) -> int:
        """The firmware-download URB (mcu.c:311): same endpoint, longer timeout."""
        return self.bulk_out(data, timeout_ms)

    def bulk_in_resp(self, buf: int, timeout_ms: int = _BULK_TIMEOUT_MS) -> bytes:
        """Read one MCU response from MT_EP_IN_CMD_RESP."""
        ep = self.in_eps[MT_EP_IN_CMD_RESP]
        return bytes(self.dev.read(ep, buf, timeout=timeout_ms))

    def bulk_in_rx(self, buf: int, timeout_ms: int = _RX_TIMEOUT_MS) -> bytes:
        """One blocking bulk-IN read from MT_EP_IN_PKT_RX, or b"" when the interval was quiet.

        The timeout must stay finite: the RX reader thread only notices shutdown between reads,
        so an infinite read keeps the thread alive past close() and the release below it then
        surfaces as a spurious ENODEV unplug. Catch USBTimeoutError by type rather than by
        message, because the WinUSB backend reports errno 10060 with no "timeout" substring.
        """
        ep = self.in_eps[MT_EP_IN_PKT_RX]
        try:
            return bytes(self.dev.read(ep, buf, timeout=timeout_ms))
        except usb.core.USBTimeoutError:
            return b""