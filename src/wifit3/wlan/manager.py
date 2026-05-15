import asyncio
import logging
import usb.core
import usb.util
from typing import List, Optional, Tuple

from wifit3.chips.rt5572.transport import RT5572USBTransport
from .interface import WlanInterface

logger = logging.getLogger(__name__)

class WlanDeviceManager:
    """
    Scans the USB bus for supported chipsets, handles firmware cold-boots,
    and returns high-level WlanInterface abstractions.
    """
    def __init__(self):
        self.interfaces: List[WlanInterface] = []
        
        from wifit3.chips.ar9271.driver import AR9271Driver
        from wifit3.chips.rtl8187.driver import RTL8187Driver
        from wifit3.chips.rt5572.driver import RT5572Driver
        
        # Supported Hardware Registry
        self.SUPPORTED_DEVICES = {
            (0x0cf3, 0x9271): {"name": "AR9271", "driver_class": AR9271Driver, "desc": "Atheros AR9271 / ALFA AWUS036NHA"},
            (0x0bda, 0x8187): {"name": "RTL8187", "driver_class": RTL8187Driver, "desc": "Realtek RTL8187L / ALFA AWUS036H"},
            (0x148f, 0x5572): {"name": "RT5572", "driver_class": RT5572Driver, "desc": "Ralink RT5572 / Panda PAU09 N600"},
        }

    async def refresh(self) -> List[WlanInterface]:
        """
        Scans PyUSB. Discovers supported devices and wraps them in a WlanInterface.
        Initialization (firmware upload, etc.) is now handled by the driver's connect() method.
        """
        import libusb_package
        backend = libusb_package.get_libusb1_backend()
        
        # Ensure clean state before refreshing
        for iface in self.interfaces:
            await iface.close()
        self.interfaces = []
        
        # Find all devices on the bus
        for dev in usb.core.find(find_all=True, backend=backend):
            vid_pid = (dev.idVendor, dev.idProduct)
            if vid_pid in self.SUPPORTED_DEVICES:
                info = self.SUPPORTED_DEVICES[vid_pid]
                logger.info(f"Found supported hardware: {info['desc']}")
                
                # Create driver instance
                if info["name"] == "RT5572":
                    # RT5572 needs its transport early for warmth probing in manager if we want to keep it passive,
                    # but let's move that logic into the driver's __init__ or a class method for consistency.
                    transport = RT5572USBTransport(dev)
                    is_warm = await self._is_rt5572_warm(dev)
                    driver_instance = info["driver_class"](transport, is_warm=is_warm)
                else:
                    # AR9271 and RTL8187 can handle their own warmth probing in connect() or be passed a guess
                    driver_instance = info["driver_class"](dev)
                
                iface = WlanInterface(driver_instance, f"wlan{len(self.interfaces)}", info["desc"])
                self.interfaces.append(iface)

        logger.info(f"Discovered {len(self.interfaces)} native WlanInterfaces.")
        return self.interfaces

    async def _is_rt5572_warm(self, dev: usb.core.Device) -> bool:
        """Passive warmth check for RT5572."""
        try:
            res = dev.ctrl_transfer(0xc0, 0x07, 0, 0x0400, 4)
            val = res[0] | (res[1] << 8) | (res[2] << 16) | (res[3] << 24)
            return not (val & (1 << 13))
        except Exception:
            return False

    def get_interface(self, name: str) -> Optional[WlanInterface]:
        for iface in self.interfaces:
            if iface.name == name:
                return iface
        return None

    async def close_all(self):
        for iface in self.interfaces:
            await iface.close()
        self.interfaces = []
