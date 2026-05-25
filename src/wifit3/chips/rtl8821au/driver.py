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
import threading
import time
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
from .firmware import (
    download_firmware_legacy,
    download_firmware_validate_legacy,
    en_download_firmware_legacy,
    load_firmware_blob,
)
from .mac import is_chip_warm, mac_power_on, post_fw_mac_init, pre_fw_init
from .phy import (
    EfuseDefaults,
    post_mac_init_phy,
    switch_band_2g_20mhz,
    switch_band_5g_20mhz,
)
from .rx import iter_bulk_frames, probe_endpoints
from .transport import RTL8821AUTransport
from .tx import (
    TX_DESC_QSEL_MGMT,
    build_tx_desc_data,
    build_tx_desc_mgmt,
    pick_bulk_out_ep,
    write_bulk,
)

logger = logging.getLogger(__name__)


class RTL8821AUDriver:
    """Driver for the Realtek RTL8821AU (e.g. ALFA AWUS036ACS).

    Single-chain RX (synchronous bulk reads polled in a worker thread).
    TX injection is not yet implemented (M7).
    """

    SUPPORTED_IDS = [
        DeviceID(0x0BDA, 0x0811, "Realtek RTL8821AU / ALFA AWUS036ACS"),
    ]
    # 2.4 GHz channels 1..13 + non-DFS 5 GHz UNII-1 (36..48) + UNII-3
    # (149..165). DFS channels are excluded by default to avoid the
    # regulator-required clearance dance.
    SUPPORTED_CHANNELS = list(range(1, 14)) + list(CHANNELS_5G_NON_DFS)
    # inject_frame accepts sw_seq → can emit a fragment train sharing one
    # 802.11 sequence number (WEP fragmentation). See tx.build_tx_desc_data.
    SUPPORTS_SW_SEQ = True

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RTL8821AUDriver":
        return cls(dev)

    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self.transport = RTL8821AUTransport(dev)
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._rx_task: Optional[asyncio.Task] = None
        self._rx_thread: Optional[threading.Thread] = None
        self._rx_running = False
        # Buffers handed to the loop but not yet parsed. Advisory soft cap so a
        # momentarily-swamped loop drops here rather than ballooning memory;
        # ±1 races between the two threads are harmless for a cap this coarse.
        self._rx_pending = 0
        self._bulk_in_ep: Optional[int] = None
        self._bulk_out_eps: list[int] = []
        self._claimed = False
        self._efuse = EfuseDefaults()

        # WlanDriver Protocol surface area.
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self.current_channel: int = 1
        self.current_band_is_2g: bool = True

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

    def _reset_bulk_pipes(self) -> None:
        """Clear halts on bulk-IN + bulk-OUT pipes so warm restarts resume RX.

        After a warm reattach the chip's MAC state is intact but the USB
        host controller may still consider the pipes halted from the
        previous session, AND the chip's internal RX FIFO can be wedged
        from frames that arrived after the prior session stopped polling.

        We do two things:
          1. `dev.clear_halt(ep)` per endpoint — host-side data toggle reset.
          2. Drain whatever bulk-IN bytes the host has buffered with a few
             short reads so subsequent reads get fresh data.

        Failures here are non-fatal.
        """
        eps = [self._bulk_in_ep] if self._bulk_in_ep is not None else []
        eps += self._bulk_out_eps
        for ep in eps:
            try:
                self.dev.clear_halt(ep)
                logger.debug("cleared halt on endpoint 0x%02x", ep)
            except (usb.core.USBError, NotImplementedError) as e:
                logger.debug("clear_halt(0x%02x) skipped: %s", ep, e)

        # Drain stale bulk-IN bytes (best-effort, short timeouts).
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

            # Warm-state probe: if a prior session left FW running + MAC
            # enabled, we just reattach — no FW upload, no MAC/PHY init,
            # no channel re-tune. Mirrors AR9271/RTL8187's `is_warm` path.
            _progress(0.05, "Probing chip state")
            warm = await loop.run_in_executor(None, is_chip_warm, self.transport)
            if warm:
                logger.info("RTL8821AU is WARM — reattaching to running session")
                return await self._warm_reattach(_progress)

            logger.info("RTL8821AU is COLD — running full bring-up")
            return await self._cold_bring_up(_progress)

        except (IOError, usb.core.USBError, NotImplementedError) as e:
            logger.error("RTL8821AU connect failed: %s", e)
            return False

    async def _cold_bring_up(self, _progress) -> bool:
        loop = asyncio.get_event_loop()

        _progress(0.10, "MAC power-on (cold)")
        await loop.run_in_executor(None, mac_power_on, self.transport)

        _progress(0.15, "Pre-FW init (LLT + DROP_DATA_EN)")
        fifo = await loop.run_in_executor(None, pre_fw_init, self.transport)

        _progress(0.25, "Enable FW download")
        await loop.run_in_executor(
            None, en_download_firmware_legacy, self.transport, True
        )

        _progress(0.35, "Uploading firmware")
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

        return await self._finish_attach(_progress, from_warm=False)

    async def _warm_reattach(self, _progress) -> bool:
        """Reattach to a running chip: just resume USB polling.

        We can't reliably re-sync the USB bulk-IN pipe in userland on
        Windows/WinUSB after a previous session closed. The kernel does
        it via URB resubmission and an unload/reload that we can't fully
        replicate. If the pipe is wedged, RX will be silent; we detect
        that in :meth:`_rx_smoke_test` and surface an actionable error
        pointing the user at a replug.
        """
        _progress(0.50, "Warm chip — skipping FW + init")
        return await self._finish_attach(_progress, from_warm=True)

    async def _finish_attach(self, _progress, *, from_warm: bool) -> bool:
        """Common tail: probe endpoints, clear halts, start RX loop."""
        loop = asyncio.get_event_loop()
        eps = probe_endpoints(self.dev)
        if not eps.bulk_in:
            logger.error("no bulk-IN endpoint discovered")
            return False
        self._bulk_in_ep = eps.primary_bulk_in
        self._bulk_out_eps = list(eps.bulk_out)

        # Clear any stale halts on the bulk pipes — important after a
        # warm reattach (host stack may still consider them halted from
        # the previous session), harmless on cold.
        await loop.run_in_executor(None, self._reset_bulk_pipes)

        # On the warm path, verify that bulk-IN is actually delivering
        # before we hand back. If it's wedged, surface a clear replug
        # message instead of silently failing later in the user's flow.
        if from_warm and not await self._rx_smoke_test():
            logger.error(
                "RTL8821AU: warm reattach succeeded but bulk-IN is wedged "
                "(no frames in 1500ms). Please unplug + replug the dongle "
                "and try again — the USB pipe state from the previous "
                "session can't be reset in userland on Windows/WinUSB."
            )
            return False

        self._rx_running = True
        self._rx_task = asyncio.create_task(self._rx_loop())
        self.is_warm = True
        _progress(1.00, "RTL8821AU online")
        return True

    async def _rx_smoke_test(self, attempts: int = 15, timeout_ms: int = 100) -> bool:
        """Single bulk-IN read with a generous timeout; return True if any
        byte arrived. Channel 1 on a busy 2.4 GHz environment delivers a
        beacon every ~100 ms so 1.5 s is plenty of margin."""
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
        is_2g = channel_band_is_2g(channel)
        if is_2g and not (1 <= channel <= 14):
            logger.warning("RTL8821AU: invalid 2.4 GHz channel %d", channel)
            return False
        if not is_2g and channel not in CHANNELS_5G_ALL:
            logger.warning("RTL8821AU: unsupported 5 GHz channel %d", channel)
            return False

        loop = asyncio.get_event_loop()
        try:
            # Band switch only when crossing 2G↔5G.
            if is_2g != self.current_band_is_2g:
                if is_2g:
                    await loop.run_in_executor(
                        None, switch_band_2g_20mhz, self.transport, self._efuse
                    )
                else:
                    await loop.run_in_executor(
                        None, switch_band_5g_20mhz, self.transport, self._efuse
                    )
                self.current_band_is_2g = is_2g

            tune = set_channel_2g_20mhz if is_2g else set_channel_5g_20mhz
            await loop.run_in_executor(None, tune, self.transport, channel)
            self.current_channel = channel
            return True
        except (IOError, usb.core.USBError, ValueError) as e:
            logger.error("set_channel(%d) failed: %s", channel, e)
            return False

    # ---- inject_frame (MGMT queue, bulk-OUT) ------------------------------
    async def inject_frame(
        self, frame_bytes: bytes, use_no_ack: bool = True,
        sw_seq: Optional[int] = None,
    ) -> bool:
        """Inject a raw 802.11 frame. ``sw_seq`` (when given) switches to the
        software-sequence descriptor so a fragment train shares one sequence
        number — required for AP reassembly (WEP fragmentation). Without it,
        the hardware assigns the seq (correct for mgmt + single-frame data)."""
        if not self._bulk_out_eps:
            logger.error("inject_frame: no bulk-OUT endpoints (driver not connected?)")
            return False
        try:
            if sw_seq is None:
                desc = build_tx_desc_mgmt(frame_bytes, band_is_2g=True)
            else:
                desc = build_tx_desc_data(frame_bytes, sw_seq, band_is_2g=True)
        except ValueError as e:
            logger.error("inject_frame: bad MPDU: %s", e)
            return False
        ep = pick_bulk_out_ep(self._bulk_out_eps, queue=TX_DESC_QSEL_MGMT)
        payload = desc + frame_bytes
        loop = asyncio.get_event_loop()
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
    # Soft cap on un-parsed buffers in flight to the loop (see _rx_pending).
    _RX_PENDING_CAP = 256

    def _read_burst(self, ep: int) -> bytes | None:
        """One blocking bulk-IN read. None on a benign timeout (no traffic);
        re-raises a real USBError for the reader thread to count."""
        try:
            return bytes(self.dev.read(ep, 16384, 100))
        except usb.core.USBError as e:
            err = getattr(e, "errno", None)
            if err in (110, 10060) or "timeout" in str(e).lower():
                return None
            raise

    def _rx_reader_thread(self, loop: asyncio.AbstractEventLoop, ep: int) -> None:
        """Dedicated thread: a tight bulk-read loop that keeps a URB posted at
        all times, independent of event-loop / UI load.

        The previous design read + parsed on the event loop, so while the UI
        was busy no ``dev.read`` was posted and the dongle's RX FIFO overflowed
        — dropping ~30% of beacons and most fast 4-way frames. Here the read
        loop never waits on the loop: it hands each raw buffer over via
        ``call_soon_threadsafe`` and immediately reads again. Parsing + the rx
        callback still run on the loop thread (``_dispatch_buffer``) so they
        never race the UI's reads of the AP registry.
        """
        consec_errors = 0
        while self._rx_running:
            try:
                buf = self._read_burst(ep)
            except usb.core.USBError as e:
                consec_errors += 1
                logger.warning("RX read failed (%d/5): %s", consec_errors, e)
                if consec_errors >= 5:
                    logger.error("RX giving up after 5 consecutive errors")
                    break
                time.sleep(0.01)
                continue
            consec_errors = 0
            if not buf:
                continue
            # Loop swamped and not draining — drop rather than balloon memory.
            if self._rx_pending >= self._RX_PENDING_CAP:
                continue
            self._rx_pending += 1
            loop.call_soon_threadsafe(self._dispatch_buffer, buf)
        logger.info("RX reader thread stopped")

    def _dispatch_buffer(self, buf: bytes) -> None:
        """Runs on the event loop: decode the bulk buffer into MPDUs and fan
        each parsed frame to the rx callback."""
        self._rx_pending -= 1
        cb = self._rx_callback
        if not cb:
            return
        for stat, mpdu, rssi in iter_bulk_frames(buf):
            parsed = WlanFrameParser.parse_80211_frame(
                mpdu, rssi if rssi is not None else -100
            )
            if parsed:
                try:
                    cb(parsed)
                except Exception:
                    logger.exception("RX callback raised")

    async def _rx_loop(self) -> None:
        """Owns the RX reader thread. The reads happen on that thread; this
        coroutine just starts it and stays alive so ``close()`` has a task to
        await. The thread exits within its 100 ms read timeout once
        ``_rx_running`` clears; ``close()`` joins it before releasing USB."""
        loop = asyncio.get_running_loop()
        ep = self._bulk_in_ep
        logger.info("RX loop started on endpoint 0x%02x", ep)
        self._rx_thread = threading.Thread(
            target=self._rx_reader_thread, args=(loop, ep),
            name="rtl8821au-rx", daemon=True,
        )
        self._rx_thread.start()
        while self._rx_running:
            await asyncio.sleep(0.1)
        logger.info("RX loop stopped")

    # ---- close ------------------------------------------------------------
    async def close(self) -> None:
        self._rx_running = False
        loop = asyncio.get_event_loop()
        if self._rx_task:
            try:
                await asyncio.wait_for(self._rx_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._rx_task.cancel()
            self._rx_task = None
        # Join the reader thread BEFORE releasing USB — it's still calling
        # dev.read() until it sees _rx_running=False (within its 100ms timeout),
        # and releasing the handle out from under it would error.
        if self._rx_thread:
            await loop.run_in_executor(None, self._rx_thread.join, 1.5)
            self._rx_thread = None
        # Run USB release in an executor; PyUSB calls block.
        await loop.run_in_executor(None, self._release)
