"""RTL8188EUS raw USB transport — Realtek vendor control + bulk I/O.

Every register access is a single vendor request (0x05): wValue = 16-bit register
offset, wIndex = 0, data stage 1/2/4 bytes little-endian [SRC] os_dep/linux/
usb_ops_linux.c usbctrl_vendorreq. Unlike the Jaguar-series cards, the 8188e
uploads firmware over EP0 too: ``write_block`` is a wide (<=196 B) vendor control
write to the FW SRAM window (rtw_writeN) [SRC] rtl8188e_hal_init.c _BlockWrite.

The bring-up code is transport-agnostic (duck-typed read8/16/32, write8/16/32,
write_block, bulk_in/out), so the same routines drive this PyUSB transport on
hardware and the pcap-replay transport in the verifiers.
"""
from __future__ import annotations

import struct

import usb.core
import usb.util

from .constants import REALTEK_VENDOR_REQUEST, REQ_TYPE_READ, REQ_TYPE_WRITE

_CTRL_TIMEOUT_MS = 1000
_BULK_TIMEOUT_MS = 1000
# RX bulk-IN read: the buffer must hold a whole USB-aggregated transfer; read
# generously and treat a timeout (quiet channel) as benign.
RX_BUF_SIZE = 0x8000
RX_TIMEOUT_MS = 200


class Rtl8188eusTransport:
    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self._in_ep = None   # bulk-IN endpoint, probed lazily
        self._out_ep = None  # bulk-OUT endpoint, probed lazily

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

    def write_block(self, addr: int, data: bytes) -> None:
        """Wide vendor control write (firmware-page chunks, <=196 B)."""
        self._write(addr, bytes(data))

    # --- bulk OUT (TX) -----------------------------------------------------
    def _bulk_out_ep(self) -> int:
        if self._out_ep is not None:
            return self._out_ep
        cfg = self.dev.get_active_configuration()
        for intf in cfg:
            for ep in intf:
                if (usb.util.endpoint_direction(ep.bEndpointAddress)
                        == usb.util.ENDPOINT_OUT
                        and usb.util.endpoint_type(ep.bmAttributes)
                        == usb.util.ENDPOINT_TYPE_BULK):
                    self._out_ep = ep.bEndpointAddress
                    return self._out_ep
        raise RuntimeError("RTL8188EUS: no bulk-OUT endpoint on the active interface")

    def bulk_out(self, data: bytes) -> None:
        self.dev.write(self._bulk_out_ep(), data, _BULK_TIMEOUT_MS)

    # --- bulk IN (RX) ------------------------------------------------------
    def _bulk_in_ep(self) -> int:
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
        raise RuntimeError("RTL8188EUS: no bulk-IN endpoint on the active interface")

    def bulk_in(self, size: int = RX_BUF_SIZE, timeout: int = RX_TIMEOUT_MS):
        """One blocking bulk-IN read; None on a benign timeout (quiet channel)."""
        try:
            return bytes(self.dev.read(self._bulk_in_ep(), size, timeout))
        except usb.core.USBTimeoutError:
            return None
        except usb.core.USBError as e:
            if (getattr(e, "errno", None) in (110, 10060)
                    or "timed out" in str(e).lower() or "timeout" in str(e).lower()):
                return None
            raise

    def close(self) -> None:
        usb.util.dispose_resources(self.dev)
