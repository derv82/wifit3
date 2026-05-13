import logging
import asyncio
import struct
import json
from typing import Optional
from pathlib import Path

from .transport import RT5572USBTransport
from .firmware import RT5572FirmwareLoader
from .constants import *

from wifit3.wlan.packet import WlanFrameParser

from .assets import rt5572_tuning, rt5572_init

logger = logging.getLogger(__name__)

class RT5572Driver:
    """
    Userspace driver for the Ralink RT5572 (rt2800usb).
    """
    RXINFO_SIZE = 4
    RXWI_SIZE = 24
    TXINFO_SIZE = 4
    TXWI_SIZE = 20

    def __init__(self, transport: RT5572USBTransport, is_warm: bool = False):
        self.transport = transport
        self.mac_address: Optional[str] = None
        self._rx_callback = None
        self._is_running = False
        self.is_warm = is_warm
        self.parser = WlanFrameParser()

        # Subscribe to transport polling
        self.transport.subscribe(self._on_bulk_in)

    def register_rx_callback(self, cb):
        self._rx_callback = cb

    def _on_bulk_in(self, data: bytes):
        """
        Processes raw data from the Bulk IN endpoint.
        Format: [RXINFO (4)] [RXWI (16)] [802.11 Frame] [Pad] [RXD (4)]
        """
        if len(data) < self.RXINFO_SIZE + self.RXWI_SIZE:
            logger.debug(f"RX Drop: Too short ({len(data)} bytes)")
            return

        # 1. Parse RXINFO (First 4 bytes)
        # Word 0: [15:0] USB_DMA_RX_PKT_LEN
        rx_pkt_len = struct.unpack("<H", data[0:2])[0]
        
        if rx_pkt_len <= self.RXWI_SIZE or rx_pkt_len > len(data) - self.RXINFO_SIZE:
            logger.debug(f"RX Drop: Bad rx_pkt_len {rx_pkt_len} (Data len: {len(data)})")
            return

        # 2. Extract RXWI (Next 24 bytes for RT5592 arch)
        # Word 2: [7:0] RSSI0 (at offset 8 in RXWI)
        rssi_raw = data[self.RXINFO_SIZE + 8]
        rssi = rssi_raw - 120 # Default RSSI offset from rt2800.h

        # 3. Extract 802.11 Frame
        # Frame starts after RXINFO and RXWI
        frame_bytes = data[self.RXINFO_SIZE + self.RXWI_SIZE : self.RXINFO_SIZE + rx_pkt_len]
        
        if len(frame_bytes) < 10:
            logger.debug(f"RX Drop: Extracted frame too short ({len(frame_bytes)})")
            return
            
        # 4. Parse and Callback
        try:
            parsed = self.parser.parse_80211_frame(frame_bytes, rssi)
            if parsed:
                if self._rx_callback:
                    self._rx_callback(parsed)
            else:
                logger.debug(f"RX Drop: Parser returned None for header {frame_bytes[:2].hex()}")
        except Exception as e:
            logger.debug(f"Frame parse fail: {e}")

    async def connect(self, firmware_path: Optional[str] = None):
        """
        Orchestrates the device cold-boot and initialization.
        """
        logger.info("Initializing RT5572...")
        
        # 1. Identify Hardware
        mac_csr0 = self.transport.read_reg32(MAC_CSR0)
        asic_ver = self.transport.read_reg32(ASIC_VER_ID)
        logger.info(f"MAC_CSR0: {hex(mac_csr0)}, ASIC_VER: {hex(asic_ver)}")

        if self.is_warm:
            logger.info("Device is already WARM. skipping firmware upload.")
        else:
            # 2. Stabilization (COLD BOOT ONLY)
            self.transport.write_reg32(AUTOWAKEUP_CFG, 0)
            self.transport.write_reg32(WPDMA_GLO_CFG, 0)
            
            # 3. Load Firmware
            if firmware_path is None or not Path(firmware_path).exists():
                # Fallback to internal assets if no path provided
                firmware_path = Path(__file__).parent / "assets" / "rt5572.bin"
                
            try:
                with open(firmware_path, "rb") as f:
                    fw_bytes = f.read()
            except FileNotFoundError:
                # Last resort: try to find it in the scripts folder if it was just extracted
                firmware_path = Path("scripts/rt5572/rt5572.bin")
                with open(firmware_path, "rb") as f:
                    fw_bytes = f.read()
            
            if not RT5572FirmwareLoader.load(self.transport, fw_bytes):
                raise RuntimeError("Firmware upload failed")

            # Finalize firmware upload by clearing mailbox
            # (Matches frame 745/747 in trace)
            self.transport.write_reg32(H2M_MAILBOX_CID, 0xffffffff)
            self.transport.write_reg32(H2M_MAILBOX_STATUS, 0xffffffff)

            # 4. Wait for PBF (Post-Boot)
            for _ in range(100):
                pbf_ctrl = self.transport.read_reg32(PBF_SYS_CTRL)
                if pbf_ctrl & PBF_SYS_CTRL_READY:
                    logger.info("PBF System Ready.")
                    break
                await asyncio.sleep(0.01)
            else:
                logger.warning("PBF System NOT ready, firmware might not be executing.")

            # 5. Kick MCU (Boot Signal)
            # Based on trace: Req 1, Val 0x8, Idx 0
            self.transport.set_device_mode(0, 0x08)
            logger.info("MCU Boot Signal sent.")

            # Wait for MCU to process the signal
            await asyncio.sleep(0.1)

        # 6 Full Hardware Initialization Sequence (Run for both COLD and WARM)
        # BBP and RF must be re-initialized every time we connect.
        logger.info(f"Replaying {len(rt5572_init.INIT_SEQ)} initialization registers...")
        for i, (idx, val) in enumerate(rt5572_init.INIT_SEQ):
            if i == 15:
                # Matches Req 1, Val 1, Idx 0 (USB_MODE_RESET) from the kernel trace
                # Must be sent exactly while MAC_SYS_CTRL is in reset (index 14)
                self.transport.set_device_mode(0, 0x01)
                logger.info("USB Endpoints Reset (Mid-Init).")
            self.transport.write_multi(idx, bytes.fromhex(val))

        # 7. Read MAC Address (From EEPROM)
        # EEPROM offsets for MAC are typically 0x0002, 0x0003, 0x0004
        eeprom_mac0 = self.transport.read_eeprom(0x0002)
        eeprom_mac1 = self.transport.read_eeprom(0x0003)
        eeprom_mac2 = self.transport.read_eeprom(0x0004)
        
        self.mac_address = ":".join(f"{b:02x}" for b in [
            eeprom_mac0 & 0xff, (eeprom_mac0 >> 8) & 0xff,
            eeprom_mac1 & 0xff, (eeprom_mac1 >> 8) & 0xff,
            eeprom_mac2 & 0xff, (eeprom_mac2 >> 8) & 0xff
        ])
        logger.info(f"Device MAC: {self.mac_address}")
        
        # 8. Set initial channel (1)
        await self.set_channel(1)
        
        # 9. Start transport polling (ONLY AFTER INIT)
        await self.transport.start()
        self._is_running = True
        
        logger.info("RT5572 Boot Sequence Complete.")

    async def set_channel(self, channel: int) -> bool:
        """
        Replays the captured register sequence for the given channel.
        """
        sequence = rt5572_tuning.get_sequence(channel)
        if not sequence:
            logger.warning(f"No tuning sequence for Channel {channel}")
            return False

        logger.debug(f"Tuning RT5572 to Channel {channel}...")
        
        for idx, val in sequence:
            # The extracted val is a hex string literal representing the exact byte order.
            # To send it exactly as it appeared on the wire, we pack as big-endian.
            self.transport.write_multi(idx, bytes.fromhex(val))
            
        return True

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        """
        Injects a raw 802.11 frame by wrapping it in Ralink TX descriptors.
        """
        # 1. Construct TXINFO (4 bytes)
        # Word 0: [15:0] Len, [24] WIV=1, [26:25] QSEL=2 (EDCA)
        pkt_len = self.TXWI_SIZE + len(frame_bytes)
        txinfo = pkt_len | TXINFO_W0_WIV | TXINFO_W0_QSEL_EDCA
        
        # 2. Construct TXWI (24 bytes / 6 words)
        # Word 0: PHYMODE, MCS, BW, STBC, SHORT_GI etc.
        txwi_w0 = 0
        if not use_no_ack:
            txwi_w0 |= TXWI_W0_ACK
        
        # Word 1: PacketID, CliID, MPDU Total Byte Count
        txwi_w1 = (len(frame_bytes) << 16)
        
        # Build TXWI buffer
        txwi = bytearray(self.TXWI_SIZE)
        struct.pack_into("<I", txwi, 0, txwi_w0)
        struct.pack_into("<I", txwi, 4, txwi_w1)
        
        # 3. Assemble full packet
        full_pkt = struct.pack("<I", txinfo) + txwi + frame_bytes
        
        # 4. Send to Bulk OUT
        await self.transport.send_bulk(full_pkt)
        return True

    async def close(self):
        self._is_running = False
        await self.transport.stop()
        logger.info("RT5572 Driver closed.")
