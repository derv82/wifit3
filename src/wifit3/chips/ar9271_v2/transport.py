"""Raw USB transport for the AR9271 (ath9k_htc).

Thin wrapper over a ``usb.core.Device`` (or the verify harness's ReplayDevice): it
exposes the four ath9k pipes as Python calls. The protocol logic (firmware download,
HTC/WMI framing, RX reassembly) lives in the sibling modules; this layer only moves bytes.

M1 wires the cold-boot firmware-download path (vendor control-OUT on EP0). The bulk/
interrupt pipes (HTC/WMI + RX/TX) land with M2.
"""
from __future__ import annotations

from . import constants as C


class AR9271Transport:
    def __init__(self, dev):
        self.dev = dev

    def control_out(self, bRequest: int, wValue: int, data: bytes | None) -> int:
        """Vendor host->device control transfer on EP0 (bmRequestType 0x40).

        The firmware-download path is the only EP0 traffic the kernel issues to this
        chip; ``wIndex`` is always 0 and the request type is fixed [SRC] hif_usb.c:1084.
        """
        return self.dev.ctrl_transfer(C.BMREQ_VENDOR_OUT, bRequest, wValue, 0,
                                      data if data is not None else 0, C.USB_MSG_TIMEOUT)
