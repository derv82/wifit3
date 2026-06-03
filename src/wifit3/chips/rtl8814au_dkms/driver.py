"""RTL8814AU driver — vendor (morrownr DKMS) cleanroom port.

Status: bring-up complete through M3b-3a. ``connect()`` runs the full deterministic
init (EFUSE -> firmware -> MAC/BB/RF -> channel tune -> TX power -> InitHalDm seed ->
hal_init turn-on tail -> monitor opmode entry), all pcap-verified, then starts the
bulk-IN RX reader so monitor frames flow to the rx callback. Still pending: RSSI
decode (M3b-3b), the runtime DIG/AGC watchdog (M3c), and TX (M4 — ``inject_frame``
raises until then).

This driver is intentionally NOT registered in ``wlan/manager.py`` yet — master
keeps the working mainline-derived ``rtw88_8814au`` port until this vendor port is
hardware-proven to beat it on 2.4 GHz breadth. Exercise it via ``scripts/rtl8814au_dkms/``.
"""
from __future__ import annotations

import asyncio
import logging
from importlib import resources
from typing import Callable, ClassVar, List, Optional

import usb.core

from wifit3.engine.protocols import DeviceID, ProgressCallback
from wifit3.wlan.packet import WlanFrameParser

from ..rx_reader import RxReaderThread
from .bb import phy_bb_config
from .chan import init_tune, set_channel_bw, set_rfe_reg_init
from .constants import PID_RTL8814AU, VID_REALTEK
from .dm import init_hal_dm
from .efuse import read_chip_params
from .firmware import bring_up
from .mac import hal_init_turn_on, mac_init_misc, phy_mac_config
from .monitor import enter_monitor
from .rf import phy_rf_config
from .rx import iter_frames
from .transport import Rtl8814auTransport

logger = logging.getLogger(__name__)

_FW_ASSET = "rtl8814au_fw.bin"
_DEFAULT_CHANNEL = 1  # connect-time tune target (matches the cold-boot capture)
# RX frames carry no signal level until the PHY-status decode lands (M3b-3b); the
# parser needs an int, so report a sentinel that is clearly "unknown" not "strong".
_RSSI_PLACEHOLDER = 0


def _load_firmware() -> bytes:
    return (resources.files(__package__) / "assets" / _FW_ASSET).read_bytes()


class Rtl8814auDkmsDriver:
    SUPPORTED_IDS: ClassVar[List[DeviceID]] = [
        DeviceID(VID_REALTEK, PID_RTL8814AU,
                 "Realtek RTL8814AU 4T4R (ALFA AWUS1900) — vendor/DKMS port"),
    ]
    # 2.4 GHz, 20 MHz primary. 5G channel tune is a later milestone (M2d ports the
    # 2.4G band switch + set_chnl_bw only), so 5G channels are not advertised yet.
    SUPPORTED_CHANNELS: ClassVar[List[int]] = list(range(1, 14))

    def __init__(self, transport: Rtl8814auTransport):
        self.transport = transport
        self.mac_address: Optional[str] = None  # M2: efuse read
        self._channel: Optional[int] = None
        self._tx_power: tuple = ()  # per-path efuse TX-power info (M2e)
        self.is_warm: bool = False
        self._rx_cb: Optional[Callable[[dict], None]] = None
        self._reader: Optional[RxReaderThread] = None

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "Rtl8814auDkmsDriver":
        return cls(Rtl8814auTransport(dev))

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_cb = cb

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        loop = asyncio.get_running_loop()
        # All bring-up does blocking synchronous USB I/O; keep it off the event loop.

        # EFUSE read (vendor probe order: before _InitPowerOn). Yields rfe_type
        # (BB phy_cond discriminator), crystal_cap, and the MAC address.
        if progress_cb:
            progress_cb(0.0, "Reading EFUSE / chip parameters")
        params = await loop.run_in_executor(None, read_chip_params, self.transport)
        self.mac_address = params.mac_address
        self._tx_power = params.tx_power
        logger.info("RTL8814AU efuse: rfe_type=%d crystal_cap=0x%02x mac=%s",
                    params.rfe_type, params.crystal_cap,
                    params.mac_address or "<none>")

        if progress_cb:
            progress_cb(0.2, "Uploading firmware (3081 IDDMA)")
        fw = _load_firmware()
        ready = await loop.run_in_executor(None, bring_up, self.transport, fw)
        if not ready:
            logger.error("RTL8814AU firmware download did not reach CPU_DL_READY")
            if progress_cb:
                progress_cb(1.0, "Firmware NOT ready")
            return False

        # Deterministic init chain M2a -> M3b-2 (all pcap-verified). Keep it in sync
        # with scripts/rtl8814au_dkms/verify_pcap.py.
        if progress_cb:
            progress_cb(0.7, "Configuring MAC / BB / RF registers")

        def _phy_config(t):
            phy_mac_config(t)     # M2a: MAC register table
            mac_init_misc(t)      # M2b: hal_init MISC stage
            phy_bb_config(t, params.rfe_type, params.crystal_cap)  # M2b: PHY_BBConfig8814
            phy_rf_config(t, params.rfe_type)                      # M2c: PHY_RFConfig8814A
            init_tune(t, _DEFAULT_CHANNEL, params.tx_power)        # M2d/M2e: ch tune + TX power
            init_hal_dm(t)                                         # M3a: InitHalDm DIG/AGC seed
            set_rfe_reg_init(t, params.rfe_type)                   # M3b-1: PHY_SetRFEReg8814A(TRUE)
            hal_init_turn_on(t, self.mac_address)                  # M3b-1: turn-on tail + MAC addr
            enter_monitor(t)                                       # M3b-2: monitor opmode (RCR/RXFLTMAP)

        await loop.run_in_executor(None, _phy_config, self.transport)
        self._channel = _DEFAULT_CHANNEL

        # M3b-3a: start the bulk-IN RX reader. It keeps a blocking bulk read posted
        # on a dedicated thread (off the event loop, so the TUI can't starve RX);
        # each aggregated buffer is split into 802.11 frames and fanned to the rx
        # callback. RSSI is a placeholder until the PHY-status decode (M3b-3b).
        self._reader = RxReaderThread(
            loop, self._read_once, self._dispatch, name="8814au-dkms-rx")
        self._reader.start()

        if progress_cb:
            progress_cb(1.0, f"Tuned to channel {_DEFAULT_CHANNEL} @ 20 MHz")
        return True

    # --- RX path (M3b-3a) --------------------------------------------------
    def _read_once(self) -> Optional[bytes]:
        """Reader-thread side: one blocking bulk-IN read (None on no traffic)."""
        return self.transport.bulk_in()

    def _dispatch(self, buf: bytes) -> None:
        """Loop side: split the aggregated bulk-IN buffer into 802.11 frames and
        fan each to the rx callback (parsed dicts). Per-frame, FCS already stripped."""
        cb = self._rx_cb
        if cb is None:
            return
        for frame in iter_frames(buf):
            parsed = WlanFrameParser.parse_80211_frame(frame, _RSSI_PLACEHOLDER)
            if parsed is not None:
                cb(parsed)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to a 2.4 GHz channel at 20 MHz. (5G tune is a later milestone.)"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, set_channel_bw, self.transport, channel, self._tx_power)
        self._channel = channel
        return True

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        raise NotImplementedError("RTL8814AU DKMS port: TX is a later milestone")

    async def close(self) -> None:
        # Stop the reader before releasing the USB handle it reads from.
        if self._reader is not None:
            await self._reader.stop()
            self._reader = None
        self.transport.close()
