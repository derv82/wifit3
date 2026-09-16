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
        ├─ probe USB endpoints, reset bulk pipes
        ├─ arm monitor RX filter
        └─ start RxReaderThread (bulk-IN pump)

Milestones M1-M4 gate against the cold-boot capture (verify_pcap.py);
M5 adds the RX acceptance + channel-1 tune region; M6 the full 53-hop
channel-scan loop, so the capture is replayed end to end.  M7 wires
``connect()`` to run the whole thing: EFUSE read (M2) + MAC/PHY init
(M3) + LC/IQ/RF tail (M4) + RX path/monitor filter/channel-1 (M5) on
the cold path, then ``_finish_attach`` on both paths to start the RX
reader and arm the monitor filter.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import Callable, Optional

import usb.core
import usb.util

from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.chips.rx_reader import RxReaderThread
from wifit3.dot11.parser import WlanFrameParser
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
        self._mac_bytes: Optional[bytes] = None   # raw 6 bytes for REG_MACID writes
        self._efuse = None
        self.is_warm: bool = False
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._mgmt_bulk_out: Optional[int] = None
        self._bulk_in_ep: Optional[int] = None
        self._bulk_out_eps: list[int] = []
        self._tx_seq: int = 0
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
        from .phy import set_tx_power
        loop = asyncio.get_running_loop()
        try:
            if self._efuse is not None:
                await loop.run_in_executor(None, set_tx_power, self.transport, channel, self._efuse)
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
        """Supply the incrementing SW sequence number the injected txdesc40 copies:
        the driver keeps HW_SEQ_ENABLE cleared for MACd for injects (core.c:5387),
        so nothing else advances the seq. HW retransmits reuse the descriptor
        (same seq); each new inject gets the next."""
        if len(frame_bytes) < 24:
            return frame_bytes
        self._tx_seq = (self._tx_seq + 1) & 0xFFF
        buf = bytearray(frame_bytes)
        struct.pack_into("<H", buf, 22, (self._tx_seq << 4) | (buf[22] & 0x0F))
        return bytes(buf)

    # ---- active monitor (HW-ACK a chosen MAC) -----------------------------

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Point REG_MACID at ``mac`` so the hardware HW-ACKs frames addressed to
        it while staying in monitor mode — the prerequisite for ACKed conversations
        (WPS/EAP/PMKID). Reversed by exit_active_monitor."""
        await self._set_self_mac(bytes(mac))
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the card's real EFUSE MAC in REG_MACID (stop ACKing the forged
        MAC); no-op when the real MAC was never cached (warm reattach)."""
        if self._mac_bytes:
            await self._set_self_mac(self._mac_bytes)

    async def _set_self_mac(self, mac_bytes: bytes) -> None:
        from .mac import set_macid
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, set_macid, self.transport, mac_bytes)

    # ---- RX-ACK detection (Driver._enable_rx_acks) ------------------

    async def _enable_rx_acks(self) -> None:
        from .rx import admit_ack_frames
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, admit_ack_frames, self.transport)

    async def _disable_rx_acks(self) -> None:
        from .rx import drop_ack_frames
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, drop_ack_frames, self.transport)

    # ---- RX read loop (RxReaderThread driver) ------------------------

    def _rx_read_once(self) -> Optional[bytes]:
        from .rx import read_rx_burst
        if self._bulk_in_ep is None:
            return None
        return read_rx_burst(self.dev, self._bulk_in_ep, max_size=16384, timeout_ms=100)

    def _rx_dispatch(self, buf: bytes) -> None:
        from .rx import iter_bulk_frames
        callback = self._rx_callback
        if callback is None and not self._ack_detect_on:
            return
        for _desc, mpdu, rssi in iter_bulk_frames(buf):
            if len(mpdu) == 10 and mpdu[0] == 0xD4:
                self.record_ack(mpdu)
                continue
            if callback is None:
                continue
            parsed = WlanFrameParser.parse_80211_frame(mpdu, rssi if rssi is not None else -100)
            if parsed:
                callback(parsed)

    async def close(self) -> None:
        if self._rx_reader is not None:
            await self._rx_reader.stop()
            self._rx_reader = None
        self._release_usb()

    # ---- bring-up paths ---------------------------------------------

    async def _cold_bring_up(self, _update) -> bool:
        t = self.transport
        loop = asyncio.get_running_loop()

        _update(0.10, "Reading chip ID...")
        sys_cfg = await loop.run_in_executor(None, t.read32, REG_SYS_CFG)
        self.chip_cut = (sys_cfg & SYS_CFG_CHIP_VERSION_MASK) >> 12
        logger.debug("REG_SYS_CFG = 0x%08x  chip_cut=%d", sys_cfg, self.chip_cut)
        if sys_cfg & SYS_CFG_TRP_VAUX_EN:
            raise RuntimeError("Unsupported test chip (TRP_VAUX_EN set)")

        _update(0.15, "Power on...")
        await loop.run_in_executor(None, self._power_on)

        from .mac import (
            init_queue_priority_2ep,
            init_queue_reserved_page,
            set_trxff_rx_page_boundary,
        )
        _update(0.18, "TX queue page alloc + priority routing...")
        await loop.run_in_executor(None, init_queue_reserved_page, t)
        await loop.run_in_executor(None, init_queue_priority_2ep, t)
        await loop.run_in_executor(None, set_trxff_rx_page_boundary, t)

        _update(0.25, "Loading firmware blob...")
        fw_blob = load_firmware_blob()

        _update(0.35, f"Uploading firmware ({len(fw_blob)} B)...")
        await loop.run_in_executor(None, download_firmware, t, fw_blob)

        _update(0.50, "Polling for MCU_WINT_INIT_READY...")
        await loop.run_in_executor(None, start_firmware, t)

        from .phy import init_antenna_selection
        _update(0.52, "Antenna-selection RFE/LED/GPIO init...")
        await loop.run_in_executor(None, init_antenna_selection, t)

        from .efuse import read_and_parse
        _update(0.55, "Reading EFUSE...")
        efuse = await loop.run_in_executor(None, read_and_parse, t)
        self._efuse = efuse
        if efuse.mac_address:
            self.mac_address = efuse.mac_address.hex(":")
            self._mac_bytes = bytes(efuse.mac_address)
        logger.debug("EFUSE MAC = %s", self.mac_address)

        from .mac import apply_mac_init_table, init_device_post_phy
        _update(0.60, "MAC init (init_mac + queue + usb_quirks)...")
        await loop.run_in_executor(None, apply_mac_init_table, t)

        from .phy import post_mac_init_phy
        _update(0.65, "PHY init (BB + AGC + crystal cap + RF)...")
        await loop.run_in_executor(
            None, post_mac_init_phy, t, self.chip_cut, efuse.default_crystal_cap)

        _update(0.70, "Device post-PHY config (RFSW..CCK PD)...")
        await loop.run_in_executor(None, init_device_post_phy, t, efuse)

        from .phy import lc_calibrate, iq_calibrate, enable_thermal_meter, \
            init_device_phy_tail, enable_rf
        _update(0.74, "LC calibration...")
        await loop.run_in_executor(None, lc_calibrate, t)
        _update(0.78, "IQ calibration...")
        await loop.run_in_executor(None, iq_calibrate, t)
        _update(0.80, "Thermal meter + PHY tail...")
        await loop.run_in_executor(None, enable_thermal_meter, t)
        await loop.run_in_executor(None, init_device_phy_tail, t)
        _update(0.84, "Reading TX power from EFUSE...")
        await loop.run_in_executor(None, enable_rf, t)

        from .mac import enable_rx_path
        _update(0.86, "RX data path + channel-1 tune...")
        await loop.run_in_executor(None, enable_rx_path, t)

        from .phy import set_tx_power
        _update(0.90, "TX power regs (ch1, EFUSE)...")
        await loop.run_in_executor(None, set_tx_power, t, 1, efuse)

        from .chan import set_channel_2g_20mhz
        _update(0.94, "Tuning to channel 1...")
        await loop.run_in_executor(None, set_channel_2g_20mhz, t, 1)
        self.current_channel = 1

        return await self._finish_attach(_update, from_warm=False)

    async def _warm_reattach(self, _update) -> bool:
        """Reattach to a running chip: the chip has the bring-up state from a
        previous session; skip everything and just resume RX polling."""
        _update(0.50, "Warm chip — skipping FW + init")
        return await self._finish_attach(_update, from_warm=True)

    async def _finish_attach(self, _update, *, from_warm: bool) -> bool:
        """Common tail (both paths): probe endpoints, reset pipes, start RX.

        The warm path skips the post-FW reset of the monitor RCR, so the
        filter is armed here on BOTH paths — a chip left by the kernel has
        a non-monitor RCR that drops client→AP (ToDS) frames.
        """
        t = self.transport
        loop = asyncio.get_running_loop()

        from .rx import probe_endpoints
        from .tx import pick_bulk_out_mgmt
        _update(0.60, "Probing USB endpoints...")
        eps = await loop.run_in_executor(None, probe_endpoints, self.dev)
        if not eps.bulk_in:
            logger.error("no bulk-IN endpoint discovered")
            return False
        self._bulk_in_ep = eps.primary_bulk_in
        self._bulk_out_eps = list(eps.bulk_out)
        self._mgmt_bulk_out = pick_bulk_out_mgmt(self._bulk_out_eps)

        _update(0.70, "Clearing stale bulk-pipe state...")
        await loop.run_in_executor(None, self._reset_bulk_pipes)

        if from_warm and not await self._rx_smoke_test():
            logger.error(
                "RTL8188FTV: warm reattach succeeded but bulk-IN is wedged "
                "(no frames in 1500ms). Please unplug + replug the dongle "
                "and try again."
            )
            return False

        from .mac import configure_filter
        _update(0.85, "Arming monitor RX filter...")
        await loop.run_in_executor(None, configure_filter, t)

        self._rx_reader = RxReaderThread(
            loop, self._rx_read_once, self._rx_dispatch, name="rtl8188ftv-rx",
            on_fatal=lambda e: self._on_lost and self._on_lost(e),
        )
        self._rx_reader.start()
        self.is_warm = True
        _update(1.00, "RTL8188FTV online.")
        return True

    async def _rx_smoke_test(self, attempts: int = 15, timeout_ms: int = 100) -> bool:
        """Single bulk-IN read with a generous timeout; return True if any
        byte arrived. Channel 1 on a busy 2.4 GHz environment delivers a
        beacon every ~100 ms so 1.5 s is plenty of margin."""
        loop = asyncio.get_running_loop()

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

    def _reset_bulk_pipes(self) -> None:
        """Clear halts on bulk-IN + bulk-OUT pipes so warm restarts resume RX.

        After a warm reattach the chip's MAC state is intact but the USB
        host controller may still consider the pipes halted from the
        previous session, AND the chip's internal RX FIFO can be wedged
        from frames that arrived after the prior session stopped polling.
        Failures here are non-fatal.
        """
        eps = [self._bulk_in_ep] if self._bulk_in_ep is not None else []
        eps += list(self._bulk_out_eps)
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