"""RTL8814AU raw USB transport — Realtek vendor control + bulk OUT.

All register access is a single vendor request (0x05): wValue = 16-bit register
offset, wIndex = 0, data stage 1/2/4 bytes little-endian. Firmware packets go out
on bulk EP 0x02. [SRC] os_dep/.../usb_ops_linux.c usbctrl_vendorreq.

The bring-up code in :mod:`firmware` is transport-agnostic (duck-typed
read8/16/32, write8/16/32, bulk_out), so the same routines drive this PyUSB
transport on hardware and the pcap-replay transport in the M1 verifier.
"""
from __future__ import annotations

import struct

import usb.core
import usb.util

from .constants import (
    EP_BULK_OUT_FW,
    REALTEK_VENDOR_REQUEST,
    REQ_TYPE_READ,
    REQ_TYPE_WRITE,
)

_CTRL_TIMEOUT_MS = 1000
_BULK_TIMEOUT_MS = 1000
# RX bulk-IN read: the buffer must hold a whole USB-aggregated transfer (the 8814A
# RX-DMA buffer is ~24 KB), so read generously; a short timeout keeps the reader
# thread responsive to stop() and returns None between bursts of traffic.
RX_BUF_SIZE = 0x8000        # 32 KB >= the RX-DMA aggregation ceiling
RX_TIMEOUT_MS = 200


class Rtl8814auTransport:
    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self._in_ep = None  # bulk-IN endpoint address, probed lazily

    # --- register access ---------------------------------------------------
    def _read(self, addr: int, length: int) -> bytes:
        return bytes(self.dev.ctrl_transfer(
            REQ_TYPE_READ, REALTEK_VENDOR_REQUEST, addr, 0, length, _CTRL_TIMEOUT_MS
        ))

    def _write(self, addr: int, data: bytes) -> None:
        self.dev.ctrl_transfer(
            REQ_TYPE_WRITE, REALTEK_VENDOR_REQUEST, addr, 0, data, _CTRL_TIMEOUT_MS
        )

    def read8(self, addr: int) -> int:
        return self._read(addr, 1)[0]

    def read16(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 2), "little")

    def read32(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 4), "little")

    def write8(self, addr: int, value: int) -> None:
        self._write(addr, bytes([value & 0xFF]))

    def write16(self, addr: int, value: int) -> None:
        self._write(addr, struct.pack("<H", value & 0xFFFF))

    def write32(self, addr: int, value: int) -> None:
        self._write(addr, struct.pack("<I", value & 0xFFFFFFFF))

    # --- bulk OUT (firmware packets) --------------------------------------
    def bulk_out(self, data: bytes) -> None:
        self.dev.write(EP_BULK_OUT_FW, data, _BULK_TIMEOUT_MS)

    # --- bulk IN (RX) ------------------------------------------------------
    def _bulk_in_ep(self) -> int:
        """Probe the active interface for the bulk-IN (RX) endpoint, cached."""
        if self._in_ep is not None:
            return self._in_ep
        cfg = self.dev.get_active_configuration()
        for intf in cfg:
            for ep in intf:
                if (usb.util.endpoint_direction(ep.bEndpointAddress)
                        == usb.util.ENDPOINT_IN
                        and usb.util.endpoint_type(ep.bmAttributes)
                        == usb.util.ENDPOINT_TYPE_BULK):
                    self._in_ep = ep.bEndpointAddress
                    return self._in_ep
        raise RuntimeError("RTL8814AU: no bulk-IN endpoint on the active interface")

    def bulk_in(self, size: int = RX_BUF_SIZE, timeout: int = RX_TIMEOUT_MS):
        """One blocking bulk-IN read. Returns the raw buffer, or None on a benign
        timeout (no traffic). Raises usb.core.USBError on a real pipe fault."""
        try:
            return bytes(self.dev.read(self._bulk_in_ep(), size, timeout))
        except usb.core.USBError as e:
            # libusb timeout (errno 110 / LIBUSB_ERROR_TIMEOUT) is benign — no
            # traffic this interval; anything else is a real fault, propagate it.
            if getattr(e, "errno", None) == 110 or "timeout" in str(e).lower():
                return None
            raise

    def close(self) -> None:
        usb.util.dispose_resources(self.dev)
