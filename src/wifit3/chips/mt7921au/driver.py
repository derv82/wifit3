import logging
from pathlib import Path
from typing import Optional, Callable

import usb.core

from . import init as chip_init
from . import mcu, rx
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
    """Userspace driver for the MediaTek MT7921AU (Wi-Fi 6).

    Bring-up state (see chips/mt7921au/MT7921AU.md): firmware boots
    (firmware.py) and the post-boot device init is ported + pcap-verified
    (init.py / mac.py / mcu.py). Monitor entry, channel tune and the RX
    descriptor decode are the remaining milestones — set_channel / inject_frame
    are not wired yet.
    """

    SUPPORTED_IDS = [
        DeviceID(0x0e8d, 0x7961, "Mediatek MT7921AU / ALFA AWUS036AXML"),
    ]
    # Dual-band Wi-Fi 6 radio, 20 MHz primary. 2.4 GHz (1-13) + the 5 GHz 20 MHz
    # channels of the world regulatory domain (regdomain.CHANNELS_5GHZ).
    SUPPORTED_CHANNELS = list(range(1, 14)) + [
        36, 40, 44, 48, 52, 56, 60, 64,
        100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
        149, 153, 157, 161, 165,
    ]

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "MT7921AUDriver":
        return cls(dev)

    def __init__(self, dev):
        self.dev = dev
        self.transport = MT7921AUTransport(dev)
        self.firmware = MT7921AUFirmwareLoader(self.transport, Path(__file__).parent / "assets")
        self.parser = WlanFrameParser()
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._init_state: Optional[chip_init.InitState] = None
        self._channel = self.SUPPORTED_CHANNELS[0]

    def register_rx_callback(self, callback: Callable[[dict], None]):
        self._rx_callback = callback

    async def connect(self, progress_cb: Optional[Callable[[str, float], None]] = None) -> bool:
        """Boot the firmware (cold or warm), then run the post-boot device init."""
        if progress_cb:
            progress_cb("Uploading firmware...", 0.1)
        logger.info("Initializing MT7921AU...")

        # Subscribe before any RX flows; the reader is started by load_firmware
        # (cold) and ensured below (warm), and runs until close().
        self.transport.subscribe(self._on_raw_rx)
        if not await self.firmware.load_firmware():
            logger.error("Failed to load MT7921AU firmware.")
            return False
        self.transport.start_rx()   # idempotent — covers the warm-boot path

        if progress_cb:
            progress_cb("Configuring device...", 0.6)
        logger.info("Running MT7921AU post-boot init...")
        self._init_state = await chip_init.post_boot_init(self.transport)

        # Enter monitor mode on the initial channel (the RX reader routes the
        # monitor commands' acks back, and 802.11 frames to _on_raw_rx).
        if progress_cb:
            progress_cb("Enabling monitor mode...", 0.9)
        await chip_init.enter_monitor(self.transport, self._channel)

        if progress_cb:
            progress_cb("Done", 1.0)
        logger.info("MT7921AU monitor mode ready on channel %d.", self._channel)
        return True

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to a 20 MHz channel via the monitor sniffer config command."""
        logger.debug("MT7921AU: tuning to channel %d", channel)
        cmd, payload = mcu.config_sniffer(channel)
        await self.transport.send_mcu_command(cmd, payload, wait_resp=False)
        self._channel = channel
        return True

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        """Transmit a raw 802.11 frame. Not yet ported (TX is wired last)."""
        logger.warning("MT7921AU inject_frame: not yet ported")
        return False

    async def close(self):
        await self.transport.stop_rx()

    def _on_raw_rx(self, data: bytes):
        """Decode one 802.11 frame off EP 0x84 (MCU responses are demuxed away by
        the transport). Strips the connac2 RX descriptor, then parses the MPDU."""
        decoded = rx.decode_frame(data)
        if decoded is None:
            return
        mpdu_off, rssi, fcs_err = decoded
        if fcs_err:
            return
        frame_bytes = data[mpdu_off:]
        if len(frame_bytes) < 10:
            return
        try:
            parsed = self.parser.parse_80211_frame(frame_bytes, rssi)
            if parsed and self._rx_callback:
                self._rx_callback(parsed)
        except Exception as e:
            logger.debug(f"MT7921AU frame parse fail: {e}")
