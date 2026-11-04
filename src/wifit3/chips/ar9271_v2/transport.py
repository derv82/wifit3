"""Raw USB transport for the AR9271 (ath9k_htc): the four ath9k pipes as Python calls, and the
REG_IN reader that hands each frame to ``ath9k_htc_rx_msg`` (the hif_usb layer)."""
from __future__ import annotations

import asyncio
import threading
from typing import Callable

import usb

from wifit3.errors import is_device_gone

from . import constants as C
from .htc import HTCTarget


class AR9271Transport:
    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self.htc = HTCTarget()
        self._reg_in_thread: threading.Thread | None = None
        self._reg_in_running = False

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

    def reg_in(self, length: int = C.MAX_REG_IN_BUF_SIZE) -> bytes:
        """Read one HTC control / WMI event frame from the REG_IN interrupt pipe (EP 0x83)
        [SRC] hif_usb.c:781-783 usb_rcvintpipe(USB_REG_IN_PIPE)."""
        return bytes(self.dev.read(C.EP_REG_IN, length, C.USB_MSG_TIMEOUT))

    def wlan_out(self, data: bytes) -> int:
        """Send a TX/HTC data frame on the WLAN_TX bulk pipe (EP 0x01) [SRC] hif_usb.c:206-207."""
        return self.dev.write(C.EP_WLAN_TX, data, C.USB_MSG_TIMEOUT)

    def wlan_in(self, length: int = 4096) -> bytes:
        """Read from the WLAN_RX bulk pipe (EP 0x82) [SRC] hif_usb.c:923-925."""
        return bytes(self.dev.read(C.EP_WLAN_RX, length, C.USB_MSG_TIMEOUT))

    def start_reg_in(self, loop: asyncio.AbstractEventLoop, on_lost: Callable[[Exception], None]) -> None:
        """ath9k_hif_usb_alloc_reg_in_urbs [SRC] hif_usb.c:964-1015: a REG_IN read is always posted, and
        each frame goes to ath9k_htc_rx_msg on ``loop``."""
        self._reg_in_running = True
        self._reg_in_thread = threading.Thread(target=self._reg_in_cb_loop, args=(loop, on_lost),
                                               name="ar9271v2-reg-in", daemon=True)
        self._reg_in_thread.start()

    def stop_reg_in(self) -> None:
        """ath9k_hif_usb_dealloc_reg_in_urbs [SRC] hif_usb.c:959-962; returns within one read timeout."""
        self._reg_in_running = False
        thread, self._reg_in_thread = self._reg_in_thread, None
        if thread is not None:
            thread.join(C.USB_MSG_TIMEOUT / 1000 * 2)

    def _reg_in_cb_loop(self, loop: asyncio.AbstractEventLoop, on_lost: Callable[[Exception], None]) -> None:
        """ath9k_hif_usb_reg_in_cb [SRC] hif_usb.c:734-795: a gone device ends it, any other error resubmits."""
        while self._reg_in_running:
            try:
                frame = self.reg_in()
            except usb.core.USBTimeoutError:
                continue
            except usb.core.USBError as error:
                if is_device_gone(error):
                    loop.call_soon_threadsafe(on_lost, error)
                    return
                continue
            if frame:
                loop.call_soon_threadsafe(self.htc.rx_msg, frame)

    def wait_for_completion_timeout(self, completion: threading.Event, timeout: float) -> bool:
        """Waits for a REG_IN frame, handed to ath9k_htc_rx_msg by the reader, to complete ``completion``."""
        return completion.wait(timeout)
