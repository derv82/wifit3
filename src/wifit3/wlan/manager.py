import asyncio
import logging
import usb.core
import usb.util
from typing import List, Optional

from wifit3.chips.ar9271.driver import AR9271Driver
from .interface import WlanInterface

logger = logging.getLogger(__name__)

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