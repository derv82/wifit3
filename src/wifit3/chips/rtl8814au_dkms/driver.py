"""RTL8814AU driver — vendor (morrownr DKMS) cleanroom port.

Status: M1 (firmware upload + FW-ready ACK). Power-on, LLT, and the 3081/IDDMA
firmware download are complete and pcap-verified; PHY/MAC/RF init, channel tune,
RX and TX are later milestones and the corresponding methods raise until then.

This driver is intentionally NOT registered in ``wlan/manager.py`` yet — master
keeps the working mainline-derived ``rtw88_8814au`` port until this vendor port is
hardware-proven to beat it. Exercise M1 via ``scripts/rtl8814au_dkms/``.
"""
from __future__ import annotations

import asyncio
import logging
from importlib import resources
from typing import Callable, ClassVar, List, Optional

import usb.core

from wifit3.engine.protocols import DeviceID, ProgressCallback

from .bb import phy_bb_config
from .chan import init_tune, set_channel_bw
from .constants import PID_RTL8814AU, VID_REALTEK
from .dm import init_hal_dm
from .efuse import read_chip_params
from .firmware import bring_up
from .mac import mac_init_misc, phy_mac_config
from .rf import phy_rf_config
from .transport import Rtl8814auTransport

logger = logging.getLogger(__name__)

_FW_ASSET = "rtl8814au_fw.bin"
_DEFAULT_CHANNEL = 1  # connect-time tune target (matches the cold-boot capture)


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

        # M2a..M2d: MAC table -> MISC -> PHY_BBConfig -> PHY_RFConfig -> channel tune.
        # (Extend this chain as later milestones land; keep it in sync with
        # scripts/rtl8814au_dkms/verify_pcap.py.)
        if progress_cb:
            progress_cb(0.7, "Configuring MAC / BB / RF registers")

        def _phy_config(t):
            phy_mac_config(t)     # M2a: MAC register table
            mac_init_misc(t)      # M2b: hal_init MISC stage
            phy_bb_config(t, params.rfe_type, params.crystal_cap)  # M2b: PHY_BBConfig8814
            phy_rf_config(t, params.rfe_type)                      # M2c: PHY_RFConfig8814A
            init_tune(t, _DEFAULT_CHANNEL, params.tx_power)        # M2d/M2e: ch tune + TX power
            init_hal_dm(t)                                         # M3a: InitHalDm DIG/AGC seed

        await loop.run_in_executor(None, _phy_config, self.transport)
        self._channel = _DEFAULT_CHANNEL
        if progress_cb:
            progress_cb(1.0, f"Tuned to channel {_DEFAULT_CHANNEL} @ 20 MHz")
        return True

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
        self.transport.close()
