import logging
from pathlib import Path
from typing import Callable, Optional

import usb.core

from . import init as chip_init
from . import mcu, rx
from .transport import MT7925AUTransport
from .firmware import MT7925AUFirmwareLoader
# ruff: noqa: F403, F405
from .constants import *
from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.dot11.parser import WlanFrameParser
from wifit3.errors import BringUpError

logger = logging.getLogger(__name__)


class MT7925AUDriver(Driver):
    """Userspace driver for the MediaTek MT7925U (Wi-Fi 7, connac3, USB).

    Bring-up state (see chips/mt7925au/MT7925AU.md): firmware boot (firmware.py) is
    ported and pcap-verified. Post-boot device init, monitor entry, channel tune,
    RX decode and TX are in progress.
    """

    SUPPORTED_IDS = [
        DeviceID(0x0e8d, 0x7925, "MT7925AU", product_name="MediaTek MT7925U"),
        DeviceID(0x0846, 0x9072, "MT7925AU", vendor="Netgear", product_name="A9000"),
    ]
    # Dual-band Wi-Fi 7 radio, 20 MHz primary. 2.4 GHz (1-13) + the 5 GHz 20 MHz
    # channels the capture sweeps (main.log: 36..165).
    SUPPORTED_CHANNELS = list(range(1, 14)) + [
        36, 40, 44, 48, 52, 56, 60, 64,
        100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
        149, 153, 157, 161, 165,
    ]
    FAKE_MAC = FakeMacSupport.UNIMPLEMENTED
    LINUX_REPLUG_AFTER_MODPROBE = True

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "MT7925AUDriver":
        drv = cls(dev)
        drv.product_name = id_entry.product_name
        return drv

    def __init__(self, dev):
        super().__init__()
        self.dev = dev
        self.transport = MT7925AUTransport(dev)
        self.firmware = MT7925AUFirmwareLoader(self.transport, Path(__file__).parent / "assets")
        self.parser = WlanFrameParser()
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._channel = self.SUPPORTED_CHANNELS[0]
        self.is_warm: bool = False
        self.mac_address: Optional[str] = None
        self._antenna_mask: int = 0x3

    def register_rx_callback(self, callback: Callable[[dict], None]):
        self._rx_callback = callback

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        self.transport._on_fatal = cb

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """Cold-boot the firmware, run post-boot device init, and enter monitor mode."""
        if progress_cb:
            progress_cb(0.1, "Uploading MT7925AU firmware...")
        self.transport.subscribe(self._on_raw_rx)
        if not await self.firmware.load_firmware():
            raise BringUpError("firmware", "MT7925AU firmware load failed")
        self.transport.start_rx()

        if progress_cb:
            progress_cb(0.6, "Configuring device...")
        state = await chip_init.post_boot_init(self.transport)
        self.mac_address = state.caps.mac
        self._antenna_mask = state.caps.antenna_mask

        if progress_cb:
            progress_cb(0.9, "Enabling monitor mode...")
        await chip_init.enter_monitor(self.transport, self._channel)
        if progress_cb:
            progress_cb(1.0, "Done")
        logger.info("MT7925AU monitor mode ready on channel %d (MAC %s).",
                    self._channel, self.mac_address)
        return True

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to a 20 MHz channel via the monitor sniffer config (UNI SNIFFER, tag 1)."""
        cmd, payload = mcu.config_sniffer(channel)
        await self.transport.send_mcu_command(cmd, payload, wait_resp=False)
        self._channel = channel
        return True

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        logger.error("MT7925AU TX not yet ported (M5)")
        return False

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        return frame_bytes

    async def _enable_rx_acks(self) -> None:
        return None

    async def _disable_rx_acks(self) -> None:
        return None

    async def close(self):
        await self.transport.stop_rx()

    def _on_raw_rx(self, data: bytes):
        """Decode one 802.11 frame off EP 0x84 (MCU responses are demuxed by the
        transport). Full connac3 RX decode lands with M4."""
        decoded = rx.decode_frame(data, self._antenna_mask)
        if decoded is None:
            return
        mpdu_off, mpdu_end, rssi, fcs_err = decoded
        if fcs_err:
            return
        frame_bytes = data[mpdu_off:mpdu_end]
        if len(frame_bytes) < 10:
            return
        try:
            parsed = self.parser.parse_80211_frame(frame_bytes, rssi)
            if parsed and self._rx_callback:
                self._rx_callback(parsed)
        except Exception as e:
            logger.debug(f"MT7925AU frame parse fail: {e}")
