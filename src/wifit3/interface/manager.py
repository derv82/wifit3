import asyncio
import logging
import time
import usb.core
import usb.util
from typing import List, Optional, Callable, Any

from wifit3.chips.ar9271.driver import AR9271Driver

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

    async def start_hopping(self, channels: List[int] = None, interval: float = 0.5):
        """Spawns an asyncio task to loop through channels."""
        if self._is_hopping:
            return
            
        if not channels:
            # Default 2.4GHz hopper. We can query the driver's capabilities later.
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

class WlanDeviceManager:
    """
    Scans the USB bus for supported chipsets, handles firmware cold-boots,
    and returns high-level WlanInterface abstractions.
    """
    def __init__(self):
        self.interfaces: List[WlanInterface] = []
        
        # Supported Hardware Registry
        self.SUPPORTED_DEVICES = {
            (0x0cf3, 0x9271): {"name": "AR9271", "driver_class": AR9271Driver, "desc": "Atheros AR9271 / ALFA AWUS036NHA"},
        }

    async def refresh(self) -> List[WlanInterface]:
        """
        Scans PyUSB. Discovers supported devices, handles firmware upload
        if they are cold, and wraps the warm handle in a WlanInterface.
        """
        # Ensure clean state before refreshing
        for iface in self.interfaces:
            await iface.close()
        self.interfaces = []
        
        # Find all devices on the bus
        for dev in usb.core.find(find_all=True):
            vid_pid = (dev.idVendor, dev.idProduct)
            if vid_pid in self.SUPPORTED_DEVICES:
                info = self.SUPPORTED_DEVICES[vid_pid]
                logger.info(f"Found supported hardware: {info['desc']}")
                
                # Currently hardcoded for AR9271 lifecycle, we can abstract this
                # when adding Realtek chips later.
                if info["name"] == "AR9271":
                    warm_dev = await self._ensure_ar9271_firmware(dev)
                    if warm_dev:
                        driver_instance = info["driver_class"](warm_dev)
                        iface = WlanInterface(driver_instance, f"wlan{len(self.interfaces)}", info["desc"])
                        self.interfaces.append(iface)

        logger.info(f"Discovered {len(self.interfaces)} native WlanInterfaces.")
        return self.interfaces

    async def _ensure_ar9271_firmware(self, dev: usb.core.Device) -> Optional[usb.core.Device]:
        """
        Checks if AR9271 is cold. If so, uploads firmware and waits for re-enumeration.
        Returns a warm usb.core.Device handle.
        """
        try:
            cfg = dev.get_active_configuration()
            intf = cfg[(0,0)]
        except usb.core.USBError as e:
            logger.warning(f"Could not get configuration, device might be uninitialized: {e}")
            # If we can't get config, assume it's cold.
            intf = [] 
            
        # A cold AR9271 (no firmware) has EP4 as an Interrupt OUT endpoint.
        # Once the firmware is loaded, EP4 changes to a Bulk OUT endpoint.
        ep4 = usb.util.find_descriptor(intf, custom_match=lambda e: e.bEndpointAddress == 0x04)
        
        is_cold = False
        if ep4 is not None:
            if usb.util.endpoint_type(ep4.bmAttributes) == usb.util.ENDPOINT_TYPE_INTR:
                is_cold = True
        else:
            # If EP4 is missing entirely, it's definitely cold or stuck.
            is_cold = True

        if is_cold:
            logger.info("AR9271 is COLD (No Firmware). Initiating upload sequence...")
            from wifit3.chips.ar9271.firmware import FirmwareLoader
            import os
            
            # Find the fw file relative to the project root
            fw_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "htc_9271_cleanroom.fw")
            success = FirmwareLoader.load(dev, fw_path)
            
            if success:
                logger.info("Waiting 3 seconds for AR9271 to re-enumerate on the bus...")
                await asyncio.sleep(3)
                
                # Re-acquire the handle
                warm_dev = usb.core.find(idVendor=0x0cf3, idProduct=0x9271)
                if warm_dev:
                    logger.info("AR9271 successfully warmed up!")
                    return warm_dev
                else:
                    logger.error("AR9271 failed to re-enumerate after firmware upload.")
                    return None
            else:
                logger.error("Firmware upload failed.")
                return None
        
        # Already warm
        return dev

    def get_interface(self, name: str) -> Optional[WlanInterface]:
        for iface in self.interfaces:
            if iface.name == name:
                return iface
        return None

    async def close_all(self):
        for iface in self.interfaces:
            await iface.close()
        self.interfaces = []
