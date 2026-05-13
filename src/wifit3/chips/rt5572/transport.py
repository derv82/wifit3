import usb.core
import logging
import asyncio
from typing import List, Union, Optional

from .constants import (
    USB_EP_BULK_IN, USB_EP_BULK_OUT, USB_SINGLE_WRITE, USB_SINGLE_READ, 
    USB_MULTI_WRITE, USB_MULTI_READ, USB_DEVICE_MODE, USB_EEPROM_READ,
    H2M_MAILBOX_CSR, HOST_CMD_CSR
)

logger = logging.getLogger(__name__)

class RT5572USBTransport:
    """
    Transport layer for Ralink rt5572 (rt2800usb).
    Handles Vendor Control Transfers for register access and firmware loading.
    """
    
    BM_REQ_VENDOR_OUT = 0x40
    BM_REQ_VENDOR_IN  = 0xc0

    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self._loop = asyncio.get_event_loop()
        self._rx_task: Optional[asyncio.Task] = None
        self._callback = None
        self._is_running = False

        # Debug: Log endpoints
        try:
            cfg = self.dev.get_active_configuration()
            intf = cfg[(0,0)]
            logger.info("Available USB Endpoints:")
            for ep in intf:
                logger.info(f"  EP: {hex(ep.bEndpointAddress)} (Type: {ep.bmAttributes & 0x03})")
        except Exception as e:
            logger.error(f"Failed to query endpoints: {e}")

    def subscribe(self, callback):
        self._callback = callback

    async def start(self):
        if self._is_running: return
        self._is_running = True
        self._rx_task = asyncio.create_task(self._poll_loop())
        logger.info("RT5572 Transport started (Bulk IN polling enabled).")

    async def stop(self):
        self._is_running = False
        if self._rx_task:
            self._rx_task.cancel()
            try:
                await self._rx_task
            except asyncio.CancelledError:
                pass
        logger.info("RT5572 Transport stopped.")

    async def send_bulk(self, data: bytes):
        """Sends a raw packet to the Bulk OUT endpoint."""
        await self._loop.run_in_executor(
            None, self.dev.write, USB_EP_BULK_OUT, data, 1000
        )

    async def _poll_loop(self):
        """
        Background loop to poll the Bulk IN endpoint for RX traffic.
        """
        while self._is_running:
            try:
                # Read from Bulk IN (0x82)
                # Max packet size for High-Speed USB Bulk is 512, 
                # but Ralink uses aggregation so we might get larger chunks.
                data = await self._loop.run_in_executor(
                    None, self.dev.read, USB_EP_BULK_IN, 4096, 100
                )
                if data and self._callback:
                    self._callback(bytes(data))
            except usb.core.USBError as e:
                # Timeout is normal (error code 60 or 'Operation timed out')
                if e.errno == 110 or "timeout" in str(e).lower():
                    await asyncio.sleep(0.01)
                    continue
                logger.error(f"USB Read Error: {e}")
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"Transport Poll Error: {e}")
                await asyncio.sleep(0.1)

    def write_reg32(self, reg: int, val: int):
        """4-byte register write (Little Endian)."""
        data = [
            val & 0xFF,
            (val >> 8) & 0xFF,
            (val >> 16) & 0xFF,
            (val >> 24) & 0xFF
        ]
        # Use MULTI_WRITE (0x06) for 32-bit registers as seen in trace
        self.dev.ctrl_transfer(self.BM_REQ_VENDOR_OUT, USB_MULTI_WRITE, 0, reg, data)

    def read_reg32(self, reg: int) -> int:
        """4-byte register read (Little Endian)."""
        # Use MULTI_READ (0x07) for 32-bit registers
        res = self.dev.ctrl_transfer(self.BM_REQ_VENDOR_IN, USB_MULTI_READ, 0, reg, 4)
        return res[0] | (res[1] << 8) | (res[2] << 16) | (res[3] << 24)

    def write_multi(self, addr: int, data: Union[bytes, List[int]]):
        """Multi-byte write (e.g., for firmware upload to 0x3000)."""
        self.dev.ctrl_transfer(self.BM_REQ_VENDOR_OUT, USB_MULTI_WRITE, 0, addr, data)

    def read_multi(self, addr: int, length: int) -> bytes:
        """Multi-byte read."""
        res = self.dev.ctrl_transfer(self.BM_REQ_VENDOR_IN, USB_MULTI_READ, 0, addr, length)
        return bytes(res)

    def write_eeprom(self, addr: int, val: int):
        """Write to EEPROM."""
        # TODO: Implement based on PCAP analysis
        pass

    def read_eeprom(self, addr: int) -> int:
        """Read from EEPROM."""
        # bRequest 9 is EEPROM read. addr is wIndex.
        res = self.dev.ctrl_transfer(self.BM_REQ_VENDOR_IN, 0x09, 0, addr, 2)
        return res[0] | (res[1] << 8)

    def set_device_mode(self, mode: int, value: int = 0):
        """Sets the device mode (bRequest 1)."""
        # value is wValue, mode is wIndex
        self.dev.ctrl_transfer(self.BM_REQ_VENDOR_OUT, USB_DEVICE_MODE, value, mode, None)

    def mcu_request(self, command: int, token: int = 0, arg0: int = 0, arg1: int = 0):
        """
        Sends an MCU request by writing to H2M_MAILBOX_CSR and HOST_CMD_CSR.
        """
        # 1. Prepare Mailbox
        # Bit 31: Owner (1 for Host)
        # Bits 24-31: Token
        # Bits 8-15: Arg1
        # Bits 0-7: Arg0
        mailbox = (1 << 31) | (token << 24) | (arg1 << 8) | arg0
        self.write_reg32(H2M_MAILBOX_CSR, mailbox)

        # 2. Trigger Command
        # Bits 0-7: Command
        host_cmd = command & 0xFF
        self.write_reg32(HOST_CMD_CSR, host_cmd)
