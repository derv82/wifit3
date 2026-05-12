import usb.core
import time
import logging
from typing import List, Union

from .constants import USB_EP_BULK_IN

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

    def reset_pipes(self):
        """Resets toggle bits and clears stalls on critical endpoints."""
        try:
            self.dev.clear_halt(USB_EP_BULK_IN)
            logger.info("RTL8187 Bulk IN pipe reset (Toggle bits cleared).")
        except Exception as e:
            # Errno 2 (Entity not found) usually means the pipe wasn't stalled to begin with.
            logger.debug(f"Did not reset RTL8187 pipes (expected if not stalled): {e}")

    def write_reg8(self, reg: int, val: int):
        """1-byte register write. Blocks until Status Stage (ACK)."""
        from ..ar9271.usb_logger import USBInterceptor
        USBInterceptor.log_tx(0, bytes([val])) # Dummy EP for log
        self.dev.ctrl_transfer(self.BM_REQ_VENDOR_OUT, self.REQ_CMD, reg, 0, [val])

    def write_reg16(self, reg: int, val: int):
        """2-byte register write (Little Endian). Blocks until Status Stage (ACK)."""
        from ..ar9271.usb_logger import USBInterceptor
        data = [val & 0xFF, (val >> 8) & 0xFF]
        USBInterceptor.log_tx(0, bytes(data))
        self.dev.ctrl_transfer(self.BM_REQ_VENDOR_OUT, self.REQ_CMD, reg, 0, data)

    def write_reg32(self, reg: int, val: Union[int, List[int]]):
        """4-byte register write. Blocks until Status Stage (ACK)."""
        from ..ar9271.usb_logger import USBInterceptor
        if isinstance(val, int):
            data = [
                val & 0xFF,
                (val >> 8) & 0xFF,
                (val >> 16) & 0xFF,
                (val >> 24) & 0xFF
            ]
        else:
            data = val
            
        USBInterceptor.log_tx(0, bytes(data))
        self.dev.ctrl_transfer(self.BM_REQ_VENDOR_OUT, self.REQ_CMD, reg, 0, data)

    def read_reg8(self, reg: int) -> int:
        """1-byte register read."""
        from ..ar9271.usb_logger import USBInterceptor
        res = self.dev.ctrl_transfer(self.BM_REQ_VENDOR_IN, self.REQ_CMD, reg, 0, 1)
        USBInterceptor.log_rx(0, bytes(res))
        return res[0]

    def read_reg16(self, reg: int) -> int:
        """2-byte register read (Little Endian)."""
        from ..ar9271.usb_logger import USBInterceptor
        res = self.dev.ctrl_transfer(self.BM_REQ_VENDOR_IN, self.REQ_CMD, reg, 0, 2)
        USBInterceptor.log_rx(0, bytes(res))
        return res[0] | (res[1] << 8)

    def read_reg32(self, reg: int) -> int:
        """4-byte register read (Little Endian)."""
        from ..ar9271.usb_logger import USBInterceptor
        res = self.dev.ctrl_transfer(self.BM_REQ_VENDOR_IN, self.REQ_CMD, reg, 0, 4)
        USBInterceptor.log_rx(0, bytes(res))
        return res[0] | (res[1] << 8) | (res[2] << 16) | (res[3] << 24)

    def write_custom(self, wVal: int, wIdx: int, data: Union[int, List[int]]):
        """Raw vendor write for non-standard registers or indices."""
        from ..ar9271.usb_logger import USBInterceptor
        if isinstance(data, list):
            USBInterceptor.log_tx(0, bytes(data))
        self.dev.ctrl_transfer(self.BM_REQ_VENDOR_OUT, self.REQ_CMD, wVal, wIdx, data)

    def read_custom(self, wVal: int, wIdx: int, length: int) -> bytes:
        """Raw vendor read for non-standard registers or indices."""
        from ..ar9271.usb_logger import USBInterceptor
        res = self.dev.ctrl_transfer(self.BM_REQ_VENDOR_IN, self.REQ_CMD, wVal, wIdx, length)
        USBInterceptor.log_rx(0, bytes(res))
        return bytes(res)
