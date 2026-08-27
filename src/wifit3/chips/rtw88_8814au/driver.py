"""RTL8814AU driver (Alfa AWUS1900) — Driver Protocol implementation.

Bring-up: `connect()` runs M1 power-on + iDDMA FW upload (cold only) -> M2
TRX/FIFO/LLT -> M4 EFUSE -> M3 PHY/RF (BB/AGC/RF x4) + channel tune, with an
RF-deaf retry that re-rolls phy_set_param until the PHY demodulates -> M5
monitor RX (promiscuous RCR + reader thread). `set_channel` retunes (+ re-pins
CCK sensitivity); `inject_frame` builds a 40-byte MGMT tx_desc and writes the
HIGH bulk-OUT lane.

Warm chips (FW already running) skip M1 and re-run M2-M5. Shares the modern
RTW_WCPU_3081 iDDMA path with the 8822bu. See RTL8814AU.md + the phased
`scripts/chips/rtw88_8814au/test_hw_8814au.py` for per-milestone HW gates.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Callable, Optional

import usb.core
import usb.util

from wifit3.chips import log_trace
from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.dot11.parser import WlanFrameParser

from wifit3.chips.rtw88_base.registers import (
    BIT_HCI_RXDMA_EN,
    BIT_MACRXEN,
    BIT_RXDMA_EN,
)
from . import constants as C
from .constants import REG_CCA_OFDM, REG_SYS_CFG1
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


class RTL8814AUDriver(Driver):
    """Driver for Realtek RTL8814AU (Alfa AWUS1900, 4T4R). M1: FW upload only."""

    # 2.4 GHz 1..13 + non-DFS 5 GHz. Channel tune lands in M3; this advertises
    # the chip's reach for when WlanInterface.start_hopping consumes it.
    SUPPORTED_CHANNELS = list(range(1, 15)) + [36, 40, 44, 48, 149, 153, 157, 161, 165]
    FAKE_MAC = FakeMacSupport.UNIMPLEMENTED   # active-monitor not ported for this variant

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device,
                        id_entry: DeviceID) -> "RTL8814AUDriver":
        return cls(dev)

    def __init__(self, dev: usb.core.Device):
        super().__init__()          # base owns the ACK tally (_ack_detect_on / _our_tx_macs / _ack_counts)
        self.dev = dev
        self.transport = RTL8814AUTransport(dev)
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._rx_reader: Optional[RxReaderThread] = None
        # Opt-in RX diagnostics (throughput log + RX-DMA register dump) for the
        # intermittent cold-boot "RX-DMA delivers nothing" hunt.
        self._rx_stats = bool(os.environ.get("WIFIT3_RX_STATS"))
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
            raise BringUpError("bring-up", str(e)) from e

    async def _bring_up(self, _progress, *, warm: bool) -> bool:
        loop = asyncio.get_event_loop()

        _progress(0.10, "Reading chip version + cut_mask")
        chip_version = await loop.run_in_executor(
            None, self.transport.read32, REG_SYS_CFG1
        )
        cut_mask = cut_mask_from_sys_cfg1(chip_version)
        logger.debug("REG_SYS_CFG1=0x%08x cut_mask=0x%02x", chip_version, cut_mask)

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
                raise BringUpError("firmware", f"FW_READY not satisfied (REG_MCUFW_CTRL=0x{last:08x})")
            logger.info("RTL8814AU M1: firmware running (MCUFW_CTRL=0x%08x)", last)

        _progress(0.90, "TRX init (queue mapping + FIFO + LLT)")
        bulkout = await loop.run_in_executor(None, count_bulk_out_eps, self.dev)
        await loop.run_in_executor(
            None, lambda: rtw_init_trx_cfg(self.transport, bulkout)
        )
        logger.debug("RTL8814AU M2: TRX/LLT init done (%d bulk-OUT eps)", bulkout)

        _progress(0.85, "Reading EFUSE (rfe_option, MAC, crystal_cap)")
        er = await loop.run_in_executor(None, read_efuse, self.transport)
        self.mac_address = ":".join(f"{b:02x}" for b in er.mac_addr)
        self._rfe_option = er.rfe_option
        logger.info("RTL8814AU M4: EFUSE rfe_option=%d (raw 0x%02x) MAC=%s xtal=0x%02x",
                    er.rfe_option, er.rfe_option_raw, self.mac_address, er.crystal_cap)
        efuse = defaults_from_efuse(er, cut=(chip_version >> 12) & 0xF)

        # Post a bulk-IN read BEFORE RX is enabled, mirroring the kernel: it
        # submits its RX URB ring at probe — long before MAC RX goes live — so
        # the pipe is drained from the instant the first frame is accepted. With
        # the reader started only after mac_init_for_rx opened RCR (and after
        # dig_init), there was an undrained window in which the device's RX path
        # backs up — BB keeps decoding into a FIFO nobody reads — and latches a
        # wedge that survives until replug (the cold-boot "few frames then
        # silent" lottery). The rx callback is None until connect() returns, so
        # frames drained during bring-up are simply discarded.
        if not await self._start_rx():
            raise BringUpError("rx", "RX pipe failed to start")

        # PHY/RF bring-up. The DIG-max-coverage seed + RX-aggregation-off fixes
        # made cold boots reliable, so the deaf re-roll is on probation: 1 attempt.
        # A deaf boot now fails connect() loudly instead of being silently
        # re-rolled. If 10/10 cold boots stay clean, drop the loop + liveness
        # probe entirely. See RTL8814AU.md known gaps.
        _PHY_RF_ATTEMPTS = 1
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
            logger.warning("RTL8814AU: RF-deaf on attempt %d/%d - re-rolling phy",
                           attempt + 1, _PHY_RF_ATTEMPTS)
        if not alive:
            raise BringUpError(
                "phy/rf",
                "RF stayed deaf after bring-up — please unplug, wait a few seconds, replug, and retry.",
            )

        self.current_channel = 1
        self.current_band_is_2g = True

        # DIG watchdog: re-converge the OFDM initial gain from the false-alarm
        # count every 2 s (what the kernel does; we didn't). Keeps RX sensitive
        # without the static-gain deaf lottery.
        self._watchdog_task = asyncio.create_task(self._dig_watchdog())
        logger.debug("RTL8814AU M5: RX online (monitor) + DIG watchdog.")
        self._log_rx_dma_state("online")
        _progress(1.00, "RTL8814AU online (monitor RX; inject pending)")
        return True

    def _log_rx_dma_state(self, tag: str, crc_ok: int | None = None) -> None:
        """Dump the whole RX pipe (gated on WIFIT3_RX_STATS) so a failed cold
        boot (delivers nothing) can be diffed against a good one. Covers both
        ends: the MAC RX *filter* (RCR + RXFLTMAP) and the RX-DMA/FIFO. When the
        caller passes `crc_ok` (the BB demod count for this window, sampled
        before the watchdog's counter reset), the line answers the key question
        directly: on a wedged boot, is the BB still decoding (crc_ok climbs)
        while the RX FIFO stays empty — i.e. frames dropped between demod and
        DMA — or has the BB itself gone dead? The filter readback flags whether
        RCR/RXFLTMAP drifted from what mac_init_for_rx wrote. All read-only."""
        if not self._rx_stats:
            return
        if not logger.isEnabledFor(log_trace.TRACE):
            return
        try:
            cr = self.transport.read32(C.REG_CR)
            pkt = self.transport.read32(C.REG_RXPKT_NUM)
            pq = self.transport.read32(C.REG_TXDMA_PQ_MAP)
            bndy = self.transport.read16(C.REG_RXFF_BNDY)
            mode = self.transport.read8(C.REG_RXDMA_MODE)
            rcr = self.transport.read32(C.REG_RCR)
            flt0 = self.transport.read16(C.REG_RXFLTMAP0)
            flt1 = self.transport.read16(C.REG_RXFLTMAP1)
            flt2 = self.transport.read16(C.REG_RXFLTMAP2)
        except (usb.core.USBError, IOError) as e:
            logger.debug("RX-DMA state read failed: %s", e)
            return
        # Flag drift from the init-time writes.
        rcr_bad = "" if rcr == C.RCR_MONITOR else f" !=0x{C.RCR_MONITOR:08x}"
        flt_bad = "" if (flt0, flt1, flt2) == (
            C.RXFLTMAP0_8814A, C.RXFLTMAP1_8814A, C.RXFLTMAP2_8814A) else " !=init"
        crc_str = "" if crc_ok is None else f" BB-crc-ok(2s)={crc_ok}"
        logger.debug(
            "RX-DMA state [%s]: CR=0x%08x(rxdma_en=%d hci_rxdma=%d macrxen=%d) "
            "RXPKT_NUM=0x%08x(idle=%d) PQ_MAP=0x%08x(agg_en=%d) BNDY=0x%04x MODE=0x%02x%s",
            tag, cr, bool(cr & BIT_RXDMA_EN), bool(cr & BIT_HCI_RXDMA_EN),
            bool(cr & BIT_MACRXEN), pkt, bool(pkt & C.BIT_RXDMA_IDLE),
            pq, bool(pq & C.BIT_RXDMA_AGG_EN), bndy, mode, crc_str)
        logger.debug(
            "RX filter [%s]: RCR=0x%08x(aap=%d)%s FLTMAP=%04x/%04x/%04x%s",
            tag, rcr, bool(rcr & 0x1), rcr_bad, flt0, flt1, flt2, flt_bad)

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
                    # Sample BB CRC-ok BEFORE read_total_fa_cnt resets the
                    # counters, so the watchdog dump shows this window's demod
                    # count alongside the RX-DMA/FIFO state.
                    crc_ok = (await loop.run_in_executor(
                        None, rx.read_crc_ok, self.transport)
                        if self._rx_stats else None)
                    fa = await loop.run_in_executor(
                        None, dynamic.read_total_fa_cnt, self.transport)
                    await loop.run_in_executor(
                        None, dynamic.dig_step, self.transport,
                        self._dig_state, fa)
                    if self._rx_stats:
                        await loop.run_in_executor(
                            None, self._log_rx_dma_state, "watchdog", crc_ok)
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
        self._rx_reader = RxReaderThread(
            loop, self._rx_read_once, self._rx_dispatch, name="rtl8814au-rx",
            stats=self._rx_stats,
            on_fatal=lambda e: self._on_lost and self._on_lost(e))
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
        if cb is None and not self._ack_detect_on:
            return
        for _stat, mpdu, rssi in rx.iter_bulk_frames(buf):
            # A 10-byte 0xD4 frame is an ACK (the parser drops control frames); the base tallies
            # it iff the ACK tap is armed and RA=mpdu[4:10] is a MAC we inject as.
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
        """Admit the AP's ACK control frames (RXFLTMAP1 bit13) so the RX tap can see the ACKs to
        our injects. mac_init_for_rx leaves bit13 clear (RXFLTMAP1_8814A=0x0400), so this is a real
        register write, not a no-op. The base arms/disarms the tally around this."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, rx.admit_ack_frames, self.transport)

    async def _disable_rx_acks(self) -> None:
        """Restore the default monitor RX filter (clear RXFLTMAP1 bit13), matching _enable_rx_acks."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, rx.drop_ack_frames, self.transport)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
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
            def _apply(force_band: bool) -> None:
                chan.set_channel(self.transport, channel,
                                 rfe_option=self._rfe_option, force_band=force_band)
                # cck_tx_dfir touches a shared CCK reg; re-pin monitor CCK
                # sensitivity after each tune so it survives channel hops.
                rx.tune_monitor_cck_sensitivity(self.transport)
                # Reset OFDM IGI to max coverage on the new channel; the DIG
                # watchdog then re-converges from there.
                if self._dig_state is not None:
                    self._dig_state = dynamic.dig_init(self.transport)

            def _tune():
                _apply(False)
                # A 2G<->5G band change can leave the RF front-end deaf (CCA=0);
                # only a fresh switch_band re-locks it. Verify and re-tune if deaf.
                # (Recovery, not yet root-caused — see RTL8814AU.md known gaps.)
                if not band_change:
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

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Build the 40-byte MGMT tx_pkt_desc (no retry-limit field, HW global retry)
        and bulk-OUT [desc][frame] once over the HIGH lane. The base registers the frame's Addr2
        for the ACK tally (when armed) and stamps the seq via _stamp_tx_seq before calling this."""
        if not self._bulk_out_eps:
            logger.error("inject_frame: no bulk-OUT endpoints")
            return False
        try:
            desc = tx.build_tx_desc_mgmt(frame_bytes, band_is_2g=self.current_band_is_2g)
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

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        """Realtek HW assigns the 802.11 sequence number (the tx_pkt_desc sets W8 EN_HWSEQ), so
        the frame goes out unchanged."""
        return frame_bytes

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
