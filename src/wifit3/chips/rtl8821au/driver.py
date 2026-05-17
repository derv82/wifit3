"""RTL8821AU driver — glues the bring-up chain onto the WlanDriver Protocol.

Composition only: every step delegates to the layered modules in this
package (mac.py, firmware.py, phy.py, chan.py, rx.py, transport.py).

Bring-up flow (mirrors `rtw88xxa_power_on`):

    connect()
      -> claim USB interface (cfg + claim)
      -> mac_power_on               (mac.py)
      -> pre_fw_init                (mac.py)        sets fifo + runs LLT init
      -> en_download_firmware_legacy(True)
      -> download_firmware_legacy   (firmware.py)
      -> en_download_firmware_legacy(False)
      -> download_firmware_validate_legacy           wait FW_READY_LEGACY=0xC6
      -> post_fw_mac_init           (mac.py)        REG_CR |= MACTXEN|MACRXEN
      -> post_mac_init_phy          (phy.py)        4 tables + switch_band(2G)
      -> set_channel_2g_20mhz(1)    (chan.py)       default channel
      -> start RX loop
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

import usb.core
import usb.util

from wifit3.engine.protocols import DeviceID, ProgressCallback
from wifit3.wlan.packet import WlanFrameParser

from .chan import set_channel_2g_20mhz
from .firmware import (
    download_firmware_legacy,
    download_firmware_validate_legacy,
    en_download_firmware_legacy,
    load_firmware_blob,
)
from .mac import mac_power_on, post_fw_mac_init, pre_fw_init
from .phy import EfuseDefaults, post_mac_init_phy
from .rx import iter_bulk_frames, probe_endpoints
from .transport import RTL8821AUTransport

logger = logging.getLogger(__name__)


class RTL8821AUDriver:
    """Driver for the Realtek RTL8821AU (e.g. ALFA AWUS036ACS).

    Single-chain RX (synchronous bulk reads polled in a worker thread).
    TX injection is not yet implemented (M7).
    """

    SUPPORTED_IDS = [
        DeviceID(0x0BDA, 0x0811, "Realtek RTL8821AU / ALFA AWUS036ACS"),
    ]

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RTL8821AUDriver":
        return cls(dev)

    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self.transport = RTL8821AUTransport(dev)
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._rx_task: Optional[asyncio.Task] = None
        self._rx_running = False
        self._bulk_in_ep: Optional[int] = None
        self._claimed = False
        self._efuse = EfuseDefaults()

        # WlanDriver Protocol surface area.
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self.current_channel: int = 1

    # ---- discovery hook ---------------------------------------------------
    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_callback = cb

    # ---- USB claim helpers -----------------------------------------------
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

    # ---- connect ----------------------------------------------------------
    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        loop = asyncio.get_event_loop()

        def _progress(pct: float, msg: str) -> None:
            if progress_cb:
                progress_cb(pct, msg)
            logger.info("[%3d%%] %s", int(pct * 100), msg)

        try:
            _progress(0.00, "Claiming USB interface")
            await loop.run_in_executor(None, self._claim)

            _progress(0.05, "MAC power-on")
            await loop.run_in_executor(None, mac_power_on, self.transport)

            _progress(0.10, "Pre-FW init (LLT + DROP_DATA_EN)")
            fifo = await loop.run_in_executor(None, pre_fw_init, self.transport)

            _progress(0.20, "Enable FW download")
            await loop.run_in_executor(
                None, en_download_firmware_legacy, self.transport, True
            )

            _progress(0.30, "Uploading firmware")
            fw = await loop.run_in_executor(None, load_firmware_blob)
            await loop.run_in_executor(
                None,
                lambda: download_firmware_legacy(self.transport, fw, None, False),
            )

            _progress(0.55, "Disable FW download")
            await loop.run_in_executor(
                None, en_download_firmware_legacy, self.transport, False
            )

            _progress(0.60, "Validating FW (FW_READY_LEGACY)")
            ok_run, last = await loop.run_in_executor(
                None, download_firmware_validate_legacy, self.transport
            )
            if not ok_run:
                logger.error("FW_READY_LEGACY not satisfied (REG_MCUFW_CTRL=0x%08x)", last)
                return False

            _progress(0.70, "Post-FW MAC init")
            await loop.run_in_executor(None, post_fw_mac_init, self.transport, fifo)

            _progress(0.85, "PHY init (mac/bb/agc/rf tables)")
            await loop.run_in_executor(None, post_mac_init_phy, self.transport, self._efuse)

            _progress(0.95, "Tuning to channel 1")
            await loop.run_in_executor(None, set_channel_2g_20mhz, self.transport, 1)
            self.current_channel = 1

            # Discover bulk-IN endpoint and start the RX loop.
            eps = probe_endpoints(self.dev)
            if not eps.bulk_in:
                logger.error("no bulk-IN endpoint discovered")
                return False
            self._bulk_in_ep = eps.primary_bulk_in
            self._rx_running = True
            self._rx_task = asyncio.create_task(self._rx_loop())

            self.is_warm = True
            _progress(1.00, "RTL8821AU online")
            return True

        except (IOError, usb.core.USBError, NotImplementedError) as e:
            logger.error("RTL8821AU connect failed: %s", e)
            return False

    # ---- set_channel ------------------------------------------------------
    async def set_channel(self, channel: int) -> bool:
        if not (1 <= channel <= 13):
            logger.warning("RTL8821AU: channel %d not supported (1..13 only)", channel)
            return False
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, set_channel_2g_20mhz, self.transport, channel
            )
            self.current_channel = channel
            return True
        except (IOError, usb.core.USBError) as e:
            logger.error("set_channel(%d) failed: %s", channel, e)
            return False

    # ---- inject_frame (TX not yet implemented) ---------------------------
    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        logger.warning("RTL8821AU: inject_frame not yet implemented (M7)")
        return False

    # ---- RX loop ----------------------------------------------------------
    async def _rx_loop(self) -> None:
        """Synchronous bulk reads polled in a worker thread."""
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
                await asyncio.sleep(0)  # yield
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

    # ---- close ------------------------------------------------------------
    async def close(self) -> None:
        self._rx_running = False
        if self._rx_task:
            try:
                await asyncio.wait_for(self._rx_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._rx_task.cancel()
            self._rx_task = None
        # Run USB release in an executor; PyUSB calls block.
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._release)
