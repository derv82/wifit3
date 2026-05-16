import usb.core
import logging
import asyncio
from typing import Optional

from .constants import (
    MT_VEND_REQ_IN, MT_VEND_REQ_OUT, EP_IN_BULK, EP_OUT_BULK
)

logger = logging.getLogger(__name__)

class MT7921AUTransport:
    """
    Transport layer for MT7921AU.
    Handles Vendor Control Transfers for register access and MCU commands.
    """
    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self._loop = asyncio.get_event_loop()
        self._rx_task: Optional[asyncio.Task] = None
        self._callback = None
        self._is_running = False

    def subscribe(self, callback):
        self._callback = callback

    async def start(self):
        if self._is_running: return
        self._is_running = True
        self._rx_task = asyncio.create_task(self._poll_loop())
        logger.info("MT7921AU Transport started.")

    async def stop(self):
        self._is_running = False
        if self._rx_task:
            self._rx_task.cancel()
            try:
                await self._rx_task
            except asyncio.CancelledError:
                pass
        logger.info("MT7921AU Transport stopped.")

    async def send_bulk(self, data: bytes):
        """Sends a raw packet to the Bulk OUT endpoint."""
        try:
            # dev.write is blocking, so we run it in an executor.
            await self._loop.run_in_executor(
                None, lambda: self.dev.write(EP_OUT_BULK, data, timeout=100)
            )
        except usb.core.USBError as e:
            logger.error(f"Failed to send bulk data: {e}")

    async def _poll_loop(self):
        while self._is_running:
            try:
                # Polling for data
                data = await self._loop.run_in_executor(
                    None, lambda: self.dev.read(EP_IN_BULK, 4096, timeout=100)
                )
                if data and self._callback:
                    self._callback(bytes(data))
            except usb.core.USBTimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Transport read error: {e}")
                await asyncio.sleep(0.1)

    def send_vendor_request(self, bmRequestType: int, bRequest: int, wValue: int, wIndex: int, data: bytes = b""):
        """
        Sends a vendor-specific control transfer.
        """
        try:
            self.dev.ctrl_transfer(
                bmRequestType=bmRequestType,
                bRequest=bRequest,
                wValue=wValue,
                wIndex=wIndex,
                data_or_wLength=data,
                timeout=1000
            )
        except usb.core.USBError as e:
            logger.debug(f"Vendor request failed ({hex(bmRequestType)} {hex(bRequest)} {hex(wValue)} {hex(wIndex)}): {e}")

    def read_vendor_request(self, bmRequestType: int, bRequest: int, wValue: int, wIndex: int, wLength: int) -> bytes:
        """
        Reads data via vendor-specific control transfer.
        """
        try:
            return bytes(self.dev.ctrl_transfer(
                bmRequestType=bmRequestType,
                bRequest=bRequest,
                wValue=wValue,
                wIndex=wIndex,
                data_or_wLength=wLength,
                timeout=1000
            ))
        except usb.core.USBError as e:
            logger.debug(f"Vendor read failed ({hex(bmRequestType)} {hex(bRequest)} {hex(wValue)} {hex(wIndex)}): {e}")
            return b""
