import asyncio
import logging
import struct
import time
import usb.core
from typing import Optional, Dict, Any

from .transport import RTL8187USBTransport
from .constants import *
from wifit3.engine.protocols import DeviceID
from wifit3.wlan.packet import WlanFrameParser

logger = logging.getLogger(__name__)

class RTL8187Driver:
    """
    Driver for the Realtek RTL8187L (e.g. AWUS036H).
    Implements standard chipset interface for wifit3.
    """

    SUPPORTED_IDS = [
        DeviceID(0x0bda, 0x8187, "Realtek RTL8187L / ALFA AWUS036H"),
    ]
    # 2.4 GHz only.
    SUPPORTED_CHANNELS = list(range(1, 14))

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RTL8187Driver":
        return cls(dev)

    def __init__(self, dev: usb.core.Device, is_warm: bool = False):
        self.dev = dev
        self.transport = RTL8187USBTransport(dev)
        self.is_warm = is_warm
        self.is_running = False
        
        self.mac_address = None
        self.current_channel = 1
        self._rx_callback = None
        self._read_task = None

    def register_rx_callback(self, cb):
        self._rx_callback = cb

    async def connect(self, progress_cb=None) -> bool:
        """Runs the boot sequence 'incantation' and identifies the hardware."""
        def _update(pct, msg):
            if progress_cb:
                progress_cb(pct, msg)
            logger.info(f"Progress {int(pct*100)}%: {msg}")

        _update(0.05, "Probing RTL8187 hardware state...")
        
        # Passive Detection: Try to read from Bulk IN (EP 0x81).
        # Warm hardware will either have data or a standard timeout.
        # Cold hardware often rejects or stalls the Bulk pipe early.
        try:
            loop = asyncio.get_running_loop()
            # Short read attempt to see if firmware is responsive
            data = await loop.run_in_executor(None, self.dev.read, USB_EP_BULK_IN, 64, 100)
            if len(data) >= 0:
                self.is_warm = True
        except usb.core.USBError:
            self.is_warm = False

        if self.is_warm:
            _update(0.1, "Device is already WARM. Skipping bootstrap sequence.")
            # self.transport.reset_pipes()
            self.is_running = True
            self._read_task = asyncio.create_task(self._read_loop())
            _update(1.0, "RTL8187 Driver successfully re-attached.")
            return True

        # --- HEAVY LIFTING OFF-RAMP ---
        # We offload the 5,000+ packet procedural grind to a background thread.
        # This keeps the main UI event loop free to process 'DriverProgress' messages.
        success = await asyncio.to_thread(self._bootstrap_sync, _update)
        
        if success:
            # Start read loop in the main async thread
            self.is_running = True
            self._read_task = asyncio.create_task(self._read_loop())
            _update(1.0, "RTL8187 Driver successfully connected.")
            return True
        
        return False

    def _bootstrap_sync(self, update_cb) -> bool:
        """
        Synchronous version of the boot sequence intended for a background thread.
        Uses time.sleep for hardware-enforced timing precision.
        """
        update_cb(0.05, "Initializing RTL8187 hardware sequence...")
        
        try:
            from .sequences.init import FULL_BOOT_SEQUENCE
        except ImportError:
            logger.error("Could not find RTL8187 boot sequence (sequences/init.py).")
            return False

        # Execute the 'Incantation' with high-res precision for EVERY command.
        # This provides a steady heartbeat the hardware needs to stay in sync.
        total_cmds = len(FULL_BOOT_SEQUENCE)
        update_cb(0.1, f"Blasting {total_cmds} setup commands (Precise Heartbeat)...")
        
        for i, (cmd_type, wVal, wIdx, data, delay) in enumerate(FULL_BOOT_SEQUENCE):
            try:
                if cmd_type == "WRITE":
                    self.transport.write_custom(wVal, wIdx, data)
                elif cmd_type == "WRITE_AND_WAIT":
                    self.transport.write_custom(wVal, wIdx, data)
                    # Block until an ACK is received on the interrupt/status endpoint.
                    # Based on PCAP flow, we expect a 1-byte ACK from the device.
                    self.transport.read_custom(wVal, wIdx, 1)
                elif cmd_type == "READ":
                    self.transport.read_custom(wVal, wIdx, data)
                elif cmd_type == "SET_CONFIG":
                    self.dev.set_configuration()
                
                # In a background thread, we use synchronous sleeps.
                # This bypasses event loop scheduling overhead.
                self._high_res_sleep_sync(delay)

                if i > 0 and i % 500 == 0:
                    prog = 0.1 + (0.8 * (i / total_cmds))
                    update_cb(prog, f"Booting: {i}/{total_cmds} steps completed.")
                    
            except usb.core.USBError as e:
                if cmd_type != "SET_CONFIG":
                    logger.error(f"FATAL: USB Error during boot sequence at step {i} (Reg {hex(wVal)}): {e}")
                    return False

        # Reset pipes to clear stalls and flush FIFO buffers (Crucial step from mvp.py)
        update_cb(0.95, "Resetting USB pipes and flushing buffers...")
        self.transport.reset_pipes()
        time.sleep(0.1) # Breather after incantation

        # PHASE 2: Lock into Monitor Mode
        # 1. Set MSR to NO_LINK (0x00) to stop autonomous background scanning
        update_cb(0.98, "Locking into Monitor Mode (Disabling Auto-Scan)...")
        self.transport.write_reg8(MSR, MSR_NO_LINK)
        
        # 2. Set RCR to accept all packets (Promiscuous)
        # We reuse the 4-byte RCR value from the PCAP but ensure bit 0 (AAP) is set.
        # 0x909cfc0b: Includes Broadcast, Multicast, Mgmt, Data, and Promiscuous (AAP)
        self.transport.write_reg32(RCR, [0x0b, 0xfc, 0x9c, 0x90])
        
        return True

    def _high_res_sleep_sync(self, seconds: float):
        """Synchronous version of high-resolution sleep."""
        import time
        if seconds < 0.020:
            start = time.perf_counter()
            while (time.perf_counter() - start) < seconds:
                pass
        else:
            time.sleep(seconds)

    async def _high_res_sleep(self, seconds: float):
        """
        High-resolution sleep that bypasses Windows' 15.6ms timer resolution.
        - Delays < 20ms: Pure busy-wait loop using time.perf_counter().
        - Delays >= 20ms: asyncio.sleep (OS-friendly) + 2ms busy-wait tail.
        """
        import time
        if seconds < 0.020:
            # For short durations, busy-wait to bypass Windows scheduler jitter.
            start = time.perf_counter()
            while (time.perf_counter() - start) < seconds:
                pass
        else:
            # For longer durations, use OS sleep then busy-wait the final 2ms.
            await asyncio.sleep(seconds - 0.002)
            start = time.perf_counter()
            while (time.perf_counter() - start) < 0.002:
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
                
                # RTL8187 Specific: The raw frame might have a hardware trailer.
                # WlanFrameParser naturally ignores trailing garbage due to bounds checks.
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
        Replays the full 156-instruction sequence for the requested channel,
        which includes necessary baseband and AGC recalibration steps.
        """
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
                elif cmd_type == "WRITE_AND_WAIT":
                    self.transport.write_custom(wVal, wIdx, data)
                    self.transport.read_custom(wVal, wIdx, 1)
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
        RTL8187 requires a 12-byte TX descriptor prepended to the frame.
        """
        frame_len = len(frame_bytes)
        
        # 12-byte TX Descriptor:
        # Byte 0-1: Frame length (12 lower bits) combined with a TX flag (0x8000)
        # Byte 2-11: Zeros (no advanced rate control or fragmentation configured for MVP)
        tx_desc = struct.pack("<H", frame_len | 0x8000) + bytes(10)
        payload = tx_desc + frame_bytes
        
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.dev.write, USB_EP_BULK_OUT, payload)
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
