"""RTL8814AU driver (Alfa AWUS1900) — WlanDriver Protocol implementation.

**Milestone status: M1 (firmware upload + FW_READY) only.** `connect()` runs the
cold bring-up through FW validation. PHY/RF init (M3), EFUSE (M4), RX/scan (M5),
and TX inject (M6) are not yet ported, so `set_channel`/`inject_frame` are
no-op stubs and no frames are delivered. The authoritative M1 hardware gate is
`scripts/rtw88_8814au/test_hw_8814au.py`.

Bring-up flow mirrors mac.c:rtw_mac_power_on + mac.c:__rtw_download_firmware,
sharing the modern iDDMA path with the 8822bu (both RTW_WCPU_3081).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

import usb.core
import usb.util

from wifit3.engine.protocols import DeviceID, ProgressCallback

from .constants import REG_SYS_CFG1, USB_IDS_8814AU
from .firmware import (
    download_firmware,
    download_firmware_validate,
    load_firmware_blob,
)
from .efuse import read_efuse
from .fifo import count_bulk_out_eps, rtw_init_trx_cfg
from .mac import cut_mask_from_sys_cfg1, is_chip_warm, mac_power_on
from .phy import defaults_from_efuse, phy_set_param
from .transport import RTL8814AUTransport

logger = logging.getLogger(__name__)


class RTL8814AUDriver:
    """Driver for Realtek RTL8814AU (Alfa AWUS1900, 4T4R). M1: FW upload only."""

    SUPPORTED_IDS = [
        DeviceID(vid, pid, desc) for (vid, pid, desc) in USB_IDS_8814AU
    ]
    # 2.4 GHz 1..13 + non-DFS 5 GHz. Channel tune lands in M3; this advertises
    # the chip's reach for when WlanInterface.start_hopping consumes it.
    SUPPORTED_CHANNELS = list(range(1, 14)) + [36, 40, 44, 48, 149, 153, 157, 161, 165]

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device,
                        id_entry: DeviceID) -> "RTL8814AUDriver":
        return cls(dev)

    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self.transport = RTL8814AUTransport(dev)
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._claimed = False
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self.current_channel: int = 1
        self.current_band_is_2g: bool = True

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_callback = cb

    def _claim(self) -> None:
        if self._claimed:
            return
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
                logger.info("detached kernel driver from interface 0")
        except (NotImplementedError, usb.core.USBError) as e:
            logger.debug("kernel-driver detach skipped: %s", e)
        try:
            self.dev.set_configuration()
        except usb.core.USBError as e:
            raise IOError(f"set_configuration failed: {e}") from e
        usb.util.claim_interface(self.dev, 0)
        self._claimed = True
        logger.info("claimed USB interface 0")

    def _release(self) -> None:
        if not self._claimed:
            return
        try:
            usb.util.release_interface(self.dev, 0)
            usb.util.dispose_resources(self.dev)
        except usb.core.USBError as e:
            logger.warning("USB release warning: %s", e)
        self._claimed = False

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        loop = asyncio.get_event_loop()

        def _progress(pct: float, msg: str) -> None:
            if progress_cb:
                progress_cb(pct, msg)
            logger.info("[%3d%%] %s", int(pct * 100), msg)

        try:
            _progress(0.00, "Claiming USB interface")
            await loop.run_in_executor(None, self._claim)

            _progress(0.05, "Probing chip state")
            warm = await loop.run_in_executor(None, is_chip_warm, self.transport)
            if warm:
                logger.info("RTL8814AU is WARM — FW already loaded (M1 satisfied)")
                self.is_warm = True
                _progress(1.00, "RTL8814AU warm (M1: FW present; RX is M5)")
                return True

            logger.info("RTL8814AU is COLD — running M1 bring-up (FW upload)")
            return await self._cold_bring_up(_progress)

        except (IOError, usb.core.USBError, NotImplementedError) as e:
            logger.error("RTL8814AU connect failed: %s", e)
            return False

    async def _cold_bring_up(self, _progress) -> bool:
        loop = asyncio.get_event_loop()

        _progress(0.10, "Reading chip version + cut_mask")
        chip_version = await loop.run_in_executor(
            None, self.transport.read32, REG_SYS_CFG1
        )
        cut_mask = cut_mask_from_sys_cfg1(chip_version)
        logger.info("REG_SYS_CFG1=0x%08x cut_mask=0x%02x", chip_version, cut_mask)

        _progress(0.20, "MAC power-on")
        await loop.run_in_executor(
            None, lambda: mac_power_on(self.transport, cut_mask=cut_mask)
        )

        _progress(0.40, "Uploading firmware (iDDMA)")
        fw = await loop.run_in_executor(None, load_firmware_blob)
        await loop.run_in_executor(
            None, lambda: download_firmware(self.dev, self.transport, fw)
        )

        _progress(0.80, "Validating FW")
        ok_run, last = await loop.run_in_executor(
            None, download_firmware_validate, self.transport
        )
        if not ok_run:
            logger.error("FW_READY not satisfied (REG_MCUFW_CTRL=0x%08x)", last)
            return False
        logger.info("RTL8814AU M1: firmware running (MCUFW_CTRL=0x%08x)", last)

        _progress(0.90, "TRX init (queue mapping + FIFO + LLT)")
        bulkout = await loop.run_in_executor(None, count_bulk_out_eps, self.dev)
        await loop.run_in_executor(
            None, lambda: rtw_init_trx_cfg(self.transport, bulkout)
        )
        logger.info("RTL8814AU M2: TRX/LLT init done (%d bulk-OUT eps)", bulkout)

        _progress(0.85, "Reading EFUSE (rfe_option, MAC, crystal_cap)")
        er = await loop.run_in_executor(None, read_efuse, self.transport)
        self.mac_address = ":".join(f"{b:02x}" for b in er.mac_addr)
        logger.info("RTL8814AU M4: EFUSE rfe_option=%d (raw 0x%02x) MAC=%s xtal=0x%02x",
                    er.rfe_option, er.rfe_option_raw, self.mac_address, er.crystal_cap)
        efuse = defaults_from_efuse(er, cut=(chip_version >> 12) & 0xF)

        _progress(0.95, "PHY init (BB/RF enable + BB/AGC/RF tables, 4 paths)")
        await loop.run_in_executor(
            None, lambda: phy_set_param(self.transport, efuse)
        )
        logger.info("RTL8814AU M3.b: PHY/RF up. Channel/RX/TX pending (M3.c/M5/M6).")
        _progress(1.00, "RTL8814AU M3.b: PHY ready (scan/inject pending)")
        return True

    async def set_channel(self, channel: int) -> bool:
        logger.warning("RTL8814AU.set_channel: channel tune lands in M3 (no-op)")
        return False

    async def inject_frame(self, frame_bytes: bytes,
                           use_no_ack: bool = True) -> bool:
        logger.warning("RTL8814AU.inject_frame: TX lands in M6 (no-op)")
        return False

    async def close(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._release)
