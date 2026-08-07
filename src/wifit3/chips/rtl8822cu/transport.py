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


class RTL8822CUTransport(Rtw88Transport):
    """RTL8822C uses the common rtw88 vendor-control protocol."""

    def __init__(self, dev: usb.core.Device, timeout_ms: int = 500):
        super().__init__(dev, timeout_ms)

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
