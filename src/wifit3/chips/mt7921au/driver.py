import logging
import asyncio
import binascii
import importlib
from pathlib import Path
from typing import Optional, Callable

from .transport import MT7921AUTransport
from .firmware import MT7921AUFirmwareLoader
from wifit3.wlan.packet import WlanFrameParser

logger = logging.getLogger(__name__)

class MT7921AUDriver:
    """
    Unified Userspace driver for the Mediatek MT7921AU (WiFi 6).
    """
    def __init__(self, dev):
        self.dev = dev
        self.transport = MT7921AUTransport(dev)
        self.firmware = MT7921AUFirmwareLoader(self.transport, Path(__file__).parent / "assets")
        self.parser = WlanFrameParser()
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self.assets_init = importlib.import_module("wifit3.chips.mt7921au.assets.mt7921au_init")
        self.assets_tuning = importlib.import_module("wifit3.chips.mt7921au.assets.mt7921au_tuning")

    def register_rx_callback(self, callback: Callable[[dict], None]):
        self._rx_callback = callback

    async def init(self) -> bool:
        """
        Cold/Warm boots the hardware into Monitor Mode.
        """
        logger.info("Initializing MT7921AU...")
        # Upload firmware
        success = await self.firmware.load_firmware()
        if not success:
            logger.error("Failed to load MT7921AU firmware.")
            return False

        logger.info("Executing MT7921AU hardware initialization sequence...")
        for req in self.assets_init.INIT_SEQ:
            bmReq, bReq, wVal, wIdx, data_hex = req
            
            # The tshark extraction gives us the setup packet and data payload.
            # Convert hex string to bytes
            data = binascii.unhexlify(data_hex) if data_hex else b""
            self.transport.send_vendor_request(bmReq, bReq, wVal, wIdx, data)
            
            # Short sleep to let the hardware digest
            await asyncio.sleep(0.001)
        
        self.transport.subscribe(self._on_raw_rx)
        await self.transport.start()
        logger.info("MT7921AU initialization complete.")
        return True

    async def stop(self):
        await self.transport.stop()

    async def set_channel(self, channel: int):
        """Tunes the hardware to the specified channel."""
        logger.debug(f"MT7921AU: Tuning to channel {channel}")
        if channel in self.assets_tuning.TUNING_MAP:
            payload_hex = self.assets_tuning.TUNING_MAP[channel]
            payload = binascii.unhexlify(payload_hex)
            await self.transport.send_bulk(payload)
        else:
            logger.warning(f"MT7921AU: Unknown channel {channel}, cannot tune.")

    async def inject(self, frame: bytes):
        """Injects a raw 802.11 frame."""
        await self.transport.send_bulk(frame)

    def _on_raw_rx(self, data: bytes):
        """Callback from transport layer."""
        # TODO: Parse RX header to extract raw 802.11 frame and RSSI
        # parsed = self.parser.parse_management_frame(raw_80211, rssi)
        # if parsed and self._rx_callback:
        #     self._rx_callback(parsed)
        pass
