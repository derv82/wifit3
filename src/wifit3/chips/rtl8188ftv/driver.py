"""RTL8188FTV driver — no-name 0bda:f179 dongles (Realtek RTL8188FTV).

Cleanroom port of the kernel `rtl8xxxu` driver's 8188f fileops vector
(`driver_sources/rtl8xxxu-source-v6.12.107/8188f.c:1708-1764`).

M1-M7 scope (complete bring-up):

    connect()
      ├─ claim USB interface
      ├─ probe chip state (is_chip_warm)
      ├─ COLD path:
      │   ├─ identify_chip (read REG_SYS_CFG)
      │   ├─ power_on (disabled → emu → active, 8188f.c:1318-1537)
      │   ├─ FW upload + start (M1)
      │   ├─ EFUSE read + parse (M2)
      │   ├─ post-FW MAC init: init_mac + queue/LLE/usb_quirks (M3)
      │   ├─ post_mac_init_phy: BB + AGC + crystal cap + RF (M3)
      │   ├─ enable_rx_data_path (RCR + DRVINFO_SZ + interrupts, M5)
      │   ├─ set_tx_power (M5, efuse-derived ch1)
      │   ├─ LC calibration (M4)
      │   ├─ IQ calibration (M4)
      │   ├─ enable_rf (M4)
      │   └─ set_channel(1) (M5: RX filt maps + AGC IGI + monitor RCR + tune)
      └─ WARM path: skip everything above (chip already running)

      then (both paths) → _finish_attach:
        ├─ probe USB endpoints
        ├─ reset bulk pipes
        └─ spawn _rx_loop asyncio task

Milestones M1-M4 gate against the cold-boot capture (verify_pcap.py);
M5 adds the RX acceptance + channel-1 tune region; M6 the full 53-hop
channel-scan loop, so the capture is replayed end to end.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

import usb.core
import usb.util

from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError

from .constants import (
    APS_FSMCO_HW_SUSPEND,
    APS_FSMCO_MAC_ENABLE,
    APS_FSMCO_PCIE,
    CR_HCI_TXDMA_ENABLE,
    CR_HCI_RXDMA_ENABLE,
    CR_TXDMA_ENABLE,
    CR_RXDMA_ENABLE,
    CR_PROTOCOL_ENABLE,
    CR_SCHEDULE_ENABLE,
    CR_SECURITY_ENABLE,
    CR_CALTIMER_ENABLE,
    REG_APS_FSMCO,
    REG_CR,
    REG_SYS_CFG,
    RTL8XXXU_MAX_REG_POLL,
    SYS_CFG_CHIP_VERSION_MASK,
    SYS_CFG_TRP_VAUX_EN,
)
from .firmware import download_firmware, load_firmware_blob, start_firmware
from .transport import RTL8188FTVTransport

logger = logging.getLogger(__name__)

CR_INIT_POWER_ON = (
    CR_HCI_TXDMA_ENABLE | CR_HCI_RXDMA_ENABLE |
    CR_TXDMA_ENABLE | CR_RXDMA_ENABLE |
    CR_PROTOCOL_ENABLE | CR_SCHEDULE_ENABLE |
    CR_SECURITY_ENABLE | CR_CALTIMER_ENABLE
)


class RTL8188FTVDriver(Driver):
    """Driver for the Realtek RTL8188FTV (no-name 0bda:f179 dongles)."""

    SUPPORTED_CHANNELS = list(range(1, 15))   # 2.4 GHz only
    FAKE_MAC = FakeMacSupport.UNIMPLEMENTED

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RTL8188FTVDriver":
        return cls(dev)

    def __init__(self, dev: usb.core.Device):
        super().__init__()
        self.dev = dev
        self.transport = RTL8188FTVTransport(dev)
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._mgmt_bulk_out: Optional[int] = None
        self._bulk_in_ep: Optional[int] = None
        self._bulk_out_eps: list[int] = []
        self._rx_reader = None
        self._claimed: bool = False
        self.current_channel: int = 1
        self.chip_cut: int = 0

    # ---- Driver Protocol surface ------------------------------------

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_callback = cb

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        self._on_lost = cb

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        loop = asyncio.get_running_loop()

        def _update(pct: float, msg: str) -> None:
            if progress_cb:
                progress_cb(pct, msg)
            logger.info("Progress %d%%: %s", int(pct * 100), msg)

        try:
            _update(0.02, "Claiming USB interface...")
            await loop.run_in_executor(None, self._claim_usb)

            _update(0.05, "Probing chip state...")
            warm = await loop.run_in_executor(None, self._is_chip_warm)

            if warm:
                logger.info("RTL8188FTV is WARM, reattaching to running session")
                return await self._warm_reattach(_update)

            logger.info("RTL8188FTV is COLD, running full bring-up")
            return await self._cold_bring_up(_update)
        except Exception as e:
            raise BringUpError("bring-up", str(e)) from e

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        from .chan import set_channel_2g_20mhz
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, set_channel_2g_20mhz, self.transport, channel)
            self.current_channel = channel
            return True
        except (ValueError, IOError):
            logger.exception("set_channel(%d) failed", channel)
            return False

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        loop = asyncio.get_running_loop()
        if self._mgmt_bulk_out is None:
            from .rx import probe_endpoints
            eps = await loop.run_in_executor(None, probe_endpoints, self.dev)
            self._mgmt_bulk_out = eps.bulk_out[0] if eps.bulk_out else None
        if self._mgmt_bulk_out is None:
            return False
        from .tx import send_mgmt_frame
        is_bcast = (frame_bytes[4:10][0] & 0x01) != 0 if len(frame_bytes) >= 10 else False
        try:
            await loop.run_in_executor(
                None, lambda: send_mgmt_frame(
                    self.dev, self._mgmt_bulk_out, frame_bytes, is_broadcast=is_bcast),
            )
        except (IOError, usb.core.USBError):
            logger.exception("inject_frame failed")
            return False
        return True

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        return frame_bytes

    async def close(self) -> None:
        if self._rx_reader is not None:
            await self._rx_reader.stop()
            self._rx_reader = None
        self._release_usb()

    # ---- bring-up paths ---------------------------------------------

    async def _cold_bring_up(self, _update) -> bool:
        loop = asyncio.get_running_loop()

        _update(0.10, "Reading chip ID...")
        sys_cfg = await loop.run_in_executor(None, self.transport.read32, REG_SYS_CFG)
        self.chip_cut = (sys_cfg & SYS_CFG_CHIP_VERSION_MASK) >> 12
        logger.debug("REG_SYS_CFG = 0x%08x  chip_cut=%d", sys_cfg, self.chip_cut)
        if sys_cfg & SYS_CFG_TRP_VAUX_EN:
            raise RuntimeError("Unsupported test chip (TRP_VAUX_EN set)")

        _update(0.15, "Power on...")
        await loop.run_in_executor(None, self._power_on)

        _update(0.25, "Loading firmware blob...")
        fw_blob = load_firmware_blob()

        _update(0.35, f"Uploading firmware ({len(fw_blob)} B)...")
        await loop.run_in_executor(None, download_firmware, self.transport, fw_blob)

        _update(0.50, "Polling for MCU_WINT_INIT_READY...")
        await loop.run_in_executor(None, start_firmware, self.transport)

        _update(1.00, "RTL8188FTV online.")
        return True

    async def _warm_reattach(self, _update) -> bool:
        _update(0.50, "Warm chip — skipping FW upload")
        return True

    # ---- power-on internals (8188f.c:1318-1537) ---------------------

    def _power_on(self) -> None:
        self._disabled_to_emu()
        self._emu_to_active()
        t = self.transport
        t.write8(REG_CR, 0)
        val16 = t.read16(REG_CR)
        val16 |= CR_INIT_POWER_ON
        t.write16(REG_CR, val16)

    def _disabled_to_emu(self) -> None:
        t = self.transport
        val8 = t.read8(REG_APS_FSMCO + 1)
        val8 &= ~((APS_FSMCO_PCIE | APS_FSMCO_HW_SUSPEND) >> 8) & 0xFF
        t.write8(REG_APS_FSMCO + 1, val8)
        val8 = t.read8(0xC4)
        val8 &= ~0x10
        t.write8(0xC4, val8)

    def _emu_to_active(self) -> None:
        t = self.transport
        # Disable SW LPS
        val8 = t.read8(REG_APS_FSMCO + 1)
        val8 &= ~(0x04)  # APS_FSMCO_SW_LPS >> 8 = BIT2
        t.write8(REG_APS_FSMCO + 1, val8)

        for _ in range(RTL8XXXU_MAX_REG_POLL):
            if t.read32(REG_APS_FSMCO) & (1 << 17):
                break
            time.sleep(0.00001)
        else:
            raise IOError("emu_to_active: power-ready bit never set")

        # Disable HWPDN
        val8 = t.read8(REG_APS_FSMCO + 1)
        val8 &= ~0x80  # APS_FSMCO_HW_POWERDOWN >> 8 = BIT7
        t.write8(REG_APS_FSMCO + 1, val8)

        # Disable WL suspend
        val8 = t.read8(REG_APS_FSMCO + 1)
        val8 &= ~0x08  # APS_FSMCO_HW_SUSPEND >> 8 = BIT3
        t.write8(REG_APS_FSMCO + 1, val8)

        # Set MAC_ENABLE, poll until self-clears
        val8 = t.read8(REG_APS_FSMCO + 1)
        val8 |= 0x01  # APS_FSMCO_MAC_ENABLE >> 8 = BIT0
        t.write8(REG_APS_FSMCO + 1, val8)

        for _ in range(RTL8XXXU_MAX_REG_POLL):
            if (t.read32(REG_APS_FSMCO) & APS_FSMCO_MAC_ENABLE) == 0:
                break
            time.sleep(0.00001)
        else:
            raise IOError("emu_to_active: MAC_ENABLE bit never cleared")

        t.write8(0x27, 0x35)

    # ---- USB helpers -------------------------------------------------

    def _claim_usb(self) -> None:
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
            logger.debug("set_configuration: %s", e)
        usb.util.claim_interface(self.dev, 0)
        self._claimed = True

    def _release_usb(self) -> None:
        if not self._claimed:
            return
        try:
            usb.util.release_interface(self.dev, 0)
            usb.util.dispose_resources(self.dev)
        except usb.core.USBError as e:
            logger.warning("USB release warning: %s", e)
        self._claimed = False

    # ---- chip warm detection ----------------------------------------

    def _is_chip_warm(self) -> bool:
        from .mac import is_chip_warm
        return is_chip_warm(self.transport)