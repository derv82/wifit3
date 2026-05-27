"""RTL8814AU driver (Alfa AWUS1900) — WlanDriver Protocol implementation.

Full bring-up (HW-verified): `connect()` runs M1 power-on + iDDMA FW upload
(cold only) -> M2 TRX/FIFO/LLT -> M4 EFUSE -> M3 PHY/RF (BB/AGC/RF x4) + channel
tune, with an RF-deaf retry that re-rolls phy_set_param until the PHY actually
demodulates -> M5 monitor RX (RX-agg + promiscuous RCR + reader thread).
`set_channel` retunes (+ re-pins CCK sensitivity); `inject_frame` builds a
40-byte MGMT tx_desc and writes the HIGH bulk-OUT lane.

Warm chips (FW already running) skip M1 and re-run M2-M5. Shares the modern
RTW_WCPU_3081 iDDMA path with the 8822bu. See RTL8814AU.md + the phased
`scripts/rtw88_8814au/test_hw_8814au.py` for per-milestone HW gates.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Callable, Optional

import usb.core
import usb.util

from wifit3.engine.protocols import DeviceID, ProgressCallback
from wifit3.wlan.packet import WlanFrameParser

from .constants import REG_CCA_OFDM, REG_SYS_CFG1, USB_IDS_8814AU
from .firmware import (
    download_firmware,
    download_firmware_validate,
    load_firmware_blob,
)
from . import chan, dynamic, rx, tx
from .efuse import read_efuse
from .fifo import count_bulk_out_eps, rtw_init_trx_cfg
from .mac import cut_mask_from_sys_cfg1, is_chip_warm, mac_power_on
from .phy import defaults_from_efuse, phy_set_param
from .transport import RTL8814AUTransport
from ..rx_reader import RxReaderThread

logger = logging.getLogger(__name__)

# Band-switch RF re-lock retries (set_channel). The 2G/5G front-end occasionally
# comes up deaf (CCA=0) after a band change; a fresh switch_band re-locks it.
_BAND_RELOCK_ATTEMPTS = 4


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
        self._rx_reader: Optional[RxReaderThread] = None
        self._dig_state: Optional[dynamic.DigState] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._bulk_in_ep: Optional[int] = None
        self._bulk_out_eps: list[int] = []
        self._claimed = False
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self.current_channel: int = 1
        self.current_band_is_2g: bool = True
        self._rfe_option: int = 1

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
            self.is_warm = warm
            logger.info("RTL8814AU is %s", "WARM (skip FW upload)" if warm else "COLD")
            return await self._bring_up(_progress, warm=warm)

        except (IOError, usb.core.USBError, NotImplementedError) as e:
            logger.error("RTL8814AU connect failed: %s", e)
            return False

    async def _bring_up(self, _progress, *, warm: bool) -> bool:
        loop = asyncio.get_event_loop()

        _progress(0.10, "Reading chip version + cut_mask")
        chip_version = await loop.run_in_executor(
            None, self.transport.read32, REG_SYS_CFG1
        )
        cut_mask = cut_mask_from_sys_cfg1(chip_version)
        logger.info("REG_SYS_CFG1=0x%08x cut_mask=0x%02x", chip_version, cut_mask)

        # M1 — power-on + FW upload. Cold only; a warm chip already has FW
        # running (is_chip_warm), and re-powering would reset it.
        if not warm:
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
        self._rfe_option = er.rfe_option
        logger.info("RTL8814AU M4: EFUSE rfe_option=%d (raw 0x%02x) MAC=%s xtal=0x%02x",
                    er.rfe_option, er.rfe_option_raw, self.mac_address, er.crystal_cap)
        efuse = defaults_from_efuse(er, cut=(chip_version >> 12) & 0xF)

        # PHY/RF bring-up with deaf-retry. ~50% of cold boots the RF path comes
        # up deaf (CCA=0, hardware-confirmed); re-running phy_set_param re-rolls
        # the analog lock, so retry until RF is hearing energy. See RTL8814AU.md.
        _PHY_RF_ATTEMPTS = 8
        alive = False
        for attempt in range(_PHY_RF_ATTEMPTS):
            _progress(0.92, f"PHY/RF bring-up (attempt {attempt + 1})")
            await loop.run_in_executor(
                None, lambda: phy_set_param(self.transport, efuse))
            await loop.run_in_executor(
                None, lambda: chan.set_channel(self.transport, 1,
                                               rfe_option=self._rfe_option,
                                               force_band=True))
            await loop.run_in_executor(None, rx.mac_init_for_rx, self.transport)
            await loop.run_in_executor(None, rx.apply_monitor_rcr, self.transport)
            # Seed DIG to max coverage (IGI=0x1c) BEFORE the liveness check, so
            # the check reflects the gain the watchdog will hold — not the
            # AGC-table default that makes deaf boots a coin flip.
            self._dig_state = await loop.run_in_executor(
                None, dynamic.dig_init, self.transport)
            alive = await loop.run_in_executor(
                None, rx.rf_receiving_frames, self.transport)
            if alive:
                if attempt:
                    logger.info("RTL8814AU: RF came up after %d re-init(s)", attempt)
                break
            logger.warning("RTL8814AU: RF-deaf on attempt %d/%d — re-rolling phy",
                           attempt + 1, _PHY_RF_ATTEMPTS)
        if not alive:
            logger.error("RTL8814AU: RF stayed deaf after %d attempts. Please "
                         "unplug, wait a few seconds, replug, and retry.",
                         _PHY_RF_ATTEMPTS)
            return False

        self.current_channel = 1
        self.current_band_is_2g = True

        _progress(0.98, "Starting RX reader")
        if not await self._start_rx():
            return False
        # DIG watchdog: re-converge the OFDM initial gain from the false-alarm
        # count every 2 s (what the kernel does; we didn't). Keeps RX sensitive
        # without the static-gain deaf lottery.
        self._watchdog_task = asyncio.create_task(self._dig_watchdog())
        logger.info("RTL8814AU M5: RX online (monitor) + DIG watchdog.")
        _progress(1.00, "RTL8814AU online (monitor RX; inject pending)")
        return True

    async def _dig_watchdog(self, period_s: float = 2.0) -> None:
        """Periodic DIG tick (mirrors rtw_watch_dog_work @ HZ*2). Reads the FA
        count + steps IGI off the event loop so USB I/O never stalls the UI."""
        loop = asyncio.get_event_loop()
        try:
            while True:
                await asyncio.sleep(period_s)
                if self._dig_state is None:
                    continue
                try:
                    fa = await loop.run_in_executor(
                        None, dynamic.read_total_fa_cnt, self.transport)
                    await loop.run_in_executor(
                        None, dynamic.dig_step, self.transport,
                        self._dig_state, fa)
                except (usb.core.USBError, IOError) as e:
                    logger.debug("DIG watchdog tick skipped: %s", e)
        except asyncio.CancelledError:
            pass

    async def _start_rx(self) -> bool:
        loop = asyncio.get_event_loop()
        eps = rx.probe_endpoints(self.dev)
        if not eps.bulk_in:
            logger.error("no bulk-IN endpoint discovered")
            return False
        self._bulk_in_ep = eps.primary_bulk_in
        self._bulk_out_eps = list(eps.bulk_out)
        await loop.run_in_executor(None, rx.prime_bulk_in, self.dev, self._bulk_in_ep)
        # Surface RX throughput (produced buffers + bytes every 2s) when stats
        # are requested OR debug logging is on — so a cold-boot "death" run shows
        # whether bulk-IN delivers nothing (DMA stalled) vs bytes that don't
        # parse (alignment/desync). The RF probe passing while no frames reach
        # the host points at this RX-DMA layer, not RF.
        rx_stats = (bool(os.environ.get("WIFIT3_RX_STATS"))
                    or logger.isEnabledFor(logging.DEBUG))
        self._rx_reader = RxReaderThread(
            loop, self._rx_read_once, self._rx_dispatch, name="rtl8814au-rx",
            stats=rx_stats,
        )
        self._rx_reader.start()
        return True

    def _rx_read_once(self) -> bytes | None:
        """One blocking bulk-IN read; None on a benign timeout."""
        try:
            return bytes(self.dev.read(self._bulk_in_ep, 16384, 100))
        except usb.core.USBError as e:
            err = getattr(e, "errno", None)
            if err in (110, 10060) or "timeout" in str(e).lower():
                return None
            raise

    def _rx_dispatch(self, buf: bytes) -> None:
        cb = self._rx_callback
        if not cb:
            return
        for _stat, mpdu, rssi in rx.iter_bulk_frames(buf):
            parsed = WlanFrameParser.parse_80211_frame(
                mpdu, rssi if rssi is not None else -100
            )
            if parsed:
                try:
                    cb(parsed)
                except Exception:
                    logger.exception("RX callback raised")

    async def set_channel(self, channel: int) -> bool:
        is_2g = channel <= 14
        if is_2g and channel not in chan.SUPPORTED_CHANNELS_2G:
            logger.warning("RTL8814AU: unsupported 2.4 GHz channel %d", channel)
            return False
        if not is_2g and channel not in chan.SUPPORTED_CHANNELS_5G:
            logger.warning("RTL8814AU: unsupported 5 GHz channel %d", channel)
            return False
        band_change = is_2g != self.current_band_is_2g
        loop = asyncio.get_event_loop()
        try:
            # A/B knob: skip the per-hop writes the kernel does NOT do.
            no_extras = bool(os.environ.get("WIFIT3_NO_HOP_EXTRAS"))

            def _apply(force_band: bool) -> None:
                chan.set_channel(self.transport, channel,
                                 rfe_option=self._rfe_option, force_band=force_band)
                if no_extras:
                    return
                # cck_tx_dfir touches a shared CCK reg; re-pin monitor CCK
                # sensitivity after each tune so it survives channel hops.
                rx.tune_monitor_cck_sensitivity(self.transport)
                # Reset OFDM IGI to max coverage on the new channel; the DIG
                # watchdog then re-converges from there.
                if self._dig_state is not None:
                    self._dig_state = dynamic.dig_init(self.transport)

            def _tune():
                _apply(False)
                # On a 2G<->5G band change the RF front-end intermittently fails
                # to re-lock (CCA=0; ~30-50%). A settle does NOT help — only a
                # fresh band switch does. Verify CCA and re-lock, mirroring the
                # cold-boot deaf recovery in connect(). switch_band matches the
                # kernel byte-for-byte, so this is genuine analog re-lock variance,
                # not a missing register. [[feedback_no_bandaids_root_cause]]
                if not band_change or no_extras:
                    return
                for attempt in range(_BAND_RELOCK_ATTEMPTS):
                    rx.reset_phy_counters(self.transport)
                    time.sleep(0.04)
                    cca = (self.transport.read32(REG_CCA_OFDM) >> 16) & 0xFFFF
                    if cca > 0:
                        if attempt:
                            logger.info("RF re-locked on band switch (%d retr%s)",
                                        attempt, "y" if attempt == 1 else "ies")
                        return
                    logger.warning("band-switch RF deaf (CCA=0) ch%d, re-lock %d/%d",
                                   channel, attempt + 1, _BAND_RELOCK_ATTEMPTS)
                    _apply(True)
            await loop.run_in_executor(None, _tune)
            self.current_channel = channel
            self.current_band_is_2g = is_2g
            return True
        except (IOError, ValueError, NotImplementedError) as e:
            logger.error("set_channel(%d) failed: %s", channel, e)
            return False

    async def inject_frame(self, frame_bytes: bytes,
                           use_no_ack: bool = True) -> bool:
        if not self._bulk_out_eps:
            logger.error("inject_frame: no bulk-OUT endpoints")
            return False
        try:
            desc = tx.build_tx_desc_mgmt(
                frame_bytes, band_is_2g=self.current_band_is_2g)
        except ValueError as e:
            logger.error("inject_frame: bad MPDU: %s", e)
            return False
        ep = tx.pick_bulk_out_ep(self._bulk_out_eps, queue=tx.TX_DESC_QSEL_MGMT)
        payload = desc + frame_bytes
        loop = asyncio.get_event_loop()
        try:
            sent = await loop.run_in_executor(
                None, lambda: tx.write_bulk(self.dev, ep, payload, timeout_ms=200))
        except usb.core.USBError as e:
            logger.error("inject_frame: bulk-OUT to 0x%02x failed: %s", ep, e)
            return False
        if sent != len(payload):
            logger.warning("inject_frame: short write %d/%d", sent, len(payload))
            return False
        return True

    async def close(self) -> None:
        loop = asyncio.get_event_loop()
        # Stop the DIG watchdog first (it does USB I/O via the executor).
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None
        # Stop the reader thread BEFORE releasing USB — it's still calling
        # dev.read() until stopped, and releasing the handle under it errors.
        if self._rx_reader is not None:
            await self._rx_reader.stop()
            self._rx_reader = None
        await loop.run_in_executor(None, self._release)
