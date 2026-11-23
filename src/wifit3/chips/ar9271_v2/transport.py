"""Raw USB transport for the AR9271 (ath9k_htc).

Thin wrapper over a ``usb.core.Device`` (or the verify harness's ReplayDevice): it
exposes the four ath9k pipes as Python calls. The protocol logic (firmware download,
HTC/WMI framing, RX reassembly) lives in the sibling modules; this layer only moves bytes.

M1 wires the cold-boot firmware-download path (vendor control-OUT on EP0). The bulk/
interrupt pipes (HTC/WMI + RX/TX) land with M2.
"""
from __future__ import annotations

import errno
import logging
import queue
import threading
from typing import Callable

import usb

from wifit3.errors import is_device_gone

from . import constants as C

logger = logging.getLogger(__name__)


class AR9271Transport:
    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self.removed = False
        self.reg_in_frames: queue.Queue[bytes] = queue.Queue()
        self.htc_rx_msg: Callable[[bytes], None] = self.reg_in_frames.put
        self.reg_in_thread: threading.Thread | None = None

    def control_out(self, bRequest: int, wValue: int, data: bytes | None) -> int:
        """Vendor host->device control transfer on EP0 (bmRequestType 0x40).

        The firmware-download path is the only EP0 traffic the kernel issues to this
        chip; ``wIndex`` is always 0 and the request type is fixed [SRC] hif_usb.c:1084.
        """
        return self.dev.ctrl_transfer(C.BMREQ_VENDOR_OUT, bRequest, wValue, 0,
                                      data if data is not None else 0, C.USB_MSG_TIMEOUT)

    def reg_out(self, data: bytes) -> int:
        """Send an HTC control / WMI command frame on the REG_OUT interrupt pipe (EP 0x04)
        [SRC] hif_usb.c:119-120 usb_sndintpipe(USB_REG_OUT_PIPE). ENODEV marks the device
        removed, then every later reg_out / reg_in raises ENODEV without USB."""
        if self.removed:
            raise usb.core.USBError("device removed", errno=errno.ENODEV)
        try:
            return self.dev.write(C.EP_REG_OUT, data, C.USB_MSG_TIMEOUT)
        except usb.core.USBError as e:
            if is_device_gone(e):
                self.removed = True
            raise

    def reboot(self) -> None:
        """ath9k_hif_usb_reboot: 0xffffffff on REG_OUT sends the chip back to its bootloader."""
        try:
            self.dev.write(C.EP_REG_OUT, bytes([0xff] * 4), C.USB_MSG_TIMEOUT)
        except usb.core.USBError:
            logger.error("ath9k_htc: USB reboot failed")

    def alloc_reg_in_urbs(self) -> None:
        """Keep a REG_IN read pending until dealloc_reg_in_urbs; each frame goes to htc_rx_msg."""
        self.reg_in_thread = threading.Thread(target=self._reg_in_cb, name="ar9271v2-reg-in",
                                              daemon=True)
        self.reg_in_thread.start()

    def dealloc_reg_in_urbs(self) -> None:
        thread = self.reg_in_thread
        self.reg_in_thread = None
        if thread is not None:
            thread.join(C.USB_MSG_TIMEOUT / 1000 + 0.5)

    def _reg_in_cb(self) -> None:
        """ath9k_hif_usb_reg_in_cb: ENODEV stops the reads; any other error resubmits."""
        while self.reg_in_thread is not None:
            try:
                frame = bytes(self.dev.read(C.EP_REG_IN, 64, C.USB_MSG_TIMEOUT))
            except usb.core.USBError as e:
                if is_device_gone(e):
                    self.removed = True
                    return
                continue
            if frame:
                self.htc_rx_msg(frame)

    def reg_in(self, timeout_ms: int = C.USB_MSG_TIMEOUT, length: int = 64) -> bytes:
        """Read one HTC control / WMI event frame from the REG_IN interrupt pipe (EP 0x83)
        [SRC] hif_usb.c:781-783 usb_rcvintpipe(USB_REG_IN_PIPE). ENODEV as reg_out."""
        if self.removed:
            raise usb.core.USBError("device removed", errno=errno.ENODEV)
        if self.reg_in_thread is not None:
            try:
                return self.reg_in_frames.get(timeout=timeout_ms / 1000)
            except queue.Empty:
                raise usb.core.USBTimeoutError("Operation timed out", errno=errno.ETIMEDOUT) from None
        try:
            return bytes(self.dev.read(C.EP_REG_IN, length, timeout_ms))
        except usb.core.USBError as e:
            if is_device_gone(e):
                self.removed = True
            raise

    def wlan_out(self, data: bytes) -> int:
        """Send a TX/HTC data frame on the WLAN_TX bulk pipe (EP 0x01) [SRC] hif_usb.c:206-207."""
        return self.dev.write(C.EP_WLAN_TX, data, C.USB_MSG_TIMEOUT)

    def wlan_in(self, length: int = 4096) -> bytes:
        """Read from the WLAN_RX bulk pipe (EP 0x82) [SRC] hif_usb.c:923-925."""
        return bytes(self.dev.read(C.EP_WLAN_RX, length, C.USB_MSG_TIMEOUT))
