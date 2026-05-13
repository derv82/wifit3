import logging
import usb.core
import time
from typing import Optional

from .transport import RT5572USBTransport
from .constants import MCU_CODE_BASE

logger = logging.getLogger(__name__)

class RT5572FirmwareLoader:
    """
    Handles uploading firmware to the Ralink rt5572 MCU.
    """
    
    @staticmethod
    def load(transport: RT5572USBTransport, fw_bytes: bytes) -> bool:
        """
        Uploads the firmware in 64-byte chunks.
        """
        logger.info(f"Uploading {len(fw_bytes)} bytes of firmware to rt5572...")
        
        # 1. MCU Preparation (Reset, mailbox, etc.)
        # Based on PCAP, there are some register writes before the upload.
        # For now, let's focus on the upload itself.
        
        # 2. Upload loop
        chunk_size = 64
        for i in range(0, len(fw_bytes), chunk_size):
            chunk = fw_bytes[i:i + chunk_size]
            addr = MCU_CODE_BASE + i
            
            try:
                transport.write_multi(addr, chunk)
            except usb.core.USBError as e:
                logger.error(f"Failed to upload firmware chunk at {hex(addr)}: {e}")
                return False
                
        logger.info("Firmware upload complete.")
        
        # 3. Kick logic removed from here as it varies by chip revision
        # and is handled in the main Driver boot sequence.
        return True
