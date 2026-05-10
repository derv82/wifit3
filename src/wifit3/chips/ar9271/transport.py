import asyncio
import logging
import struct
import usb.core
from typing import Dict, List, Callable, Tuple, Optional

from .protocol.htc import HTCProtocol
from .constants import *

logger = logging.getLogger(__name__)

class CreditManager:
    """
    Manages AR9271 buffer credits to prevent device-side overflow.
    """
    def __init__(self):
        self._credits: Dict[int, int] = {0: 512} # EP 0 (Control) has virtual infinite credits
        self._condition = asyncio.Condition()

    async def update(self, ep_id: int, count: int):
        """Replenishes credits for a specific endpoint."""
        async with self._condition:
            self._credits[ep_id] = self._credits.get(ep_id, 0) + count
            logger.debug(f"Credits replenished for EP {ep_id}: +{count} (Total: {self._credits[ep_id]})")
            self._condition.notify_all()

    async def acquire(self, ep_id: int):
        """Blocks until a credit is available for the given endpoint."""
        async with self._condition:
            while self._credits.get(ep_id, 0) <= 0:
                logger.debug(f"Waiting for credits on EP {ep_id}...")
                await self._condition.wait()
            self._credits[ep_id] -= 1

    def set_initial(self, ep_id: int, count: int):
        """Force-sets the credit count (used during READY handshake)."""
        self._credits[ep_id] = count
        logger.info(f"Initial credits set for EP {ep_id}: {count}")

class AR9271USBTransport:
    """
    Handles raw USB communication, HTC encapsulation, and credit management.
    Separates the 'plumbing' from the protocol logic.
    """
    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self.htc = HTCProtocol()
        self.credit_manager = CreditManager()
        
        self.is_running = False
        self._listeners: List[asyncio.Task] = []
        self._subscribers: Dict[int, List[Callable[[bytes], None]]] = {}
        
        # State
        self.wmi_ep_id = 1 # Default

    def subscribe(self, htc_ep_id: int, callback: Callable[[bytes], None]):
        """Registers a callback for packets arriving on a specific HTC endpoint."""
        if htc_ep_id not in self._subscribers:
            self._subscribers[htc_ep_id] = []
        self._subscribers[htc_ep_id].append(callback)

    async def start(self):
        """Spawns the background listener tasks."""
        self.is_running = True
        
        # 1. Control Listener (Single task is fine for Interrupt EP 0x83)
        self._listeners.append(asyncio.create_task(self._read_loop(USB_EP_HTC_CTRL_IN, "Control")))
        
        # 2. Data/WMI Listeners (Multi-URB Pressure for Bulk EP 0x82)
        # Spawning 4 concurrent tasks to keep the DMA pipeline full
        for i in range(4):
            self._listeners.append(asyncio.create_task(self._read_loop(USB_EP_DATA_WMI_IN, f"Data-{i}")))
            
        logger.info(f"USB Transport started with 5 listeners (Pressure=4 on 0x82).")

    def reset_pipes(self):
        """Resets toggle bits and clears stalls on critical endpoints."""
        try:
            self.dev.clear_halt(USB_EP_DATA_WMI_IN)
            self.dev.clear_halt(USB_EP_WMI_CMD_OUT)
            logger.info("USB Pipes reset (Toggle bits cleared).")
        except Exception as e:
            logger.warning(f"Failed to reset pipes: {e}")

    async def stop(self):
        """Shuts down listeners and releases resources."""
        self.is_running = False
        for task in self._listeners:
            task.cancel()
        self._listeners = []
        logger.info("USB Transport stopped.")

    async def send(self, htc_ep_id: int, payload: bytes, is_wmi: bool = True):
        """
        Encapsulates and sends a packet, waiting for credits if necessary.
        """
        # 1. Wait for Credits
        await self.credit_manager.acquire(htc_ep_id)
        
        # 2. Pack based on service type
        if is_wmi:
            packet = self.htc.pack_wmi(htc_ep_id, payload)
        else:
            packet = self.htc.pack_control(htc_ep_id, payload)
            
        # 3. Write to USB (Bulk Out EP 0x04)
        from .usb_logger import USBInterceptor
        USBInterceptor.log_tx(USB_EP_WMI_CMD_OUT, packet)
        
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.dev.write, USB_EP_WMI_CMD_OUT, packet)

    async def _read_loop(self, ep_addr: int, name: str):
        """Continuous polling of an IN endpoint."""
        loop = asyncio.get_running_loop()
        while self.is_running:
            try:
                raw = await loop.run_in_executor(
                    None, self.dev.read, ep_addr, 4096, 100
                )
                if not raw:
                    continue
                
                data = bytes(raw)
                from .usb_logger import USBInterceptor
                USBInterceptor.log_rx(ep_addr, data)
                
                # Handle the raw packet
                await self._handle_incoming(data, ep_addr)

            except usb.core.USBError as e:
                if e.errno not in (10060, 110): # Timeout is normal
                    logger.error(f"Transport {name} USBError: {e}")
                await asyncio.sleep(0.001)
            except Exception as e:
                logger.error(f"Transport {name} Error: {e}")
                await asyncio.sleep(0.01)

    async def _handle_incoming(self, data: bytes, ep_addr: int):
        """Parses headers, handles trailers/credits, and dispatches to subscribers."""
        # 1. Handle Hardware Encapsulation (EP 0x82 only)
        if ep_addr == USB_EP_DATA_WMI_IN: # 0x82
            # Skip 12-byte Hardware RX Descriptor to find the HTC Header
            if len(data) < 12 + self.htc.HTC_HDR_STD_LEN:
                return
            htc_raw = data[12:]
        else:
            htc_raw = data

        # 2. Unpack HTC Header
        try:
            htc_ep, flags, trailer_len, payload = self.htc.unpack(htc_raw, ep_addr)
        except Exception as e:
            logger.debug(f"HTC Unpack fail on EP {hex(ep_addr)}: {e}")
            return
        
        # 3. Handle HTC Trailers (Credit Reports)
        if flags & 0x02: # HTC_FLAGS_RECV_TRAILER
            if trailer_len > 0 and len(payload) >= trailer_len:
                # Trailer is at the VERY end of the specified payload
                trailer = payload[-trailer_len:]
                # Clean payload is everything BEFORE the trailer
                actual_payload = payload[:-trailer_len]
                
                # Parse 2-byte trailer records: [EPID][Credits]
                start_off = 0
                if len(trailer) >= 2 and trailer[0] == 0x00 and trailer[1] == 0xC6:
                    start_off = 2
                    
                for i in range(start_off, len(trailer), 2):
                    if i + 1 < len(trailer):
                        rep_ep = trailer[i]
                        rep_count = trailer[i+1]
                        if rep_ep < 22: # ENDPOINT_MAX
                            await self.credit_manager.update(rep_ep, rep_count)
                
                payload = actual_payload

        # 4. Special Case: READY message on EP 0 contains initial credits
        if htc_ep == 0 and len(payload) >= 6:
            res = self.htc.parse_ready_msg(payload)
            if res:
                credits, _ = res
                self.credit_manager.set_initial(1, credits)
                self.credit_manager.set_initial(2, credits)

        # 5. Dispatch to subscribers
        if htc_ep in self._subscribers:
            for cb in self._subscribers[htc_ep]:
                cb(payload)
