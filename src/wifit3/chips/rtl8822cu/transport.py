"""RTL8822CU USB transport and endpoint inspection."""
from __future__ import annotations

from dataclasses import dataclass

import usb.core

from wifit3.chips.rtw88_base.transport import Rtw88Transport

from .constants import (
    USB_ENDPOINT_DIR_MASK,
    USB_ENDPOINT_TYPE_BULK,
    USB_ENDPOINT_TYPE_MASK,
    USB_INTERFACE_CLASS_VENDOR,
)


@dataclass(frozen=True)
class EndpointLayout:
    interface: int
    bulk_in: tuple[int, ...]
    bulk_out: tuple[int, ...]


# ON-section page-switch echo, guarded #if CONFIG_RTL8822B||8821C||8822C||8822E
# [SRC os_dep/linux/usb_ops_linux.c:172-203]: every vendor access to an ON-section
# register (addr < 0xFE00 and (addr <= 0xFF or 0x1000..0x10FF)) is followed by a
# 1-byte write to 0x4E0 carrying IO-buffer byte[0] (read-back byte for a read, the
# written byte for a write).
REG_PAGE_SWITCH_ECHO = 0x04E0


def _is_on_section(addr: int) -> bool:
    return addr < 0xFE00 and (addr <= 0xFF or 0x1000 <= addr <= 0x10FF)


class RTL8822CUTransport(Rtw88Transport):
    """RTL8822C uses the common rtw88 vendor-control protocol, plus the ON-section
    0x4E0 echo the 8822b/8821c/8822c/8822e USB path appends to each such access."""

    def __init__(self, dev: usb.core.Device, timeout_ms: int = 500):
        super().__init__(dev, timeout_ms)

    def _echo(self, low_byte: int) -> None:
        self.dev.ctrl_transfer(0x40, 0x05, REG_PAGE_SWITCH_ECHO, 0x00,
                               bytes([low_byte & 0xFF]), self.timeout_ms)

    def _read(self, addr: int, length: int) -> bytes:
        data = super()._read(addr, length)
        if _is_on_section(addr):
            self._echo(data[0] if data else 0)
        return data

    def _write(self, addr: int, payload) -> None:
        super()._write(addr, payload)
        if _is_on_section(addr):
            payload = bytes(payload)
            self._echo(payload[0] if payload else 0)

    def endpoints(self) -> EndpointLayout:
        cfg = self.dev.get_active_configuration()
        for intf in cfg:
            if intf.bInterfaceClass != USB_INTERFACE_CLASS_VENDOR:
                continue
            ins: list[int] = []
            outs: list[int] = []
            for ep in intf:
                if (ep.bmAttributes & USB_ENDPOINT_TYPE_MASK) != USB_ENDPOINT_TYPE_BULK:
                    continue
                if ep.bEndpointAddress & USB_ENDPOINT_DIR_MASK:
                    ins.append(ep.bEndpointAddress)
                else:
                    outs.append(ep.bEndpointAddress)
            if ins or outs:
                return EndpointLayout(intf.bInterfaceNumber, tuple(ins), tuple(outs))
        raise RuntimeError("RTL8822CU: no vendor-specific bulk interface")
