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

        # Execute the 'Incantation' with high-res precision for EVERY command.
        # This provides a steady heartbeat the hardware needs to stay in sync.
        total_cmds = len(FULL_BOOT_SEQUENCE)
        logger.info(f"Blasting {total_cmds} setup commands (Precise Heartbeat)...")
        
        for i, (cmd_type, wVal, wIdx, data, delay) in enumerate(FULL_BOOT_SEQUENCE):
            try:
                if cmd_type == "WRITE":
                    self.transport.write_custom(wVal, wIdx, data)
                elif cmd_type == "READ":
                    self.transport.read_custom(wVal, wIdx, data) 
                elif cmd_type == "SET_CONFIG":
                    self.dev.set_configuration()
                
                # Honor the recorded delay with microsecond precision.
                # Small gaps (like 1ms) will use a high-res busy-wait.
                # Long gaps (like 200ms) will yield the CPU via asyncio.sleep.
                await self._high_res_sleep(delay)

                if i > 0 and i % 500 == 0:
                    logger.info(f"Initialization Progress: {i}/{total_cmds} steps completed.")
                    
            except usb.core.USBError as e:
                if cmd_type != "SET_CONFIG":
                    logger.error(f"FATAL: USB Error during boot sequence at step {i} (Reg {hex(wVal)}): {e}")
                    return False

        # Reset pipes to clear stalls and flush FIFO buffers (Crucial step from mvp.py)
        self.transport.reset_pipes()
        await asyncio.sleep(0.1) # Breather after incantation

        logger.info("RTL8187 Hardware Ready (MAC Read Skipped to prevent state corruption).")
        
        # Start read loop
        self.is_running = True
        self._read_task = asyncio.create_task(self._read_loop())
        
        return True

    async def _high_res_sleep(self, seconds: float):
        """
        Precise sleep that bypasses Windows' 15.6ms timer resolution.
        Uses asyncio.sleep for the bulk, and time.perf_counter() for the tail.
        """
        import time
        start = time.perf_counter()
        
        # If delay is long enough, yield the CPU for the majority of it.
        # We leave a 20ms buffer to ensure we don't 'over-sleep' due to OS jitter.
        if seconds > 0.030:
            await asyncio.sleep(seconds - 0.020)
            
        # Precise busy-wait for the remaining time.
        while (time.perf_counter() - start) < seconds:
            pass

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
        Currently supports Ch 1 and 6 via extracted PCAP sequences.
        """
        if channel == self.current_channel:
            return True
            
        logger.info(f"Tuning RTL8187 to Channel {channel}...")
        
        try:
            from .sequences.tuning import TUNING_SEQUENCES
        except ImportError:
            logger.error("Could not find RTL8187 tuning sequences.")
            return False
            
        if channel not in TUNING_SEQUENCES:
            logger.error(f"Channel {channel} is not yet supported for RTL8187.")
            return False
            
        sequence = TUNING_SEQUENCES[channel]
        
        for i, (cmd_type, wVal, wIdx, data, delay) in enumerate(sequence):
            try:
                if cmd_type == "WRITE":
                    self.transport.write_custom(wVal, wIdx, data)
                elif cmd_type == "READ":
                    self.transport.read_custom(wVal, wIdx, data)
                
                await self._high_res_sleep(delay)
                    
            except usb.core.USBError as e:
                logger.error(f"USB Error during channel tuning at step {i}: {e}")
                return False

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
