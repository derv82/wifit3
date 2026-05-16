import logging
import struct
import asyncio
from pathlib import Path

from .transport import MT7921AUTransport
from .constants import FIRMWARE_WM, FIRMWARE_ROM_PATCH

logger = logging.getLogger(__name__)

class MT7921AUFirmwareLoader:
    """
    Handles parsing and loading the multi-stage firmware for MT7921AU.
    """
    def __init__(self, transport: MT7921AUTransport, assets_dir: Path):
        self.transport = transport
        self.assets_dir = assets_dir

    def _build_mcu_header(self, payload_len: int) -> bytes:
        """
        Builds the 12-byte SDIO/MCU header for Mediatek chips.
        """
        total_len = payload_len + 12
        word0 = total_len
        word1 = total_len | 0x41000000
        word2 = 0x80010000
        return struct.pack("<III", word0, word1, word2)

    async def _upload_chunked(self, data: bytes, chunk_size: int = 1392):
        """
        Uploads data to the MCU by chopping it into chunks, adding the MCU header,
        and optionally padding the frame.
        """
        for i in range(0, len(data), chunk_size):
            chunk = data[i:i+chunk_size]
            hdr = self._build_mcu_header(len(chunk))
            
            packet = hdr + chunk
            
            # Mediatek requires padding: round_up(len, 4) + 4 - len
            # If length is already mod 4, it adds 4 bytes of padding.
            pad_len = ((len(packet) + 3) & ~3) + 4 - len(packet)
            packet += b'\x00' * pad_len
            
            await self.transport.send_bulk(packet)
            # Give the USB bus a tiny moment to breathe
            await asyncio.sleep(0.001)

    async def load_firmware(self) -> bool:
        """
        Orchestrates the firmware upload sequence.
        """
        logger.info("Starting MT7921AU firmware upload sequence...")
        
        # Load ROM Patch
        patch_path = self.assets_dir / FIRMWARE_WM
        if not patch_path.exists():
            logger.error(f"Firmware missing: {patch_path}")
            return False
            
        with open(patch_path, 'rb') as f:
            patch_data = f.read()
            
        # The Linux driver strips the first 192 bytes of the ROM Patch header
        logger.info(f"Uploading ROM Patch ({len(patch_data)} bytes)...")
        await self._upload_chunked(patch_data[192:])
        
        # Load RAM Code
        ram_path = self.assets_dir / FIRMWARE_ROM_PATCH
        if not ram_path.exists():
            logger.error(f"Firmware missing: {ram_path}")
            return False
            
        with open(ram_path, 'rb') as f:
            ram_data = f.read()
            
        # The Linux driver strips the first 128 bytes of the RAM Code header
        logger.info(f"Uploading RAM Code ({len(ram_data)} bytes)...")
        await self._upload_chunked(ram_data[128:])
        
        logger.info("Firmware payload fully transferred.")
        return True
