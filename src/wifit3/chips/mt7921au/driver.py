import logging
import importlib
import struct
from pathlib import Path
from typing import Optional, Callable

import usb.core

from .transport import MT7921AUTransport
from .firmware import MT7921AUFirmwareLoader
# Star-imports the chip's register/PHY constants; the names resolve at runtime
# but ruff can't see them statically, so suppress the import-* lints file-wide.
# ruff: noqa: F403, F405
from .constants import *
from wifit3.engine.protocols import DeviceID
from wifit3.wlan.packet import WlanFrameParser

logger = logging.getLogger(__name__)


class MT7921AUDriver:
    """
    Unified Userspace driver for the Mediatek MT7921AU (WiFi 6).
    """

    SUPPORTED_IDS = [
        DeviceID(0x0e8d, 0x7961, "Mediatek MT7921AU / ALFA AWUS036AXML"),
    ]
    # Conservative default: 2.4 GHz only. The MT7921AU is a Wi-Fi 6
    # dual-band radio so it *can* do 5 GHz, but this driver's bring-up
    # is paused at the WinUSB EP0 step (see MT7921AU.md) — expand once
    # full 5 GHz tuning is verified.
    SUPPORTED_CHANNELS = list(range(1, 14))

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "MT7921AUDriver":
        return cls(dev)

    def __init__(self, dev):
        self.dev = dev
        self.transport = MT7921AUTransport(dev)
        self.firmware = MT7921AUFirmwareLoader(self.transport, Path(__file__).parent / "assets")
        self.parser = WlanFrameParser()
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._mcu_seq = 1
        
        # Load pre-extracted sequences
        self.assets_init = importlib.import_module("wifit3.chips.mt7921au.assets.mt7921au_init")
        self.assets_tuning = importlib.import_module("wifit3.chips.mt7921au.assets.mt7921au_tuning")

    def register_rx_callback(self, callback: Callable[[dict], None]):
        self._rx_callback = callback

    async def connect(self, progress_cb: Optional[Callable[[str, float], None]] = None) -> bool:
        """
        Cold/Warm boots the hardware into Monitor Mode.
        """
        if progress_cb:
            progress_cb("Uploading Firmware...", 0.1)
        logger.info("Initializing MT7921AU...")
        
        # Upload firmware
        success = await self.firmware.load_firmware()
        if not success:
            logger.error("Failed to load MT7921AU firmware.")
            return False

        # NOTE: the captured post-boot INIT_SEQ (assets/mt7921au_init.py) is a replay
        # of unified-bus (0x5f) register writes — but once the firmware is running it
        # stops servicing control transfers, so every one of those writes times out
        # (~20 s of failures that left the chip worse off). Post-boot configuration on
        # this chip is done with MCU commands (the kernel sends ~130 after boot), not
        # register writes. Porting that sequence faithfully from the scatter capture is
        # the RX-path work; until then, do NOT replay INIT_SEQ.

        # Enable Sniffer Mode
        if progress_cb:
            progress_cb("Enabling Monitor Mode...", 0.9)
        logger.info("Enabling MT7921AU Sniffer Mode...")
        await self._enable_sniffer(True)
        
        self.transport.subscribe(self._on_raw_rx)
        await self.transport.start()
        if progress_cb:
            progress_cb("Done", 1.0)
        logger.info("MT7921AU initialization complete.")
        return True

    def _get_next_seq(self) -> int:
        seq = self._mcu_seq
        self._mcu_seq = (self._mcu_seq + 1) & 0xFF
        if self._mcu_seq == 0:
            self._mcu_seq = 1
        return seq

    def _build_mcu_uni_header(self, cid: int, payload_len: int, option: int = 0x05) -> bytes:
        """
        Builds the 64-byte Unified MCU Command Header.
        """
        header = bytearray(64)
        # Bytes 0-31: Hardware TXD (Zeros for control)
        
        # Bytes 32-33: Payload length
        struct.pack_into("<H", header, 32, payload_len)
        # Bytes 34-35: Command ID
        struct.pack_into("<H", header, 34, cid)
        # Byte 37: Packet Type (0xA0 = Command)
        header[37] = 0xA0
        # Byte 39: Sequence
        header[39] = self._get_next_seq()
        # Byte 42: S2D (0x00 = Host to MCU)
        header[42] = 0x00
        # Byte 43: Option Flags
        header[43] = option
        
        return bytes(header)

    async def _enable_sniffer(self, enable: bool):
        """
        Sends the MCU_UNI_CMD_SNIFFER command.
        """
        payload = b"\x01\x00\x00\x00" if enable else b"\x00\x00\x00\x00"
        header = self._build_mcu_uni_header(MCU_UNI_CMD_SNIFFER, len(payload))
        await self.transport.send_bulk(header + payload, EP_OUT_MCU)

    async def stop(self):
        await self.transport.stop()

    async def set_channel(self, channel: int, scan: bool = False):
        """Tunes the hardware to the specified channel."""
        logger.debug(f"MT7921AU: Tuning to channel {channel}")
        
        # We can build it dynamically now
        payload = bytearray(12)
        payload[0] = channel # control_ch at offset 64
        payload[1] = channel # center_ch at offset 65
        # Remaining 10 bytes Zeros
        
        header = self._build_mcu_uni_header(MCU_UNI_CMD_CH_SWITCH, len(payload))
        await self.transport.send_bulk(header + bytes(payload), EP_OUT_MCU)

    async def inject(self, frame: bytes):
        """Injects a raw 802.11 frame."""
        # 80-byte header: 64-byte MCU TXD + 16-byte padding
        header = bytearray(80)
        
        # DW0: OWNER_NIC | Pkt Type Command (0xA0)
        # 0xA0 is 10100000. 
        dw0 = TXD_DW0_OWNER_NIC | (0xA0 << 16) 
        struct.pack_into("<I", header, 0, dw0)
        
        # DW1: WLAN_IDX=0x3FF, Q_IDX=9 (MCU Mgmt)
        dw1 = (9 << TXD_DW1_Q_IDX_SHIFT) | (0x3FF & TXD_DW1_WLAN_IDX_MASK)
        struct.pack_into("<I", header, 4, dw1)
        
        # DW3: FIX_RATE
        struct.pack_into("<I", header, 12, TXD_DW3_FIX_RATE)

        # MCU part (Starts at Byte 32)
        # Length at Offset 32
        struct.pack_into("<H", header, 32, 80 + len(frame))
        # CID at Offset 36
        header[36] = MCU_CMD_TX_MGMT
        # Pkt Type at Offset 37
        header[37] = 0xA0
        # S2D at Offset 42
        header[42] = 0x00
        # SET_QUERY at Offset 43
        header[43] = 0x01
        # SEQ at Offset 39
        header[39] = self._get_next_seq()
        
        await self.transport.send_bulk(header + frame, EP_OUT_DATA)

    def _on_raw_rx(self, data: bytes):
        """Callback from transport layer."""
        if len(data) < RXD_SIZE:
            return

        # 1. Parse RXD
        rxd0 = struct.unpack("<I", data[0:4])[0]
        rxd1 = struct.unpack("<I", data[4:8])[0]

        # FCS Check (Bit 16 of DW1)
        if rxd1 & RXD_DW1_FCS_ERR:
            logger.debug("MT7921AU: Dropping corrupt frame (FCS Error)")
            return

        # Frame Length (Bits [13:0] of DW0)
        frame_len = rxd0 & RXD_DW0_LEN_MASK
        
        # 2. Extract RSSI (RCPI Formula)
        # Byte 14: RCPI0, Byte 15: RCPI1
        rcpi0 = data[14]
        rcpi1 = data[15]
        
        valid_rcpis = [r for r in [rcpi0, rcpi1] if r > 0]
        if not valid_rcpis:
            rssi = -128
        else:
            avg_rcpi = sum(valid_rcpis) / len(valid_rcpis)
            rssi = (avg_rcpi * 0.5) - 110
        
        # 3. Extract 802.11 Frame
        # Frame starts after RXD
        frame_bytes = data[RXD_SIZE : RXD_SIZE + frame_len]
        
        if len(frame_bytes) < 10:
            return
            
        try:
            # The parser handles management vs data frames
            parsed = self.parser.parse_80211_frame(frame_bytes, int(rssi))
            if parsed and self._rx_callback:
                self._rx_callback(parsed)
        except Exception as e:
            logger.debug(f"MT7921AU Frame parse fail: {e}")
