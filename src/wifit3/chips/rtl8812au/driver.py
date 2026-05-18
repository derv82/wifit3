"""RTL8812AU driver — full bring-up through RX (M3-b end-state).

Bring-up flow:

    connect()
      -> _claim                                (set_configuration + claim ifc 0)
      -> probe_chip_state                      (M5 + M2-c warm tiers)

      COLD path:                               (M1 + M2-b + M2-d + M3-a + RX)
        -> mac_power_on                        rf_reset + pwr_seq + init_sys_cfg
        -> pre_fw_init                         set_trx_fifo + llt_init + DROP_DATA_EN
        -> en_download_firmware_legacy(True)
        -> download_firmware_legacy            poll BIT_FWDL_CHK_RPT
        -> en_download_firmware_legacy(False)
        -> download_firmware_validate_legacy   FW_READY_LEGACY = 0xC6
        -> post_fw_mac_init                    REG_CR |= MACTXEN|MACRXEN
        -> post_mac_init_phy                   5 init tables + switch_band(2G)
        -> set_channel_2g_20mhz(1)             ch1, 20 MHz, both RF paths
        -> _finish_attach                      probe EPs + clear halts + start RX

      FW_WARM path:
        -> post_fw_mac_init + post_mac_init_phy + set_channel(1) + _finish_attach

      FULLY_WARM path:
        -> _finish_attach (with bulk-IN smoke test — bail if pipe wedged)
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
    CHANNELS_5G_NON_DFS,
    channel_band_is_2g,
    set_channel_2g_20mhz,
)
from .constants import USB_PID_AWUS036ACH, USB_VID_REALTEK
from .firmware import (
    download_firmware_legacy,
    download_firmware_validate_legacy,
    en_download_firmware_legacy,
    load_firmware_blob,
)
from .fifo import set_trx_fifo_info
from .mac import (
    ChipState,
    init_queue_priority,
    init_queue_reserved_page,
    init_tx_buffer_boundary,
    mac_power_on,
    post_fw_mac_init,
    pre_fw_init,
    probe_chip_state,
)
from .phy import EfuseDefaults, post_mac_init_phy
from .rx import iter_bulk_frames, probe_endpoints
from .transport import RTL8812AUTransport
from .tx import (
    TX_DESC_QSEL_MGMT,
    build_tx_desc_mgmt,
    pick_bulk_out_ep,
    write_bulk,
)

logger = logging.getLogger(__name__)


class RTL8812AUDriver:
    """Driver for the Realtek RTL8812AU (e.g. ALFA AWUS036ACH).

    M3-b status: cold-boot + FW upload + MAC + PHY init + channel 1 tune +
    RX loop running. 5 GHz, TX inject, set_channel for arbitrary channels,
    and 40/80 MHz bandwidths are M-LATER.
    """

    SUPPORTED_IDS = [
        DeviceID(USB_VID_REALTEK, USB_PID_AWUS036ACH,
                 "Realtek RTL8812AU / ALFA AWUS036ACH"),
    ]
    # M3-b: 2.4 GHz only for now. 5 GHz band-switch + channel tune is M-LATER.
    SUPPORTED_CHANNELS = list(range(1, 14))

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RTL8812AUDriver":
        return cls(dev)

    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self.transport = RTL8812AUTransport(dev)
        self._claimed = False
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._rx_task: Optional[asyncio.Task] = None
        self._rx_running = False
        self._bulk_in_ep: Optional[int] = None
        self._bulk_out_eps: list[int] = []
        self._efuse = EfuseDefaults()

        # WlanDriver Protocol surface area.
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self.current_channel: int = 1
        self.current_band_is_2g: bool = True

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_callback = cb

    # ---- USB claim helpers ------------------------------------------------
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

    def _reset_bulk_pipes(self) -> None:
        """Clear halts on bulk-IN + bulk-OUT pipes and drain stale RX bytes.

        Best-effort. Useful after a warm reattach where the host stack
        may still consider the pipes halted from a previous session.
        """
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

            _progress(0.05, "Probing chip state")
            state = await loop.run_in_executor(
                None, probe_chip_state, self.transport
            )
            logger.info("RTL8812AU state: %s", state.value)

            if state is ChipState.FULLY_WARM:
                _progress(0.50, "Warm reattach (FW + MAC + PHY)")
                return await self._finish_attach(_progress, from_warm=True)

            if state is ChipState.FW_WARM:
                _progress(0.30, "Warm FW — running post-FW MAC init")
                fifo = set_trx_fifo_info()
                await loop.run_in_executor(
                    None, post_fw_mac_init, self.transport, fifo
                )
                _progress(0.60, "Running post-MAC PHY init")
                await loop.run_in_executor(
                    None, post_mac_init_phy, self.transport, self._efuse
                )
                _progress(0.80, "Tuning to channel 1")
                await loop.run_in_executor(
                    None, set_channel_2g_20mhz, self.transport, 1
                )
                self.current_channel = 1
                return await self._finish_attach(_progress, from_warm=False)

            return await self._cold_bring_up(_progress)

        except (IOError, usb.core.USBError, NotImplementedError) as e:
            logger.error("RTL8812AU connect failed: %s", e)
            return False

    async def _cold_bring_up(self, _progress) -> bool:
        loop = asyncio.get_event_loop()

        _progress(0.10, "MAC power-on (cold)")
        await loop.run_in_executor(None, mac_power_on, self.transport)

        _progress(0.20, "Pre-FW init (LLT + DROP_DATA_EN)")
        fifo = await loop.run_in_executor(None, pre_fw_init, self.transport)

        _progress(0.30, "Enable FW download")
        await loop.run_in_executor(
            None, en_download_firmware_legacy, self.transport, True
        )

        _progress(0.40, "Uploading firmware")
        fw = await loop.run_in_executor(None, load_firmware_blob)
        ack = await loop.run_in_executor(
            None,
            lambda: download_firmware_legacy(self.transport, fw, None, False),
        )
        if not ack:
            logger.error("RTL8812AU: BIT_FWDL_CHK_RPT never set — upload failed.")
            return False

        _progress(0.60, "Disable FW download")
        await loop.run_in_executor(
            None, en_download_firmware_legacy, self.transport, False
        )

        _progress(0.65, "Validating FW (FW_READY_LEGACY)")
        ok_run, last = await loop.run_in_executor(
            None, download_firmware_validate_legacy, self.transport
        )
        if not ok_run:
            logger.error("FW_READY_LEGACY not satisfied (REG_MCUFW_CTRL=0x%08x)", last)
            return False

        _progress(0.75, "Post-FW MAC init")
        await loop.run_in_executor(None, post_fw_mac_init, self.transport, fifo)

        _progress(0.85, "PHY init (5 tables + switch_band 2G)")
        await loop.run_in_executor(None, post_mac_init_phy, self.transport, self._efuse)

        _progress(0.92, "Tuning to channel 1")
        await loop.run_in_executor(None, set_channel_2g_20mhz, self.transport, 1)
        self.current_channel = 1

        return await self._finish_attach(_progress, from_warm=False)

    async def _finish_attach(self, _progress, *, from_warm: bool) -> bool:
        """Common tail: probe endpoints, clear halts, start RX loop."""
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
                "RTL8812AU: warm reattach succeeded but bulk-IN is wedged "
                "(no frames in 1500ms). Please unplug + replug the dongle "
                "and try again."
            )
            return False

        self._rx_running = True
        self._rx_task = asyncio.create_task(self._rx_loop())
        self.is_warm = True
        _progress(1.00, "RTL8812AU online (RX live on ch1)")
        return True

    async def _rx_smoke_test(self, attempts: int = 15, timeout_ms: int = 100) -> bool:
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

    # ---- set_channel ------------------------------------------------------
    async def set_channel(self, channel: int) -> bool:
        if not channel_band_is_2g(channel):
            logger.warning(
                "RTL8812AU: 5 GHz channel %d not yet supported (M-LATER)",
                channel,
            )
            return False
        if not (1 <= channel <= 13):
            logger.warning("RTL8812AU: invalid 2.4 GHz channel %d", channel)
            return False
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, set_channel_2g_20mhz, self.transport, channel
            )
            self.current_channel = channel
            return True
        except (IOError, usb.core.USBError, ValueError) as e:
            logger.error("set_channel(%d) failed: %s", channel, e)
            return False

    def _arm_tx_queues(self) -> None:
        """Re-program REG_RQPN / REG_RQPN_NPQ / REG_TXDMA_PQ_MAP.

        On 8812au, these get silently cleared somewhere between
        ``post_fw_mac_init`` (which writes them) and a subsequent TX
        attempt. Empirically, every TX bulk-OUT after a full bring-up
        times out unless we re-arm the queue mapping first. Re-arming is
        idempotent and cheap (~3 control writes), so we just do it
        before every inject burst.

        Root cause not yet pinned down — most likely a side-effect of
        the 2T2R rf_b table load or some other 8812a-specific phy init
        that 8821au doesn't do. TODO for M-LATER: bisect which phy step
        clears the queue state, fix at source.
        """
        fifo = set_trx_fifo_info()
        init_queue_reserved_page(self.transport, fifo)
        init_tx_buffer_boundary(self.transport, fifo)
        init_queue_priority(self.transport)

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        if not self._bulk_out_eps:
            logger.error("inject_frame: no bulk-OUT endpoints (driver not connected?)")
            return False
        try:
            desc = build_tx_desc_mgmt(frame_bytes, band_is_2g=self.current_band_is_2g)
        except ValueError as e:
            logger.error("inject_frame: bad MPDU: %s", e)
            return False
        ep = pick_bulk_out_ep(self._bulk_out_eps, queue=TX_DESC_QSEL_MGMT)
        payload = desc + frame_bytes
        loop = asyncio.get_event_loop()
        # 8812au quirk: re-arm queue mapping right before TX (see _arm_tx_queues).
        await loop.run_in_executor(None, self._arm_tx_queues)
        try:
            sent = await loop.run_in_executor(
                None, lambda: write_bulk(self.dev, ep, payload, timeout_ms=200)
            )
        except usb.core.USBError as e:
            logger.error("inject_frame: bulk-OUT to 0x%02x failed: %s", ep, e)
            return False
        if sent != len(payload):
            logger.warning("inject_frame: short write %d/%d to 0x%02x", sent, len(payload), ep)
            return False
        return True

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
