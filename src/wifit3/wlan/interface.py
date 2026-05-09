import asyncio
import logging
import time
from typing import List, Optional, Callable, Any

logger = logging.getLogger(__name__)

class WlanInterface:
    """
    High-level 802.11 abstraction for a hardware driver.
    The UI interacts exclusively with this class.
    """
    def __init__(self, driver_instance: Any, name: str, description: str):
        self.driver = driver_instance
        self.name = name
        self.description = description
        self.current_channel = 1
        
        self._rx_callbacks: List[Callable[[bytes, int, float], None]] = []
        self._hopping_task: Optional[asyncio.Task] = None
        self._is_hopping = False

    async def connect(self):
        """Initializes the underlying hardware handshake."""
        await self.driver.connect()

    async def set_channel(self, channel: int) -> bool:
        """Translates a channel number into the driver's register sequences."""
        success = await self.driver.set_channel(channel)
        if success:
            self.current_channel = channel
        return success

    def register_rx_callback(self, callback_func: Callable[[bytes, int, float], None]):
        """
        UI registers a function here. 
        Expected signature: func(frame_bytes, rssi, timestamp)
        """
        if callback_func not in self._rx_callbacks:
            self._rx_callbacks.append(callback_func)

    def _fire_rx_callbacks(self, frame_bytes: bytes, rssi: int):
        ts = time.time()
        for cb in self._rx_callbacks:
            try:
                cb(frame_bytes, rssi, ts)
            except Exception as e:
                logger.error(f"RX Callback failed: {e}")

    async def send_raw(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        """
        Injects a raw 802.11 frame.
        The underlying driver is responsible for wrapping it in the correct
        hardware descriptors (e.g., ath_tx_status) before sending.
        """
        if hasattr(self.driver, 'inject_frame'):
             return await self.driver.inject_frame(frame_bytes, use_no_ack)
        logger.warning(f"Driver for {self.name} does not support injection.")
        return False
    
    async def deauth(self, ap_bssid: str, client_bssid: str, wait_for_ack: bool):
        # TODO: Craft deauth packet, send via send_raw()
        pass

    async def start_hopping(self, channels: List[int] = None, interval: float = 0.5):
        """Spawns an asyncio task to loop through channels."""
        if self._is_hopping:
            return
            
        if not channels:
            # Default 2.4GHz hopper.
            # TODO Rely on self.driver.supported_channels instead (once ready)
            channels = [1, 6, 11, 2, 7, 12, 3, 8, 13, 4, 9, 5, 10]
            
        self._is_hopping = True
        self._hopping_task = asyncio.create_task(self._hop_loop(channels, interval))
        logger.info(f"Started channel hopping on {self.name}")

    async def _hop_loop(self, channels: List[int], interval: float):
        import itertools
        channel_cycle = itertools.cycle(channels)
        
        while self._is_hopping:
            channel = next(channel_cycle)
            await self.set_channel(channel)
            await asyncio.sleep(interval)

    async def stop_hopping(self):
        """Cancels the hopping task."""
        self._is_hopping = False
        if self._hopping_task:
            self._hopping_task.cancel()
            try:
                await self._hopping_task
            except asyncio.CancelledError:
                pass
            self._hopping_task = None
        logger.info(f"Stopped channel hopping on {self.name}")

    async def close(self):
        """Halts the driver loops and releases the USB interface."""
        await self.stop_hopping()
        await self.driver.close()