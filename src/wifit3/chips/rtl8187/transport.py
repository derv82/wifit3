import usb.core
import time
import logging
from typing import List, Union

logger = logging.getLogger(__name__)

class RTL8187USBTransport:
    """
    Lightweight transport for RTL8187.
    Handles synchronous Vendor Control Transfers (0x40/0xC0).
    """
    
    BM_REQ_VENDOR_OUT = 0x40
    BM_REQ_VENDOR_IN  = 0xC0
    REQ_CMD = 5

    def __init__(self, dev: usb.core.Device):
        self.dev = dev

    def write_reg8(self, reg: int, val: int, delay: float = 0.005):
        """1-byte register write."""
        self.dev.ctrl_transfer(self.BM_REQ_VENDOR_OUT, self.REQ_CMD, reg, 0, [val])
        if delay > 0:
            time.sleep(delay)

    def write_reg16(self, reg: int, val: int, delay: float = 0.005):
        """2-byte register write (Little Endian)."""
        data = [val & 0xFF, (val >> 8) & 0xFF]
        self.dev.ctrl_transfer(self.BM_REQ_VENDOR_OUT, self.REQ_CMD, reg, 0, data)
        if delay > 0:
            time.sleep(delay)

    def write_reg32(self, reg: int, val: Union[int, List[int]], delay: float = 0.01):
        """4-byte register write."""
        if isinstance(val, int):
            data = [
                val & 0xFF,
                (val >> 8) & 0xFF,
                (val >> 16) & 0xFF,
                (val >> 24) & 0xFF
            ]
        else:
            data = val
            
        self.dev.ctrl_transfer(self.BM_REQ_VENDOR_OUT, self.REQ_CMD, reg, 0, data)
        if delay > 0:
            time.sleep(delay)

    def read_reg8(self, reg: int) -> int:
        """1-byte register read."""
        res = self.dev.ctrl_transfer(self.BM_REQ_VENDOR_IN, self.REQ_CMD, reg, 0, 1)
        return res[0]

    def read_reg16(self, reg: int) -> int:
        """2-byte register read (Little Endian)."""
        res = self.dev.ctrl_transfer(self.BM_REQ_VENDOR_IN, self.REQ_CMD, reg, 0, 2)
        return res[0] | (res[1] << 8)

    def read_reg32(self, reg: int) -> int:
        """4-byte register read (Little Endian)."""
        res = self.dev.ctrl_transfer(self.BM_REQ_VENDOR_IN, self.REQ_CMD, reg, 0, 4)
        return res[0] | (res[1] << 8) | (res[2] << 16) | (res[3] << 24)

    def write_custom(self, wVal: int, wIdx: int, data: Union[int, List[int]]):
        """Raw vendor write for non-standard registers or indices."""
        self.dev.ctrl_transfer(self.BM_REQ_VENDOR_OUT, self.REQ_CMD, wVal, wIdx, data)

    def read_custom(self, wVal: int, wIdx: int, length: int) -> bytes:
        """Raw vendor read for non-standard registers or indices."""
        res = self.dev.ctrl_transfer(self.BM_REQ_VENDOR_IN, self.REQ_CMD, wVal, wIdx, length)
        return bytes(res)
