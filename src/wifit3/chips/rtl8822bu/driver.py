"""RTL8822BU driver — full Driver Protocol implementation.

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

from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.dot11.parser import WlanFrameParser

from .chan import (
    CHANNELS_5G_ALL,
    CHANNELS_5G_NON_DFS,
    channel_band_is_2g,
    set_channel_2g_20mhz,
    set_channel_5g_20mhz,
)
from .constants import (
    REG_SYS_CFG1,
)
from .dynamic import DigState, dig_init, dig_step, read_total_fa_cnt
from .firmware import (
    download_firmware,
    download_firmware_validate,
    load_firmware_blob,
)
from .mac import (
    admit_ack_frames,
    apply_monitor_rx_filter,
    cut_mask_from_sys_cfg1,
    drop_ack_frames,
    init_priority_queue_8822b,
    is_chip_warm,
    mac_init_for_rx,
    mac_power_on,
)
from .phy import EfuseDefaults, phy_set_param
from .rx import iter_bulk_frames, probe_endpoints
from ..rx_reader import RxReaderThread
from .transport import RTL8822BUTransport
from .tx import (
    TX_DESC_QSEL_MGMT,
    build_tx_desc_mgmt,
    pick_bulk_out_ep,
    write_bulk,
)

logger = logging.getLogger(__name__)


class RTL8822BUDriver(Driver):
    """Driver for Realtek RTL8822BU (TP-Link T3U, ASUS USB-AC55, Edimax, ...)."""

    # 2.4 GHz channels 1..14 + non-DFS 5 GHz (UNII-1 + UNII-3).
    SUPPORTED_CHANNELS = list(range(1, 15)) + list(CHANNELS_5G_NON_DFS)
    FAKE_MAC = FakeMacSupport.UNIMPLEMENTED   # active-monitor not ported for this variant

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device,
                        id_entry: DeviceID) -> "RTL8822BUDriver":
        return cls(dev)

    def __init__(self, dev: usb.core.Device):
        super().__init__()          # base owns the ACK tally (_ack_detect_on / _our_tx_macs / _ack_counts)
        self.dev = dev
        self.transport = RTL8822BUTransport(dev)
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._rx_reader: Optional[RxReaderThread] = None
        self._dig_state: Optional[DigState] = None
        self._watchdog_task: Optional[asyncio.Task] = None
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

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        """Sink for a terminal RX-reader failure (unplug). Forwarded to the RxReaderThread's
        on_fatal; resolved at call time so registration order vs connect() can't strand it."""
        self._on_lost = cb

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
        logger.debug("claimed USB interface 0")

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
                logger.info("RTL8822BU is WARM, reattaching to running session")
                return await self._warm_reattach(_progress)

            logger.info("RTL8822BU is COLD, running full bring-up")
            return await self._cold_bring_up(_progress)

        except (IOError, usb.core.USBError, NotImplementedError) as e:
            raise BringUpError("bring-up", str(e)) from e

    async def _cold_bring_up(self, _progress) -> bool:
        loop = asyncio.get_event_loop()

        _progress(0.10, "Reading chip version + computing cut_mask")
        chip_version = await loop.run_in_executor(
            None, self.transport.read32, REG_SYS_CFG1
        )
        cut_mask = cut_mask_from_sys_cfg1(chip_version)
        logger.debug("REG_SYS_CFG1=0x%08x cut_mask=0x%02x", chip_version, cut_mask)

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
            raise BringUpError("firmware", f"FW_READY not satisfied (REG_MCUFW_CTRL=0x{last:08x})")

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
            raise BringUpError("endpoints", "no bulk-IN endpoint discovered")
        self._bulk_in_ep = eps.primary_bulk_in
        self._bulk_out_eps = list(eps.bulk_out)

        await loop.run_in_executor(None, self._reset_bulk_pipes)

        if from_warm and not await self._rx_smoke_test():
            raise BringUpError(
                "warm reattach",
                "bulk-IN is wedged (no frames in 1500ms) — please unplug + replug the dongle "
                "and try again; the USB pipe state from the previous session can't be reset "
                "in userland on Windows/WinUSB.",
            )

        # Force the monitor RX filter on BOTH paths — the warm path skips
        # mac_init_for_rx, and the cold init writes the STA RCR (no AAP) that
        # drops client→AP (ToDS) frames. Mirrors rtl8821au.
        await loop.run_in_executor(None, apply_monitor_rx_filter, self.transport)

        self._rx_reader = RxReaderThread(
            loop, self._rx_read_once, self._rx_dispatch, name="rtl8822bu-rx",
            on_fatal=lambda e: self._on_lost and self._on_lost(e)
        )
        self._rx_reader.start()
        self.is_warm = True

        # DIG watchdog: re-converge the OFDM initial gain from the false-alarm
        # count every 2 s (the kernel's rtw_watch_dog_work @ HZ*2). Without it
        # IGI stays at the AGC-table default for the whole session, so RX is
        # left either deaf to weak APs or saturating on a strong one. Seed from
        # the AGC default (kernel rtw_phy_init reads dig[0]); on the warm path
        # this resumes from whatever IGI the running chip already holds.
        self._dig_state = await loop.run_in_executor(None, dig_init, self.transport)
        self._watchdog_task = asyncio.create_task(self._dig_watchdog())

        _progress(1.00, "RTL8822BU online")
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
                        None, read_total_fa_cnt, self.transport
                    )
                    await loop.run_in_executor(
                        None, dig_step, self.transport, self._dig_state, fa
                    )
                except (usb.core.USBError, IOError) as e:
                    logger.debug("DIG watchdog tick skipped: %s", e)
        except asyncio.CancelledError:
            pass

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
                logger.debug("RX smoke test: got %d bytes - pipe is alive", len(data))
                return True
        return False

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
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

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Transmit one 802.11 management frame once (e.g. a deauth). Builds the MGMT tx_pkt_desc
        (no retry-limit field, HW global retry) and bulk-OUTs ``[desc | frame]``."""
        if not self._bulk_out_eps:
            logger.error("inject_frame: no bulk-OUT endpoints")
            return False
        try:
            desc = build_tx_desc_mgmt(
                frame_bytes, band_is_2g=self.current_band_is_2g,
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

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        """Realtek HW assigns the 802.11 sequence number (the txdesc sets EN_HWSEQ), so the
        frame goes out unchanged."""
        return frame_bytes

    # ---- RX callables for the shared RxReaderThread ---------------------
    # read_once runs on the reader thread; dispatch runs on the event loop.

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
        """Decode a bulk buffer into MPDUs → parse → rx callback (on the loop)."""
        cb = self._rx_callback
        if cb is None and not self._ack_detect_on:
            return
        for stat, mpdu, rssi in iter_bulk_frames(buf):
            # A 10-byte 0xD4 frame is an ACK (the parser drops control frames); the base tallies it
            # iff the ACK tap is armed and RA=mpdu[4:10] is a MAC we inject as.
            if len(mpdu) == 10 and mpdu[0] == 0xD4:
                self.record_ack(mpdu)
                continue
            if cb is None:
                continue
            parsed = WlanFrameParser.parse_80211_frame(
                mpdu, rssi if rssi is not None else -100
            )
            if parsed:
                try:
                    cb(parsed)
                except Exception:
                    logger.exception("RX callback raised")

    async def _enable_rx_acks(self) -> None:
        """Admit ACK control frames (RXFLTMAP1 bit13) so the tap can see the AP's ACKs to us
        (this chip filters them out at the monitor default). The base arms the tally; here we
        only flip the RX filter. Not enter_active_monitor, which makes the chip emit ACKs."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, admit_ack_frames, self.transport)

    async def _disable_rx_acks(self) -> None:
        """Restore the default monitor RX filter (clear RXFLTMAP1 bit13)."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, drop_ack_frames, self.transport)

    async def close(self) -> None:
        loop = asyncio.get_event_loop()
        # Stop the DIG watchdog first — it does USB I/O via the executor.
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
