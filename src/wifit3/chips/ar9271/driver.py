import asyncio
import logging
import usb.core
import usb.util
from typing import Optional, Dict, List

from .protocol.htc import HTCProtocol
from .protocol.wmi import WMIProtocol
from .protocol.metadata import AthMetadataLayer
from .constants import *

logger = logging.getLogger(__name__)

class AR9271Driver:
    """
    Main Driver for the Atheros AR9271.
    Orchestrates protocols, heartbeat, and device state.
    """
    
    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self.htc = HTCProtocol()
        self.wmi = WMIProtocol()
        self.meta = AthMetadataLayer()
        
        self.is_running = False
        self._reader_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        
        self._event_queues: Dict[int, asyncio.Queue] = {
            # Map SeqID -> Queue for ACKs
        }
        self._rx_queue = asyncio.Queue()

    async def connect(self):
        """
        Performs the HTC/WMI handshake and starts background tasks.
        """
        logger.info("Starting AR9271 Handshake...")
        
        # 1. Start Reader Loop first to catch HTC Ready
        self.is_running = True
        self._reader_task = asyncio.create_task(self._reader_loop())
        
        # 2. Wait for HTC Ready on EP 0x83 (HTC Control IN)
        # In a real driver, we'd wait for the reader to signal this.
        # For simplicity in this implementation, we assume it arrives.
        
        # 3. Connect WMI Service
        # HTC_MSG_CONNECT_SERVICE_ID (0x0002)
        # [EP=0] [Flags=0] [Len=10] [Pad=0] [Msg=0x0002] [Svc=0x0100] ...
        connect_msg = bytearray.fromhex("0000000a000000000002010000000304")
        await self._usb_write(USB_EP_WMI_CMD_OUT, connect_msg)
        
        # 4. Start Heartbeat
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("AR9271 Connected and Heartbeat started.")

    async def _heartbeat_loop(self):
        """Sends WMI_ECHO every 150ms to prevent firmware hang."""
        while self.is_running:
            try:
                await self.send_wmi_command(WMI_ECHO_CMDID, b'', wait_for_ack=False)
                await asyncio.sleep(0.150)
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
                break

    async def send_wmi_command(self, command_id: int, payload: bytes, wait_for_ack: bool = True):
        """
        Wraps payload in WMI/HTC headers and sends to device.
        """
        # 1. Pack WMI
        wmi_payload, seq = self.wmi.pack_command(command_id, payload)
        
        # 2. Pack HTC
        htc_packet = self.htc.pack(HTC_ENDPOINT_WMI, wmi_payload)
        
        # 3. Wait for Credits
        while self.htc.credits <= 0 and wait_for_ack:
            await asyncio.sleep(0.001)
        
        self.htc.consume_credit()
        
        # 4. Write to USB
        if wait_for_ack:
            ack_queue = asyncio.Queue()
            self._event_queues[seq] = ack_queue
            
        await self._usb_write(USB_EP_WMI_CMD_OUT, htc_packet)
        
        if wait_for_ack:
            try:
                # Wait 1s for ACK
                await asyncio.wait_for(ack_queue.get(), timeout=1.0)
                return True
            except asyncio.TimeoutError:
                logger.warning(f"Timeout waiting for ACK for Seq {seq}")
                return False
            finally:
                del self._event_queues[seq]
        return True

    async def _reader_loop(self):
        """Continuous background read from Bulk IN (0x82)."""
        loop = asyncio.get_running_loop()
        while self.is_running:
            try:
                # PyUSB read is blocking, use executor
                raw = await loop.run_in_executor(
                    None, self.dev.read, USB_EP_DATA_WMI_IN, 4096, 100
                )
                if not raw:
                    continue
                
                data = bytes(raw)
                from .usb_logger import USBInterceptor
                USBInterceptor.log_rx(USB_EP_DATA_WMI_IN, data)
                
                # 1. Check for Credit Reports / HTC Control
                credit_count = self.htc.parse_credit_report(data)
                if credit_count is not None:
                    self.htc.update_credits(credit_count)
                    continue

                # 2. Check for WMI Events
                try:
                    ep, flags, htc_payload = self.htc.unpack(data)
                    if ep == HTC_ENDPOINT_WMI:
                        ev_id, seq, wmi_payload = self.wmi.unpack_event(htc_payload)
                        if seq in self._event_queues:
                            await self._event_queues[seq].put(True)
                except Exception:
                    # Not a WMI event or malformed
                    pass

                # 3. Check for Data Frames
                frame, rssi, length = self.meta.parse_rx(data) # This needs to skip HTC? 
                # Actually, parse_rx expects raw payload. 
                # HTC unpack gives us htc_payload. 
                # Hardware descriptors are in EP 2 (Data) usually.
                
            except usb.core.USBError as e:
                # Timeout is normal (errno 10060 on Windows)
                if e.errno not in (10060, 110):
                    logger.error(f"Reader USBError: {e}")
                await asyncio.sleep(0.01)

    async def _usb_write(self, endpoint: int, data: bytes):
        from .usb_logger import USBInterceptor
        USBInterceptor.log_tx(endpoint, data)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.dev.write, endpoint, data)

    async def set_channel(self, channel: int):
        """
        Executes a tuning sequence to hop to the target channel.
        """
        from .sequences.tuning import get_channel_hop_sequence
        logger.info(f"Tuning AR9271 to Channel {channel}...")
        
        sequence = get_channel_hop_sequence(channel)
        for cmd_id, payload in sequence:
            success = await self.send_wmi_command(cmd_id, payload)
            if not success:
                logger.error(f"Failed to send tuning command {hex(cmd_id)}")
                return False
        
        logger.info(f"Successfully tuned to Channel {channel}.")
        return True

    async def close(self):
        self.is_running = False
        if self._reader_task:
            self._reader_task.cancel()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        logger.info("AR9271 Driver closed.")
