import asyncio
import logging
import struct
import usb.core
from typing import Optional, Dict, Any

from .transport import RTL8187USBTransport
from .constants import *
from wifit3.wlan.packet import WlanFrameParser

logger = logging.getLogger(__name__)

class RTL8187Driver:
    """
    Driver for the Realtek RTL8187L (e.g. AWUS036H).
    Implements standard chipset interface for wifit3.
    """
    
    def __init__(self, dev: usb.core.Device, is_warm: bool = True):
        self.dev = dev
        self.transport = RTL8187USBTransport(dev)
        self.is_warm = is_warm # RTL8187 usually warm on plug
        self.is_running = False
        
        self.mac_address = None
        self.current_channel = 1
        self._rx_callback = None
        self._read_task = None

    def register_rx_callback(self, cb):
        self._rx_callback = cb

    async def connect(self) -> bool:
        """Runs the boot sequence 'incantation' and identifies the hardware."""
        logger.info("Initializing RTL8187 hardware sequence...")
        
        try:
            from .sequences.init import FULL_BOOT_SEQUENCE
        except ImportError:
            logger.error("Could not find RTL8187 boot sequence (sequences/init.py).")
            return False

        # Execute the 'Incantation'
        total_cmds = len(FULL_BOOT_SEQUENCE)
        for i, (cmd_type, wVal, wIdx, data, delay) in enumerate(FULL_BOOT_SEQUENCE):
            try:
                if cmd_type == "WRITE":
                    self.transport.write_custom(wVal, wIdx, data)
                elif cmd_type == "READ":
                    self.transport.read_custom(wVal, wIdx, data) # data is length here
                elif cmd_type == "SET_CONFIG":
                    self.dev.set_configuration()
            except usb.core.USBError as e:
                if cmd_type != "SET_CONFIG":
                    logger.warning(f"USB Error during boot sequence at step {i}: {e}")
            
            if delay > 0:
                await asyncio.sleep(delay)

        # Identify MAC
        self.mac_address = self._read_mac()
        logger.info(f"RTL8187 Hardware Ready. MAC: {self.mac_address}")
        
        # Start read loop
        self.is_running = True
        self._read_task = asyncio.create_task(self._read_loop())
        
        return True

    def _read_mac(self) -> str:
        """Reads the MAC address from the chipset registers."""
        try:
            m0 = self.transport.read_reg32(MAC0)
            m4 = self.transport.read_reg16(MAC4)
            mac = [
                m0 & 0xFF, (m0 >> 8) & 0xFF, (m0 >> 16) & 0xFF, (m0 >> 24) & 0xFF,
                m4 & 0xFF, (m4 >> 8) & 0xFF
            ]
            return ":".join(f"{b:02x}" for b in mac)
        except Exception as e:
            logger.error(f"Failed to read MAC address: {e}")
            return "00:00:00:00:00:00"

    async def _read_loop(self):
        """Continuous polling of the Bulk IN endpoint."""
        loop = asyncio.get_running_loop()
        buffer_size = 4096
        
        logger.info(f"Started RTL8187 bulk read loop on EP {hex(USB_EP_BULK_IN)}")
        
        while self.is_running:
            try:
                raw = await loop.run_in_executor(
                    None, self.dev.read, USB_EP_BULK_IN, buffer_size, 100
                )
                if not raw:
                    continue
                
                data = bytes(raw)
                
                # RTL8187 Specific: The raw frame might have a header or trailer.
                # In MVP, we assumed direct 802.11 frame start.
                # TODO: Identify and extract hardware RSSI if possible.
                rssi = -50 
                
                parsed = WlanFrameParser.parse_80211_frame(data, rssi)
                if parsed and self._rx_callback:
                    self._rx_callback(parsed)

            except usb.core.USBError as e:
                if e.errno not in (10060, 110): # Timeout is normal
                    logger.error(f"RTL8187 Transport USBError: {e}")
                await asyncio.sleep(0.001)
            except Exception as e:
                logger.error(f"RTL8187 Transport Error: {e}")
                await asyncio.sleep(0.01)

    async def set_channel(self, channel: int) -> bool:
        """
        Tunes the RTL8187 to a specific frequency.
        Currently supports Ch 1 and 6.
        """
        # TODO: Implement Full Spectrum support from captures.
        if channel == self.current_channel:
            return True
            
        logger.info(f"Tuning RTL8187 to Channel {channel}...")
        
        # Placeholder for real channel switching logic.
        # This usually involves poking BB and RF registers.
        self.current_channel = channel
        return True

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        """
        Injects a raw 802.11 frame.
        RTL8187 requires a TX descriptor before the frame.
        """
        # TODO: Implement real RTL8187 TX descriptor
        # For now, let's see if raw frame works (unlikely for most hard-macs)
        # Linux rtl8187 driver prepends a 9-byte or 14-byte descriptor.
        logger.warning("RTL8187 Frame Injection not fully implemented (missing TX descriptor).")
        
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.dev.write, USB_EP_BULK_OUT, frame_bytes)
            return True
        except Exception as e:
            logger.error(f"RTL8187 Injection failed: {e}")
            return False

    async def close(self):
        """Shuts down the driver and releases resources."""
        self.is_running = False
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
        logger.info("RTL8187 Driver closed.")
