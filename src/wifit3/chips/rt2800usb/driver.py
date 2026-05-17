import logging
import asyncio
import struct
import json
import time
import importlib
from typing import Optional
from pathlib import Path

from .transport import RT2800USBTransport
from .firmware import RT2800USBFirmwareLoader
from .constants import *

from wifit3.engine.protocols import DeviceID
from wifit3.wlan.packet import WlanFrameParser

logger = logging.getLogger(__name__)


import usb.core


class RT2800USBDriver:
    """
    Unified Userspace driver for the Ralink rt2800usb family (RT5572, RT3572, RT5372).
    """
    RXINFO_SIZE = 4
    TXINFO_SIZE = 4

    SUPPORTED_IDS = [
        DeviceID(0x148f, 0x5572, "Ralink RT5572 / Panda PAU09 N600", extras={"chip_id": "rt5572"}),
        DeviceID(0x148f, 0x3572, "Ralink RT3572 / ALFA AWUS051NH v2", extras={"chip_id": "rt3572"}),
        DeviceID(0x148f, 0x5372, "Ralink RT5372 / Panda PAU05",       extras={"chip_id": "rt5372"}),
    ]

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RT2800USBDriver":
        chip_id = id_entry.extras.get("chip_id", "rt5572")
        return cls(RT2800USBTransport(dev), chip_id=chip_id)

    def __init__(self, transport: RT2800USBTransport, chip_id: str = "rt5572"):
        self.transport = transport
        self.mac_address: Optional[str] = None
        self._rx_callback = None
        self._is_running = False
        self.is_warm = False
        self.parser = WlanFrameParser()
        self.chip_id = chip_id.lower()

        # Dynamic descriptor sizes based on chip architecture
        if self.chip_id == "rt5572":
            self.rxwi_size = 24
            self.txwi_size = 20
        else:
            self.rxwi_size = 16
            self.txwi_size = 16

        # Dynamically load assets based on chip_id
        try:
            self.assets_init = importlib.import_module(f"wifit3.chips.rt2800usb.assets.{self.chip_id}_init")
            self.assets_tuning = importlib.import_module(f"wifit3.chips.rt2800usb.assets.{self.chip_id}_tuning")
        except ImportError as e:
            logger.error(f"Failed to load assets for chip {self.chip_id}: {e}")
            raise ValueError(f"Unsupported or missing assets for chip {self.chip_id}")

        # Subscribe to transport polling
        self.transport.subscribe(self._on_bulk_in)

    def register_rx_callback(self, cb):
        self._rx_callback = cb

    def _on_bulk_in(self, data: bytes):
        """
        Processes raw data from the Bulk IN endpoint.
        Format: [RXINFO (4)] [RXWI (16/24)] [802.11 Frame] [Pad] [RXD (4)]
        """
        if len(data) < self.RXINFO_SIZE + self.rxwi_size:
            logger.debug(f"RX Drop: Too short ({len(data)} bytes)")
            return

        # 1. Parse RXINFO (First 4 bytes)
        # Word 0: [15:0] USB_DMA_RX_PKT_LEN
        rx_pkt_len = struct.unpack("<H", data[0:2])[0]
        
        if rx_pkt_len <= self.rxwi_size or rx_pkt_len > len(data) - self.RXINFO_SIZE:
            logger.debug(f"RX Drop: Bad rx_pkt_len {rx_pkt_len} (Data len: {len(data)})")
            return

        # 2. Extract RXWI (Next 24 bytes for RT5592 arch)
        # Word 2: [7:0] RSSI0 (at offset 8 in RXWI)
        rssi_raw = data[self.RXINFO_SIZE + 8]
        rssi = rssi_raw - 120 # Default RSSI offset from rt2800.h

        # 3. Extract 802.11 Frame
        # Frame starts after RXINFO and RXWI
        frame_bytes = data[self.RXINFO_SIZE + self.rxwi_size : self.RXINFO_SIZE + rx_pkt_len]
        
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

    async def connect(self, firmware_path: Optional[str] = None, progress_cb=None):
        """
        Orchestrates the device cold-boot and initialization.
        """
        def _update(pct, msg):
            if progress_cb:
                progress_cb(pct, msg)
            logger.info(f"Progress {int(pct*100)}%: {msg}")

        # --- HEAVY LIFTING OFF-RAMP ---
        # We offload the procedural grind to a background thread.
        # This keeps the main UI event loop responsive.
        success = await asyncio.to_thread(self._bootstrap_sync, firmware_path, _update)
        
        if success:
            # We need to run these async parts on the main loop
            await self.set_channel(1)
            await self.transport.start()
            self._is_running = True
            _update(1.0, f"{self.chip_id.upper()} Driver successfully connected. MAC: {self.mac_address}")
            return True
        
        return False

    def _bootstrap_sync(self, firmware_path: Optional[str], update_cb) -> bool:
        """
        Synchronous version of the boot and init sequence for background thread.
        """
        update_cb(0.05, f"Identifying {self.chip_id.upper()} hardware...")
        
        # 1. Identify Hardware
        mac_csr0 = self.transport.read_reg32(MAC_CSR0)
        asic_ver = self.transport.read_reg32(ASIC_VER_ID)
        logger.info(f"MAC_CSR0: {hex(mac_csr0)}, ASIC_VER: {hex(asic_ver)}")

        # Check warmth state passively exactly once before boot
        pbf_ctrl = self.transport.read_reg32(PBF_SYS_CTRL)
        self.is_warm = not bool(pbf_ctrl & (1 << 13))

        if self.is_warm:
            update_cb(0.1, "Device is already WARM. Skipping firmware upload.")
        else:
            # 2. Stabilization (COLD BOOT ONLY)
            update_cb(0.1, "Stabilizing hardware for cold boot...")
            self.transport.write_reg32(AUTOWAKEUP_CFG, 0)
            self.transport.write_reg32(WPDMA_GLO_CFG, 0)
            
            # 3. Load Firmware
            update_cb(0.15, "Loading firmware into MCU memory...")
            if firmware_path is None or not Path(firmware_path).exists():
                # Fallback to internal assets if no path provided
                firmware_path = Path(__file__).parent / "assets" / f"{self.chip_id}.bin"
                
            try:
                with open(firmware_path, "rb") as f:
                    fw_bytes = f.read()
            except FileNotFoundError:
                # Last resort: try to find it in the scripts folder if it was just extracted
                firmware_path = Path(f"scripts/{self.chip_id}/{self.chip_id}.bin")
                try:
                    with open(firmware_path, "rb") as f:
                        fw_bytes = f.read()
                except FileNotFoundError:
                    # Final fallback: use rt5572.bin as they share firmware
                    firmware_path = Path(__file__).parent / "assets" / "rt5572.bin"
                    with open(firmware_path, "rb") as f:
                        fw_bytes = f.read()
            
            if not RT2800USBFirmwareLoader.load(self.transport, fw_bytes):
                logger.error("Firmware upload failed")
                return False

            update_cb(0.4, "Finalizing firmware upload...")
            # Finalize firmware upload by clearing mailbox
            self.transport.write_reg32(H2M_MAILBOX_CID, 0xffffffff)
            self.transport.write_reg32(H2M_MAILBOX_STATUS, 0xffffffff)

            # 4. Wait for PBF (Post-Boot)
            update_cb(0.45, "Waiting for PBF System stabilization...")
            for i in range(100):
                pbf_ctrl = self.transport.read_reg32(PBF_SYS_CTRL)
                if pbf_ctrl & PBF_SYS_CTRL_READY:
                    logger.info("PBF System Ready.")
                    break
                if i % 10 == 0:
                    update_cb(0.45 + (i * 0.001), f"Waiting for PBF... ({i}/100)")
                time.sleep(0.01)
            else:
                logger.warning("PBF System NOT ready, firmware might not be executing.")

            # 5. Kick MCU (Boot Signal)
            update_cb(0.55, "Sending MCU Boot Signal...")
            # Based on trace: Req 1, Val 0x8, Idx 0
            self.transport.set_device_mode(0, 0x08)
            logger.info("MCU Boot Signal sent.")

            # Wait for MCU to process the signal
            time.sleep(0.1)

        # 6 Full Hardware Initialization Sequence (Run for both COLD and WARM)
        init_seq = getattr(self.assets_init, "INIT_SEQ", [])
        total_init = len(init_seq)
        update_cb(0.6, f"Replaying {total_init} initialization registers...")
        for i, (idx, val) in enumerate(init_seq):
            if i == 15:
                # Matches Req 1, Val 1, Idx 0 (USB_MODE_RESET) from the kernel trace
                self.transport.set_device_mode(0, 0x01)
                logger.info("USB Endpoints Reset (Mid-Init).")
            
            self.transport.write_multi(idx, bytes.fromhex(val))
            
            if i > 0 and i % 50 == 0:
                prog = 0.6 + (0.3 * (i / total_init))
                update_cb(prog, f"Initializing registers... ({i}/{total_init})")

        # 7. Read MAC Address (From EEPROM)
        update_cb(0.95, "Reading MAC address from EEPROM...")
        eeprom_mac0 = self.transport.read_eeprom(0x0002)
        eeprom_mac1 = self.transport.read_eeprom(0x0003)
        eeprom_mac2 = self.transport.read_eeprom(0x0004)
        
        self.mac_address = ":".join(f"{b:02x}" for b in [
            eeprom_mac0 & 0xff, (eeprom_mac0 >> 8) & 0xff,
            eeprom_mac1 & 0xff, (eeprom_mac1 >> 8) & 0xff,
            eeprom_mac2 & 0xff, (eeprom_mac2 >> 8) & 0xff
        ])
        logger.info(f"Device MAC: {self.mac_address}")
        
        return True

    async def set_channel(self, channel: int) -> bool:
        """
        Replays the captured register sequence for the given channel.
        """
        sequence = self.assets_tuning.get_sequence(channel)
        if not sequence:
            logger.warning(f"No tuning sequence for Channel {channel} on {self.chip_id}")
            return False

        logger.debug(f"Tuning {self.chip_id.upper()} to Channel {channel}...")
        
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
        pkt_len = self.txwi_size + len(frame_bytes)
        txinfo = pkt_len | TXINFO_W0_WIV | TXINFO_W0_QSEL_EDCA
        
        # 2. Construct TXWI (24 bytes / 6 words)
        # Word 0: PHYMODE, MCS, BW, STBC, SHORT_GI etc.
        txwi_w0 = 0
        if not use_no_ack:
            txwi_w0 |= TXWI_W0_ACK
        
        # Word 1: PacketID, CliID, MPDU Total Byte Count
        txwi_w1 = (len(frame_bytes) << 16)
        
        # Build TXWI buffer
        txwi = bytearray(self.txwi_size)
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
        logger.info(f"{self.chip_id.upper()} Driver closed.")
