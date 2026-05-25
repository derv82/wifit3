"""RTL8822BU driver — full WlanDriver Protocol implementation.

Bring-up flow (mirrors `mac.c:rtw_mac_power_on` + `mac.c:__rtw_download_firmware`):

    connect()
      -> claim USB interface (cfg + claim)
      -> is_chip_warm?
           cold: full bring-up (power_on + FW upload + validate + phy + mac_init + tune)
           warm: light reattach (skip everything; smoke-test bulk-IN)
      -> probe endpoints + start RX loop
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

import usb.core
import usb.util

from wifit3.engine.protocols import DeviceID, ProgressCallback
from wifit3.wlan.packet import WlanFrameParser

from .chan import (
    CHANNELS_5G_ALL,
    CHANNELS_5G_NON_DFS,
    channel_band_is_2g,
    set_channel_2g_20mhz,
    set_channel_5g_20mhz,
)
from .constants import (
    REG_CR,
    REG_MCUFW_CTRL,
    REG_SYS_CFG1,
    USB_IDS_8822BU,
)
from .firmware import (
    download_firmware,
    download_firmware_validate,
    load_firmware_blob,
)
from .mac import (
    apply_monitor_rx_filter,
    cut_mask_from_sys_cfg1,
    init_priority_queue_8822b,
    is_chip_warm,
    mac_init_for_rx,
    mac_power_on,
)
from .phy import EfuseDefaults, phy_set_param
from .rx import iter_bulk_frames, probe_endpoints
from .transport import RTL8822BUTransport
from .tx import (
    TX_DESC_QSEL_MGMT,
    build_tx_desc_mgmt,
    pick_bulk_out_ep,
    write_bulk,
)

logger = logging.getLogger(__name__)


class RTL8822BUDriver:
    """Driver for Realtek RTL8822BU (TP-Link T3U, ASUS USB-AC55, Edimax, ...)."""

    SUPPORTED_IDS = [
        DeviceID(vid, pid, desc) for (vid, pid, desc) in USB_IDS_8822BU
    ]
    # 2.4 GHz channels 1..13 + non-DFS 5 GHz (UNII-1 + UNII-3).
    SUPPORTED_CHANNELS = list(range(1, 14)) + list(CHANNELS_5G_NON_DFS)

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device,
                        id_entry: DeviceID) -> "RTL8822BUDriver":
        return cls(dev)

    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self.transport = RTL8822BUTransport(dev)
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._rx_task: Optional[asyncio.Task] = None
        self._rx_running = False
        self._bulk_in_ep: Optional[int] = None
        self._bulk_out_eps: list[int] = []
        self._claimed = False
        self._efuse = EfuseDefaults()

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

    def _reset_bulk_pipes(self) -> None:
        eps = [self._bulk_in_ep] if self._bulk_in_ep is not None else []
        eps += self._bulk_out_eps
        for ep in eps:
            try:
                self.dev.clear_halt(ep)
                logger.debug("cleared halt on endpoint 0x%02x", ep)
            except (usb.core.USBError, NotImplementedError) as e:
                logger.debug("clear_halt(0x%02x) skipped: %s", ep, e)

        if self._bulk_in_ep is not None:
            drained = 0
            for _ in range(8):
                try:
                    data = self.dev.read(self._bulk_in_ep, 16384, 20)
                    drained += len(data)
                except usb.core.USBError:
                    break
            if drained:
                logger.debug("drained %d stale bytes from bulk-IN", drained)

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
                logger.info("RTL8822BU is WARM — reattaching to running session")
                return await self._warm_reattach(_progress)

            logger.info("RTL8822BU is COLD — running full bring-up")
            return await self._cold_bring_up(_progress)

        except (IOError, usb.core.USBError, NotImplementedError) as e:
            logger.error("RTL8822BU connect failed: %s", e)
            return False

    async def _cold_bring_up(self, _progress) -> bool:
        loop = asyncio.get_event_loop()

        _progress(0.10, "Reading chip version + computing cut_mask")
        chip_version = await loop.run_in_executor(
            None, self.transport.read32, REG_SYS_CFG1
        )
        cut_mask = cut_mask_from_sys_cfg1(chip_version)
        logger.info("REG_SYS_CFG1=0x%08x cut_mask=0x%02x", chip_version, cut_mask)

        _progress(0.15, "MAC power-on")
        await loop.run_in_executor(
            None, lambda: mac_power_on(self.transport, cut_mask=cut_mask)
        )

        _progress(0.30, "Uploading firmware (iDDMA)")
        fw = await loop.run_in_executor(None, load_firmware_blob)
        await loop.run_in_executor(
            None, lambda: download_firmware(self.dev, self.transport, fw)
        )

        _progress(0.55, "Validating FW")
        ok_run, last = await loop.run_in_executor(
            None, download_firmware_validate, self.transport
        )
        if not ok_run:
            logger.error("FW_READY not satisfied (REG_MCUFW_CTRL=0x%08x)", last)
            return False

        _progress(0.70, "PHY init (mac/bb/agc/rf tables)")
        await loop.run_in_executor(
            None, lambda: phy_set_param(self.transport, self._efuse)
        )

        _progress(0.85, "MAC init for RX")
        await loop.run_in_executor(None, mac_init_for_rx, self.transport)

        _progress(0.90, "Init priority queues + LLT (for TX)")
        await loop.run_in_executor(
            None, init_priority_queue_8822b, self.transport
        )

        _progress(0.95, "Tuning to channel 1")
        await loop.run_in_executor(
            None, lambda: set_channel_2g_20mhz(self.transport, 1)
        )
        self.current_channel = 1
        self.current_band_is_2g = True

        return await self._finish_attach(_progress, from_warm=False)

    async def _warm_reattach(self, _progress) -> bool:
        _progress(0.50, "Warm chip — skipping FW + init")
        return await self._finish_attach(_progress, from_warm=True)

    async def _finish_attach(self, _progress, *, from_warm: bool) -> bool:
        loop = asyncio.get_event_loop()
        eps = probe_endpoints(self.dev)
        if not eps.bulk_in:
            logger.error("no bulk-IN endpoint discovered")
            return False
        self._bulk_in_ep = eps.primary_bulk_in
        self._bulk_out_eps = list(eps.bulk_out)

        await loop.run_in_executor(None, self._reset_bulk_pipes)

        if from_warm and not await self._rx_smoke_test():
            logger.error(
                "RTL8822BU: warm reattach succeeded but bulk-IN is wedged "
                "(no frames in 1500ms). Please unplug + replug the dongle "
                "and try again — the USB pipe state from the previous "
                "session can't be reset in userland on Windows/WinUSB."
            )
            return False

        # Force the monitor RX filter on BOTH paths — the warm path skips
        # mac_init_for_rx, and the cold init writes the STA RCR (no AAP) that
        # drops client→AP (ToDS) frames. Mirrors rtl8821au.
        await loop.run_in_executor(None, apply_monitor_rx_filter, self.transport)

        self._rx_running = True
        self._rx_task = asyncio.create_task(self._rx_loop())
        self.is_warm = True
        _progress(1.00, "RTL8822BU online")
        return True

    async def _rx_smoke_test(self, attempts: int = 15,
                             timeout_ms: int = 100) -> bool:
        loop = asyncio.get_event_loop()

        def _try_read():
            try:
                return bytes(self.dev.read(self._bulk_in_ep, 16384, timeout_ms))
            except usb.core.USBError:
                return b""

        for _ in range(attempts):
            data = await loop.run_in_executor(None, _try_read)
            if data:
                logger.info("RX smoke test: got %d bytes — pipe is alive", len(data))
                return True
        return False

    async def set_channel(self, channel: int) -> bool:
        is_2g = channel_band_is_2g(channel)
        if is_2g and not (1 <= channel <= 14):
            logger.warning("RTL8822BU: invalid 2.4 GHz channel %d", channel)
            return False
        if not is_2g and channel not in CHANNELS_5G_ALL:
            logger.warning("RTL8822BU: unsupported 5 GHz channel %d", channel)
            return False

        loop = asyncio.get_event_loop()
        try:
            tune = set_channel_2g_20mhz if is_2g else set_channel_5g_20mhz
            await loop.run_in_executor(
                None,
                lambda: tune(self.transport, channel,
                             antenna_tx_paths=self._efuse.antenna_tx_paths,
                             antenna_rx_paths=self._efuse.antenna_rx_paths),
            )
            self.current_channel = channel
            self.current_band_is_2g = is_2g
            return True
        except (IOError, usb.core.USBError, ValueError) as e:
            logger.error("set_channel(%d) failed: %s", channel, e)
            return False

    async def inject_frame(self, frame_bytes: bytes,
                           use_no_ack: bool = True) -> bool:
        if not self._bulk_out_eps:
            logger.error("inject_frame: no bulk-OUT endpoints")
            return False
        try:
            desc = build_tx_desc_mgmt(
                frame_bytes, band_is_2g=self.current_band_is_2g
            )
        except ValueError as e:
            logger.error("inject_frame: bad MPDU: %s", e)
            return False
        ep = pick_bulk_out_ep(self._bulk_out_eps, queue=TX_DESC_QSEL_MGMT)
        payload = desc + frame_bytes
        loop = asyncio.get_event_loop()
        try:
            sent = await loop.run_in_executor(
                None,
                lambda: write_bulk(self.dev, ep, payload, timeout_ms=200),
            )
        except usb.core.USBError as e:
            logger.error("inject_frame: bulk-OUT to 0x%02x failed: %s", ep, e)
            return False
        if sent != len(payload):
            logger.warning("inject_frame: short write %d/%d to 0x%02x",
                           sent, len(payload), ep)
            return False
        return True

    async def _rx_loop(self) -> None:
        loop = asyncio.get_event_loop()
        ep = self._bulk_in_ep
        consec_errors = 0

        def _read_once() -> bytes | None:
            try:
                return bytes(self.dev.read(ep, 16384, 100))
            except usb.core.USBError as e:
                err = getattr(e, "errno", None)
                if err in (110, 10060) or "timeout" in str(e).lower():
                    return None
                raise

        logger.info("RX loop started on endpoint 0x%02x", ep)
        while self._rx_running:
            try:
                buf = await loop.run_in_executor(None, _read_once)
            except usb.core.USBError as e:
                consec_errors += 1
                logger.warning("RX read failed (%d/5): %s", consec_errors, e)
                if consec_errors >= 5:
                    logger.error("RX giving up after 5 consecutive errors")
                    break
                await asyncio.sleep(0.01)
                continue

            if buf is None:
                consec_errors = 0
                await asyncio.sleep(0)
                continue
            consec_errors = 0

            for stat, mpdu, rssi in iter_bulk_frames(buf):
                if not self._rx_callback:
                    continue
                parsed = WlanFrameParser.parse_80211_frame(
                    mpdu, rssi if rssi is not None else -100
                )
                if parsed:
                    try:
                        self._rx_callback(parsed)
                    except Exception:
                        logger.exception("RX callback raised")
        logger.info("RX loop stopped")

    async def close(self) -> None:
        self._rx_running = False
        if self._rx_task:
            try:
                await asyncio.wait_for(self._rx_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._rx_task.cancel()
            self._rx_task = None
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._release)
