import asyncio
import logging
import usb.core
import usb.util
from typing import List, Optional, Tuple

from wifit3.chips.rt2800usb.transport import RT2800USBTransport
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
        from wifit3.chips.rt2800usb.driver import RT2800USBDriver
        from wifit3.chips.mt7921au.driver import MT7921AUDriver
        
        # Supported Hardware Registry
        self.SUPPORTED_DEVICES = {
            (0x0cf3, 0x9271): {"name": "AR9271", "driver_class": AR9271Driver, "desc": "Atheros AR9271 / ALFA AWUS036NHA"},
            (0x0bda, 0x8187): {"name": "RTL8187", "driver_class": RTL8187Driver, "desc": "Realtek RTL8187L / ALFA AWUS036H"},
            (0x148f, 0x5572): {"name": "RT5572", "driver_class": RT2800USBDriver, "desc": "Ralink RT5572 / Panda PAU09 N600", "chip_id": "rt5572"},
            (0x148f, 0x3572): {"name": "RT3572", "driver_class": RT2800USBDriver, "desc": "Ralink RT3572 / ALFA AWUS051NH v2", "chip_id": "rt3572"},
            (0x148f, 0x5372): {"name": "RT5372", "driver_class": RT2800USBDriver, "desc": "Ralink RT5372 / Panda PAU05", "chip_id": "rt5372"},
            (0x0e8d, 0x7961): {"name": "MT7921AU", "driver_class": MT7921AUDriver, "desc": "Mediatek MT7921AU / ALFA AWUS036AXML"},
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
                if info["driver_class"].__name__ == "RT2800USBDriver":
                    transport = RT2800USBTransport(dev)
                    driver_instance = info["driver_class"](transport, chip_id=info["chip_id"])
                else:
                    # AR9271 and RTL8187 can handle their own warmth probing in connect() or be passed a guess
                    driver_instance = info["driver_class"](dev)
                
                iface = WlanInterface(driver_instance, f"wlan{len(self.interfaces)}", info["desc"])
                self.interfaces.append(iface)

        logger.info(f"Discovered {len(self.interfaces)} native WlanInterfaces.")
        return self.interfaces

    def get_interface(self, name: str) -> Optional[WlanInterface]:
        for iface in self.interfaces:
            if iface.name == name:
                return iface
        return None

    async def close_all(self):
        for iface in self.interfaces:
            await iface.close()
        self.interfaces = []
