"""RTL8821AU (DKMS) USB transport — Realtek rtw88-family vendor control transfers.

Every register access is one bRequest 0x05 vendor control transfer: read uses
bmRequestType 0xC0, write 0x40, with the 16-bit register address in wValue and
wIndex 0. The firmware-download page writes ride this same control path in
≤196-byte payloads (`writeN`), not bulk-OUT — bulk endpoints are only used for
RX/TX later. [SRC] include/usb_ops.h:19-31, hal/hal_hci/hal_usb.c:338-540.

The read*/write*/writeN surface matches `scripts/rtw88_pcap_replay.ReplayTransport`
so the bring-up code runs unchanged against either real hardware or the pcap.
"""
from __future__ import annotations

import usb.core
import usb.util

from .constants import (
    MAX_VENDOR_REQ_CMD_SIZE,
    REALTEK_USB_VENQT_CMD_IDX,
    REALTEK_USB_VENQT_CMD_REQ,
    REALTEK_USB_VENQT_READ,
    REALTEK_USB_VENQT_WRITE,
)

CTRL_TIMEOUT_MS = 500  # [SRC] include/usb_ops_linux.h:22
# RX bulk-IN read: the buffer must hold a whole USB-aggregated transfer; read
# generously, with a short timeout so the reader thread stays responsive to stop()
# and returns None between bursts of traffic on a quiet channel.
RX_BUF_SIZE = 0x8000        # 32 KB >= the RX-DMA aggregation ceiling
RX_TIMEOUT_MS = 200
_BULK_TIMEOUT_MS = 1000


class RTL8821AUDkmsTransport:
    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self._in_ep = None  # bulk-IN (RX) endpoint address, probed lazily

    def _read(self, addr: int, length: int) -> bytes:
        return bytes(self.dev.ctrl_transfer(
            REALTEK_USB_VENQT_READ, REALTEK_USB_VENQT_CMD_REQ,
            addr & 0xFFFF, REALTEK_USB_VENQT_CMD_IDX, length, CTRL_TIMEOUT_MS))

    def read8(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 1), "little")

    def read16(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 2), "little")

    def read32(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 4), "little")

    def writeN(self, addr: int, data: bytes) -> None:
        data = bytes(data)
        if len(data) > MAX_VENDOR_REQ_CMD_SIZE:
            raise ValueError(f"vendor write {len(data)} > {MAX_VENDOR_REQ_CMD_SIZE} B")
        self.dev.ctrl_transfer(
            REALTEK_USB_VENQT_WRITE, REALTEK_USB_VENQT_CMD_REQ,
            addr & 0xFFFF, REALTEK_USB_VENQT_CMD_IDX, data, CTRL_TIMEOUT_MS)

    def write8(self, addr: int, val: int) -> None:
        self.writeN(addr, (val & 0xFF).to_bytes(1, "little"))

    def write16(self, addr: int, val: int) -> None:
        self.writeN(addr, (val & 0xFFFF).to_bytes(2, "little"))

    def write32(self, addr: int, val: int) -> None:
        self.writeN(addr, (val & 0xFFFFFFFF).to_bytes(4, "little"))

    # --- bulk OUT (firmware uses control writes; bulk-OUT is TX, ep 0x09) ---
    def bulk_out(self, data: bytes) -> None:
        self.dev.write(0x09, data, _BULK_TIMEOUT_MS)

    # --- bulk IN (RX, ep 0x84) --------------------------------------------
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
        raise RuntimeError("RTL8821AU: no bulk-IN endpoint on the active interface")

    def bulk_in(self, size: int = RX_BUF_SIZE, timeout: int = RX_TIMEOUT_MS):
        """One blocking bulk-IN read. Returns the raw buffer, or None on a benign
        timeout (no traffic this interval). Raises usb.core.USBError on a real fault.

        A read timeout is benign and common (every quiet channel yields one). pyusb
        raises USBTimeoutError for it; catch that type rather than sniffing the message,
        because the Windows/WinUSB backend reports errno 10060 "Operation timed out"
        (no "timeout" substring), so a message check silently misses it and the reader
        counts benign timeouts as fatal.
        """
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
