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
        Scans PyUSB. Discovers supported devices, handles firmware upload
        if they are cold, and wraps the warm handle in a WlanInterface.
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
                
                # AR9271 Specific lifecycle (Delayed Initialization)
                if info["name"] == "AR9271":
                    # We pass is_warm=False by default; the driver will probe in connect()
                    driver_instance = info["driver_class"](dev, is_warm=False)
                    iface = WlanInterface(driver_instance, f"wlan{len(self.interfaces)}", info["desc"])
                    self.interfaces.append(iface)

                # RTL8187 Specific lifecycle (usually warm)
                elif info["name"] == "RTL8187":
                    # Passive Detection: Try to read from Bulk IN (EP 0x81)
                    is_warm = False
                    try:
                        data = dev.read(0x81, 64, timeout=100)
                        if len(data) > 0:
                            is_warm = True
                    except usb.core.USBError:
                        pass
                    
                    driver_instance = info["driver_class"](dev, is_warm=is_warm)
                    iface = WlanInterface(driver_instance, f"wlan{len(self.interfaces)}", info["desc"])
                    self.interfaces.append(iface)

                # RT5572 Specific lifecycle (firmware upload)
                elif info["name"] == "RT5572":
                    warm_dev, was_already_warm = await self._ensure_rt5572_firmware(dev)
                    if warm_dev:
                        transport = RT5572USBTransport(warm_dev)
                        driver_instance = info["driver_class"](transport, is_warm=was_already_warm)
                        iface = WlanInterface(driver_instance, f"wlan{len(self.interfaces)}", info["desc"])
                        self.interfaces.append(iface)

        logger.info(f"Discovered {len(self.interfaces)} native WlanInterfaces.")
        return self.interfaces

    async def _ensure_rt5572_firmware(self, dev: usb.core.Device) -> Tuple[Optional[usb.core.Device], bool]:
        """
        Safely checks if RT5572 is cold or warm.
        Returns a tuple of (warm usb.core.Device handle, was_already_warm).
        """
        # Passive Detection: Read PBF_SYS_CTRL (0x0400).
        # Probing shows COLD hardware returns 0x2080 (Bit 13 set).
        # Initialized hardware returns 0x0f80 (Bit 13 cleared).
        try:
            # RT5572 uses bRequest 7 for multi-read
            res = dev.ctrl_transfer(0xc0, 0x07, 0, 0x0400, 4)
            val = res[0] | (res[1] << 8) | (res[2] << 16) | (res[3] << 24)

            # If Bit 13 is cleared, the MCU has finished its internal init.
            if not (val & (1 << 13)):
                logger.info(f"RT5572 is already WARM (PBF State: {hex(val)}).")
                return dev, True
        except usb.core.USBError:
            pass

        # If we got here, it's likely COLD or just plugged in.
        logger.info("RT5572 appears COLD (Hardware Uninitialized).")
        return dev, False

    def get_interface(self, name: str) -> Optional[WlanInterface]:
        for iface in self.interfaces:
            if iface.name == name:
                return iface
        return None

    async def close_all(self):
        for iface in self.interfaces:
            await iface.close()
        self.interfaces = []
