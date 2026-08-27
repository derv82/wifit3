"""RTL8188EUS driver — TP-Link TL-WN722N v2/v3 (Realtek RTL8188EUS).

Cleanroom port of the kernel `rtl8xxxu` driver's 8188e fileops vector
(`driver_sources/rtl8xxxu-source-v6.18/8188e.c:1835-1885`).

M1-M7 scope (complete bring-up):

    connect()
      ├─ claim USB interface
      ├─ probe chip state (mac.is_chip_warm)
      ├─ COLD path:
      │   ├─ power_on, FW upload, MAC + PHY init (M1-M3)
      │   ├─ enable_rx_data_path + enable_cck_ofdm_block
      │   ├─ set_channel(1)
      │   └─ enable_rf
      └─ WARM path: skip everything above (chip is FW-running + MAC-enabled
                    from a previous wifit3 session); just resume

      then (both paths) → _finish_attach:
        ├─ probe USB endpoints
        ├─ reset bulk pipes (clear_halt + drain stale bytes)
        ├─ on warm: bulk-IN smoke test (1.5s) — surface "please replug"
        │   message if pipe is wedged from a wedged previous session
        └─ spawn _rx_loop asyncio task

`inject_frame(deauth_bytes)` sends a MGMT frame on the bulk-OUT (`tx.py`).
`close()` stops the RX loop + releases USB. No IQK/LC calibration,
no data-frame TX; those land in optional polish milestones.
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
from wifit3.dot11.parser import WlanFrameParser

from .chan import set_channel_2g_20mhz
from .constants import (
    APS_FSMCO_HW_POWERDOWN,
    APS_FSMCO_HW_SUSPEND,
    APS_FSMCO_MAC_ENABLE,
    APS_FSMCO_PCIE,
    APS_FSMCO_POWER_READY,
    CR_INIT_POWER_ON,
    REG_AFE_XTAL_CTRL,
    REG_APS_FSMCO,
    REG_CR,
    REG_LPLDO_CTRL,
    REG_SYS_CFG,
    REG_SYS_FUNC,
    RTL8XXXU_MAX_REG_POLL,
    SYS_FUNC_BB_GLB_RSTN,
    SYS_FUNC_BBRSTB,
)
from .efuse import EfuseDefaults, read_and_parse
from .firmware import download_firmware, load_firmware_blob, start_firmware
from .iqk import phy_iq_calibrate, phy_lc_calibrate
from .mac import (
    admit_ack_frames,
    apply_monitor_rx_filter,
    drop_ack_frames,
    enable_rx_data_path,
    is_chip_warm,
    post_fw_mac_init,
)
from .phy import enable_cck_ofdm_block, enable_rf, post_mac_init_phy, set_tx_power
from .rx import iter_bulk_frames, probe_endpoints
from ..rx_reader import RxReaderThread
from .transport import RTL8188EUSTransport
from .tx import pick_bulk_out_mgmt, send_mgmt_frame

logger = logging.getLogger(__name__)


class RTL8188EUSDriver(Driver):
    """Driver for the Realtek RTL8188EUS (e.g. TP-Link TL-WN722N v2/v3)."""

    # 2.4 GHz only.
    SUPPORTED_CHANNELS = list(range(1, 15))
    FAKE_MAC = FakeMacSupport.UNIMPLEMENTED   # active-monitor not ported for this variant

    @classmethod
    def from_usb_device(
        cls, dev: usb.core.Device, id_entry: DeviceID
    ) -> "RTL8188EUSDriver":
        return cls(dev)

    def __init__(self, dev: usb.core.Device):
        super().__init__()          # base owns the ACK tally (_ack_detect_on / _our_tx_macs / _ack_counts)
        self.dev = dev
        self.transport = RTL8188EUSTransport(dev)
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._mgmt_bulk_out: Optional[int] = None
        self._bulk_in_ep: Optional[int] = None
        self._bulk_out_eps: list[int] = []
        self._rx_reader: Optional[RxReaderThread] = None
        self._claimed: bool = False
        self.current_channel: int = 1
        self._efuse: EfuseDefaults = EfuseDefaults()  # fallback defaults until cold path parses

    # ---- Driver Protocol surface ------------------------------------

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_callback = cb

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        """Sink for a terminal RX-reader failure (unplug). Forwarded to the RxReaderThread's
        on_fatal; resolved at call time so registration order vs connect() can't strand it."""
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
            warm = await loop.run_in_executor(None, is_chip_warm, self.transport)

            if warm:
                logger.info("RTL8188EUS is WARM, reattaching to running session")
                return await self._warm_reattach(_update)

            logger.info("RTL8188EUS is COLD, running full bring-up")
            return await self._cold_bring_up(_update)
        except Exception as e:
            raise BringUpError("bring-up", str(e)) from e

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, set_channel_2g_20mhz, self.transport, channel)
            # Per-channel-group TX power re-application — required because
            # the 5 channel groups use different power curves (8188f.c:338).
            await loop.run_in_executor(None, set_tx_power, self.transport, channel, self._efuse)
            self.current_channel = channel
            return True
        except (ValueError, IOError):
            logger.exception("set_channel(%d) failed", channel)
            return False

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Build the MGMT TX descriptor (HW ACK-retry limit 6, the vendor MGMT value) and
        send ``[desc | frame]`` once on the bulk-OUT pipe (currently MGMT-only). BMC is derived
        from addr1's group bit. The retry-limit field in the TX descriptor controls HW
        retransmission."""
        loop = asyncio.get_running_loop()
        if self._mgmt_bulk_out is None:
            eps = await loop.run_in_executor(None, probe_endpoints, self.dev)
            self._mgmt_bulk_out = pick_bulk_out_mgmt(eps.bulk_out)

        # Determine bcast/mcast from addr1 (frame_bytes[4:10]).
        is_bcast = False
        if len(frame_bytes) >= 10:
            addr1 = frame_bytes[4:10]
            is_bcast = (addr1[0] & 0x01) != 0  # I/G bit

        try:
            await loop.run_in_executor(
                None,
                lambda: send_mgmt_frame(
                    self.dev, self._mgmt_bulk_out, frame_bytes,
                    is_broadcast=is_bcast,
                ),
            )
        except (IOError, usb.core.USBError):
            logger.exception("inject_frame failed")
            return False
        return True

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        """Realtek HW assigns the 802.11 sequence number (fill_txdesc_v3's seq counter takes
        over), so the frame goes out unchanged."""
        return frame_bytes

    async def close(self) -> None:
        """Stop the RX reader + release USB. Idempotent."""
        if self._rx_reader is not None:
            await self._rx_reader.stop()
            self._rx_reader = None
        self._release_usb()

    # ---- bring-up paths -------------------------------------------------

    async def _cold_bring_up(self, _update) -> bool:
        loop = asyncio.get_running_loop()

        _update(0.10, "Reading chip ID...")
        sys_cfg = await loop.run_in_executor(None, self.transport.read32, REG_SYS_CFG)
        logger.debug("REG_SYS_CFG = 0x%08x", sys_cfg)

        _update(0.15, "Power on (disabled -> emu -> active)...")
        await loop.run_in_executor(None, self._power_on)

        _update(0.25, "Loading firmware blob...")
        fw_blob = load_firmware_blob()

        _update(0.35, f"Uploading firmware ({len(fw_blob)} B)...")
        await loop.run_in_executor(None, download_firmware, self.transport, fw_blob)

        _update(0.50, "Polling for MCU_WINT_INIT_READY...")
        await loop.run_in_executor(None, start_firmware, self.transport)

        _update(0.55, "Reading EFUSE (MAC + per-channel TX power)...")
        try:
            self._efuse = await loop.run_in_executor(None, read_and_parse, self.transport)
            if self._efuse.mac_address:
                self.mac_address = self._efuse.mac_address.hex(":")
        except (IOError, OSError) as e:
            logger.warning("EFUSE read failed (%s); using fallback defaults", e)

        _update(0.60, "Post-FW MAC init (mactable + LLT + MAC_TX/RX enable)...")
        await loop.run_in_executor(None, post_fw_mac_init, self.transport)

        _update(0.75, "PHY init (BB + AGC + RF path A tables)...")
        await loop.run_in_executor(None, post_mac_init_phy, self.transport, self._efuse)

        _update(0.85, "Enable RX data path (RCR + DRVINFO_SZ + interrupts)...")
        await loop.run_in_executor(None, enable_rx_data_path, self.transport)

        _update(0.88, "Enable CCK + OFDM baseband blocks...")
        await loop.run_in_executor(None, enable_cck_ofdm_block, self.transport)

        _update(0.91, "Tune to channel 1 @ 20 MHz + set TX power...")
        await loop.run_in_executor(None, set_channel_2g_20mhz, self.transport, 1)
        await loop.run_in_executor(None, set_tx_power, self.transport, 1, self._efuse)
        self.current_channel = 1

        _update(0.92, "LC calibration (VCO tank)...")
        await loop.run_in_executor(None, phy_lc_calibrate, self.transport)

        _update(0.93, "IQ calibration (path A LOK + TX/RX IQK)...")
        await loop.run_in_executor(None, phy_iq_calibrate, self.transport)

        _update(0.94, "Enable RF (OFDM RX/TX path A)...")
        await loop.run_in_executor(None, enable_rf, self.transport)

        return await self._finish_attach(_update, from_warm=False)

    async def _warm_reattach(self, _update) -> bool:
        """Reattach to a chip already running FW + MAC-enabled.

        Skip the FW upload + MAC/PHY init + channel set + enable_rf
        chain — the chip is already in that state from a previous session.
        Just resume USB polling. If the bulk-IN pipe is wedged from the
        previous session we surface a "please replug" message in
        :meth:`_finish_attach`'s smoke test.
        """
        _update(0.50, "Warm chip — skipping FW + MAC/PHY init")
        return await self._finish_attach(_update, from_warm=True)

    async def _finish_attach(self, _update, *, from_warm: bool) -> bool:
        """Common tail: probe endpoints, clear halts, start RX loop."""
        loop = asyncio.get_running_loop()

        _update(0.96, "Probing USB endpoints + clearing halts...")
        eps = await loop.run_in_executor(None, probe_endpoints, self.dev)
        if not eps.bulk_in:
            logger.error("no bulk-IN endpoint discovered")
            return False
        self._bulk_in_ep = eps.primary_bulk_in
        self._bulk_out_eps = list(eps.bulk_out)
        if eps.bulk_out:
            self._mgmt_bulk_out = pick_bulk_out_mgmt(eps.bulk_out)

        await loop.run_in_executor(None, self._reset_bulk_pipes)

        if from_warm:
            _update(0.97, "Smoke-testing bulk-IN (warm reattach)...")
            if not await self._rx_smoke_test():
                logger.error(
                    "RTL8188EUS: warm reattach succeeded but bulk-IN is wedged "
                    "(no frames in 1500ms). Please unplug + replug the dongle "
                    "and try again — the USB pipe state from the previous "
                    "session can't be reset in userland on Windows/WinUSB."
                )
                return False

        # Force the monitor RCR on BOTH paths — the warm path skips
        # enable_rx_data_path, leaving a non-promiscuous RCR that drops
        # client→AP (ToDS) frames. Mirrors rtl8821au/rtl8822bu.
        await loop.run_in_executor(None, apply_monitor_rx_filter, self.transport)

        _update(0.99, "Starting RX reader...")
        self._rx_reader = RxReaderThread(
            loop, self._rx_read_once, self._rx_dispatch, name="rtl8188eus-rx",
            on_fatal=lambda e: self._on_lost and self._on_lost(e)
        )
        self._rx_reader.start()
        self.is_warm = True
        _update(1.00, "RTL8188EUS online.")
        return True

    # ---- USB-state helpers ----------------------------------------------

    def _claim_usb(self) -> None:
        """Set default configuration + claim interface 0."""
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
            # Already-configured is fine on warm reattach; permission errors are not.
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

    def _reset_bulk_pipes(self) -> None:
        """Clear halts on bulk pipes + drain stale bulk-IN bytes.

        Important on warm reattach (host stack may consider pipes halted
        from the previous session), harmless on cold.
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

    async def _rx_smoke_test(self, attempts: int = 15, timeout_ms: int = 100) -> bool:
        """Single bulk-IN read with generous timeout; True if any byte arrived.

        Channel 1 on a busy 2.4 GHz band delivers a beacon ~every 100ms so
        1.5s of attempts is plenty.
        """
        loop = asyncio.get_running_loop()

        def _try_read():
            try:
                return bytes(self.dev.read(self._bulk_in_ep, 16384, timeout_ms))
            except usb.core.USBError:
                return b""

        for _ in range(attempts):
            data = await loop.run_in_executor(None, _try_read)
            if data:
                logger.debug("RX smoke test: got %d bytes - pipe alive", len(data))
                return True
        return False

    # ---- RX loop --------------------------------------------------------

    # ---- RX callables for the shared RxReaderThread ---------------------
    # read_once runs on the reader thread; dispatch runs on the event loop.

    def _rx_read_once(self) -> Optional[bytes]:
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
        for _desc, mpdu, rssi in iter_bulk_frames(buf):
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
        """Register write: admit ACK control frames (RXFLTMAP1 bit13) so the tap can see the
        AP's ACKs to us. The 8188e fileops leaves RXFLTMAP at the HW default (ACKs filtered), so
        this bit must be opened explicitly (the base arms the tally). Not enter_active_monitor,
        which makes the chip EMIT ACKs."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, admit_ack_frames, self.transport)

    async def _disable_rx_acks(self) -> None:
        """Restore the default monitor RX filter (clear RXFLTMAP1 bit13)."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, drop_ack_frames, self.transport)

    # ---- power-on internals --------------------------------------------

    def _power_on(self) -> None:
        """Port of `rtl8188eu_power_on` (8188e.c:1165-1193)."""
        self._disabled_to_emu()
        self._emu_to_active()
        # Enable DMA/protocol/sched/sec/caltimer. MAC_TX/MAC_RX intentionally
        # held off due to the 88E TRXFF_BNDY HW bug (8188e.c:1176-1183) —
        # those flip on after REG_TRXFF_BNDY is set in post_fw_mac_init.
        self.transport.write16(REG_CR, CR_INIT_POWER_ON)

    def _disabled_to_emu(self) -> None:
        """Port of `rtl8188e_disabled_to_emu` (8188e.c:993-1000)."""
        val16 = self.transport.read16(REG_APS_FSMCO)
        val16 &= ~(APS_FSMCO_HW_SUSPEND | APS_FSMCO_PCIE)
        self.transport.write16(REG_APS_FSMCO, val16)

    def _emu_to_active(self) -> None:
        """Port of `rtl8188e_emu_to_active` (8188e.c:1002-1069)."""
        t = self.transport

        # Wait till 0x04[17] = 1 power ready.
        for _ in range(RTL8XXXU_MAX_REG_POLL):
            if t.read32(REG_APS_FSMCO) & APS_FSMCO_POWER_READY:
                break
            time.sleep(0.00001)
        else:
            raise IOError("emu_to_active: power-ready bit (APS_FSMCO[17]) never set")

        # Reset baseband.
        val8 = t.read8(REG_SYS_FUNC)
        val8 &= ~(SYS_FUNC_BBRSTB | SYS_FUNC_BB_GLB_RSTN) & 0xFF
        t.write8(REG_SYS_FUNC, val8)

        # 0x24[23] = 1: schmitt-trigger enable.
        val32 = t.read32(REG_AFE_XTAL_CTRL)
        val32 |= 1 << 23
        t.write32(REG_AFE_XTAL_CTRL, val32)

        # 0x04[15] = 0: disable HWPDN (driver-controlled).
        val16 = t.read16(REG_APS_FSMCO)
        val16 &= ~APS_FSMCO_HW_POWERDOWN
        t.write16(REG_APS_FSMCO, val16)

        # 0x04[12:11] = 0: disable WL suspend.
        val16 = t.read16(REG_APS_FSMCO)
        val16 &= ~(APS_FSMCO_HW_SUSPEND | APS_FSMCO_PCIE)
        t.write16(REG_APS_FSMCO, val16)

        # Set MAC_ENABLE, then poll until it self-clears.
        val32 = t.read32(REG_APS_FSMCO)
        val32 |= APS_FSMCO_MAC_ENABLE
        t.write32(REG_APS_FSMCO, val32)
        for _ in range(RTL8XXXU_MAX_REG_POLL):
            if (t.read32(REG_APS_FSMCO) & APS_FSMCO_MAC_ENABLE) == 0:
                break
            time.sleep(0.00001)
        else:
            raise IOError("emu_to_active: MAC_ENABLE bit never cleared")

        # LDO normal mode (REG_LPLDO_CTRL bit 4 = 0).
        val8 = t.read8(REG_LPLDO_CTRL)
        val8 &= ~(1 << 4) & 0xFF
        t.write8(REG_LPLDO_CTRL, val8)
