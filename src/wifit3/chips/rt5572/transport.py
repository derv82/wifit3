import usb.core
import logging
from typing import List, Union

from .constants import (
    USB_EP_BULK_IN, USB_SINGLE_WRITE, USB_SINGLE_READ, 
    USB_MULTI_WRITE, USB_MULTI_READ
)

logger = logging.getLogger(__name__)

class RT5572USBTransport:
    """
    Transport layer for Ralink rt5572 (rt2800usb).
    Handles Vendor Control Transfers for register access and firmware loading.
    """
    
    BM_REQ_VENDOR_OUT = 0x40
    BM_REQ_VENDOR_IN  = 0xc0

    def __init__(self, dev: usb.core.Device):
        self.dev = dev

    def write_reg32(self, reg: int, val: int):
        """4-byte register write (Little Endian)."""
        data = [
            val & 0xFF,
            (val >> 8) & 0xFF,
            (val >> 16) & 0xFF,
            (val >> 24) & 0xFF
        ]
        self.dev.ctrl_transfer(self.BM_REQ_VENDOR_OUT, USB_SINGLE_WRITE, 0, reg, data)

    def read_reg32(self, reg: int) -> int:
        """4-byte register read (Little Endian)."""
        res = self.dev.ctrl_transfer(self.BM_REQ_VENDOR_IN, USB_SINGLE_READ, 0, reg, 4)
        return res[0] | (res[1] << 8) | (res[2] << 16) | (res[3] << 24)

    def write_multi(self, addr: int, data: Union[bytes, List[int]]):
        """Multi-byte write (e.g., for firmware upload to 0x3000)."""
        self.dev.ctrl_transfer(self.BM_REQ_VENDOR_OUT, USB_MULTI_WRITE, 0, addr, data)

    def read_multi(self, addr: int, length: int) -> bytes:
        """Multi-byte read."""
        res = self.dev.ctrl_transfer(self.BM_REQ_VENDOR_IN, USB_MULTI_READ, 0, addr, length)
        return bytes(res)

    def write_eeprom(self, addr: int, val: int):
        """Write to EEPROM."""
        # TODO: Implement based on PCAP analysis
        pass

    def read_eeprom(self, addr: int) -> int:
        """Read from EEPROM."""
        # bRequest 9 is EEPROM read. addr is wIndex.
        res = self.dev.ctrl_transfer(self.BM_REQ_VENDOR_IN, 0x09, 0, addr, 2)
        return res[0] | (res[1] << 8)

    def set_device_mode(self, mode: int):
        """Sets the device mode (bRequest 1)."""
        # From PCAP: Req 1, Val 0x11, Idx 0
        self.dev.ctrl_transfer(self.BM_REQ_VENDOR_OUT, 0x01, mode, 0, None)
