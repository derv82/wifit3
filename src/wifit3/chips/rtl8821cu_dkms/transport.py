"""RTL8821CU USB transport — Realtek vendor control transfers + the 8821c
register-page-switch mirror.

Every register access is one bRequest 0x05 vendor control transfer: read uses
bmRequestType 0xC0, write 0x40, the 16-bit register address in wValue, wIndex 0.
[SRC] include/usb_ops.h:19-22, os_dep/linux/usb_ops_linux.c:26-260.

8821c quirk reproduced here: usbctrl_vendorreq() follows every access to an
ON-section register (addr <= 0xFF or 0x1000..0x10FF) with an extra 1-byte write
to 0x4E0 carrying the low byte of the IO buffer — the read-back value for a read,
the written value for a write. [SRC] os_dep/linux/usb_ops_linux.c:171-201. This
lives at the transport layer (below the HAL), so it is dev-centric: it calls
``dev.ctrl_transfer`` directly, which lets ``scripts/rtw88_pcap_replay.ReplayDevice``
drive the real transport unchanged and byte-check the mirror against the capture.

Shares no code with the other Realtek transports by design (anti-DRY); the
mechanism is identical but the copy is independent so a change here can't regress
a sibling card.
"""
from __future__ import annotations

import usb.core
import usb.util

from .constants import (
    MAX_VENDOR_REQ_CMD_SIZE,
    ON_SEC_RANGES,
    REALTEK_USB_VENQT_CMD_IDX,
    REALTEK_USB_VENQT_CMD_REQ,
    REALTEK_USB_VENQT_READ,
    REALTEK_USB_VENQT_WRITE,
    REG_PAGE_SWITCH_CONFIRM,
)

CTRL_TIMEOUT_MS = 500
RX_BUF_SIZE = 0x8000
RX_TIMEOUT_MS = 200
_BULK_TIMEOUT_MS = 1000


def _is_on_sec(addr: int) -> bool:
    """ON-section register: the page-switch confirm fires only for these.
    [SRC] os_dep/linux/usb_ops_linux.c:172-178 (value<0xFE00: <=0xFF or 0x1000..0x10FF)."""
    return any(lo <= addr <= hi for lo, hi in ON_SEC_RANGES)


class Rtl8821cuTransport:
    def __init__(self, dev: usb.core.Device, bulk_out_ep: int = 0x04):
        self.dev = dev
        self._bulk_out_ep = bulk_out_ep
        self._in_ep = None  # bulk-IN (RX) endpoint, probed lazily

    # --- vendor control transfers (with the ON-section page-switch mirror) ---
    def _mirror(self, low_byte: int) -> None:
        """The post-access 1-byte write to 0x4E0. 0x4E0 is not itself ON-section,
        so it does not recurse. [SRC] os_dep/linux/usb_ops_linux.c:183-200."""
        self.dev.ctrl_transfer(REALTEK_USB_VENQT_WRITE, REALTEK_USB_VENQT_CMD_REQ,
                               REG_PAGE_SWITCH_CONFIRM, REALTEK_USB_VENQT_CMD_IDX,
                               bytes([low_byte & 0xFF]), CTRL_TIMEOUT_MS)

    def _read(self, addr: int, length: int) -> bytes:
        ret = bytes(self.dev.ctrl_transfer(
            REALTEK_USB_VENQT_READ, REALTEK_USB_VENQT_CMD_REQ,
            addr & 0xFFFF, REALTEK_USB_VENQT_CMD_IDX, length, CTRL_TIMEOUT_MS))
        if _is_on_sec(addr):
            self._mirror(ret[0] if ret else 0)
        return ret

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
        if _is_on_sec(addr):
            self._mirror(data[0] if data else 0)

    def write8(self, addr: int, val: int) -> None:
        self.writeN(addr, (val & 0xFF).to_bytes(1, "little"))

    def write16(self, addr: int, val: int) -> None:
        self.writeN(addr, (val & 0xFFFF).to_bytes(2, "little"))

    def write32(self, addr: int, val: int) -> None:
        self.writeN(addr, (val & 0xFFFFFFFF).to_bytes(4, "little"))

    # --- bulk OUT (FW/TX) / bulk IN (RX) -------------------------------------
    def bulk_out(self, data: bytes) -> None:
        self.dev.write(self._bulk_out_ep, data, _BULK_TIMEOUT_MS)

    def _bulk_in_ep(self) -> int:
        if self._in_ep is not None:
            return self._in_ep
        cfg = self.dev.get_active_configuration()
        for intf in cfg:
            for ep in intf:
                if (usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN
                        and usb.util.endpoint_type(ep.bmAttributes)
                        == usb.util.ENDPOINT_TYPE_BULK):
                    self._in_ep = ep.bEndpointAddress
                    return self._in_ep
        raise RuntimeError("RTL8821CU: no bulk-IN endpoint on the active interface")

    def bulk_in(self, size: int = RX_BUF_SIZE, timeout: int = RX_TIMEOUT_MS):
        """One blocking bulk-IN read. None on a benign timeout (quiet channel)."""
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
