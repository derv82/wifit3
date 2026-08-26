"""Raw USB transport for the AR9271 (ath9k_htc).

Thin wrapper over a ``usb.core.Device`` (or the verify harness's ReplayDevice): it
exposes the four ath9k pipes as Python calls. The protocol logic (firmware download,
HTC/WMI framing, RX reassembly) lives in the sibling modules; this layer only moves bytes.

M1 wires the cold-boot firmware-download path (vendor control-OUT on EP0). The bulk/
interrupt pipes (HTC/WMI + RX/TX) land with M2.
"""
from __future__ import annotations

import usb

from . import constants as C


class AR9271Transport:
    def __init__(self, dev: usb.core.Device):
        self.dev = dev

    def control_out(self, bRequest: int, wValue: int, data: bytes | None) -> int:
        """Vendor host->device control transfer on EP0 (bmRequestType 0x40).

        The firmware-download path is the only EP0 traffic the kernel issues to this
        chip; ``wIndex`` is always 0 and the request type is fixed [SRC] hif_usb.c:1084.
        """
        return self.dev.ctrl_transfer(C.BMREQ_VENDOR_OUT, bRequest, wValue, 0,
                                      data if data is not None else 0, C.USB_MSG_TIMEOUT)

    def reg_out(self, data: bytes) -> int:
        """Send an HTC control / WMI command frame on the REG_OUT interrupt pipe (EP 0x04)
        [SRC] hif_usb.c:119-120 usb_sndintpipe(USB_REG_OUT_PIPE)."""
        return self.dev.write(C.EP_REG_OUT, data, C.USB_MSG_TIMEOUT)

    def reg_in(self, length: int = 64) -> bytes:
        """Read one HTC control / WMI event frame from the REG_IN interrupt pipe (EP 0x83)
        [SRC] hif_usb.c:781-783 usb_rcvintpipe(USB_REG_IN_PIPE)."""
        return bytes(self.dev.read(C.EP_REG_IN, length, C.USB_MSG_TIMEOUT))

    def wlan_out(self, data: bytes) -> int:
        """Send a TX/HTC data frame on the WLAN_TX bulk pipe (EP 0x01) [SRC] hif_usb.c:206-207."""
        return self.dev.write(C.EP_WLAN_TX, data, C.USB_MSG_TIMEOUT)

    def wlan_in(self, length: int = 4096) -> bytes:
        """Read from the WLAN_RX bulk pipe (EP 0x82) [SRC] hif_usb.c:923-925."""
        return bytes(self.dev.read(C.EP_WLAN_RX, length, C.USB_MSG_TIMEOUT))
