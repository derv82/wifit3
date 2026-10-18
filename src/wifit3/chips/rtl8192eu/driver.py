"""RTL8192EU driver — TP-Link TL-WN823N v2/v3, TL-WN822N v4, DWA-131 E1, ...

Cleanroom port of the kernel `rtl8xxxu` driver's 8192e fileops vector
(`8192e.c:1721-1767` `rtl8192eu_fops`).

Cold bring-up mirrors `rtl8xxxu_init_device()` (`core.c:3918-4412`) for the
RTL8192E path, plus the `enable_rf` tail of `rtl8xxxu_start()` (`core.c:7376`):

    connect()
      ├─ claim USB interface
      ├─ probe chip state (mac.is_chip_warm)
      ├─ COLD path (mirrors init_device):
      │   ├─ M0  probe endpoints → config_endpoints (TX-queue flags)
      │   ├─ M1  power_on (LDOV12 / LDO_SW_CTRL + crystal AFE adjust +
      │   │        disabled_to_emu + emu_to_active + REG_CR MACTX/RX enable)
      │   │        ├─ M1b queue pages + DMA lanes + RX page boundary (pre-FW)
      │   │        → FW download → 8051 start
      │   ├─ M2  EFUSE read (MAC + per-group/per-path TX power)
      │   ├─ M3  MAC init table
      │   ├─ M4  PHY BB (BB + AGC tables, crystal cap, AFE_XTAL_CTRL) + RF A/B
      │   │        + RFSW control
      │   ├─ M4c TX buffer boundary + auto-LLT + gen2 usb quirks
      │   ├─ M5  RX data path (HIMR/RCR/DRVINFO) + adaptive controls + beacon
      │   │        params + CCK/OFDM block + CAM invalidate — kernel order
      │   ├─ M6  channel 1 @ 20 MHz + per-group TX power + HW sequencer/BAR/
      │   │        EDCA + RTL8192E tail
      │   ├─ M7  LC calibration + IQ calibration (2T2R) + thermal meter + NAV/
      │   │        HRPWM/mgmt-ACK + LDPC fix + CCK AGC report
      │   └─ M8  enable_rf
      └─ WARM path: skip everything above (chip is FW-running + MAC-enabled
                    from a previous wifit3 session); just resume

      then (both paths) → _finish_attach:
        ├─ reuse the probed USB endpoints (probe fresh on warm)
        ├─ reset bulk pipes (clear_halt + drain stale bytes)
        ├─ on warm: bulk-IN smoke test (1.5s) — surface "please replug"
        │   message if the pipe is wedged from a previous session
        └─ spawn the RX reader asyncio task

The 8192e fops forces the full init: `.needs_full_init = 1` (`8192e.c:1753`)
makes `rtl8xxxu_init_device` clear `macpower` (`core.c:3942-3943`), so the
queue-reservation, TX-buffer-boundary, LLT and usb-quirks blocks always run —
there is no "MAC already powered" shortcut on this part. `.has_tx_report`
(8188e has it, `8188e.c:1866`) and `.init_reg_pkt_life_time`
(`8188e.c:1867`) are **absent** from this fops, so the TX-report enable
(`core.c:4059-4077`) and packet-lifetime (`core.c:4222-4225`) blocks are
skipped. 2T2R: `rf_paths = 2` (`8192e.c:489-491`).

`inject_frame(deauth_bytes)` sends a MGMT frame on the bulk-OUT (`tx.py`,
txdesc40 / `rtl8xxxu_fill_txdesc_v2`). `close()` stops the RX reader and
releases USB.
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
    APS_FSMCO_MAC_ENABLE,
    CR_INIT_POWER_ON,
    CR_MAC_RX_ENABLE,
    CR_MAC_TX_ENABLE,
    REG_8192E_LDOV12_CTRL,
    REG_AFE_CTRL4,
    REG_AFE_PLL_CTRL,
    REG_APS_FSMCO,
    REG_CR,
    REG_LDO_SW_CTRL,
    REG_MACID,
    REG_SYS_CFG,
    RTL8XXXU_MAX_REG_POLL,
    SYS_CFG_SPS_LDO_SEL,
)
from .efuse import EfuseDefaults, read_and_parse
from .firmware import download_firmware, load_firmware_blob, start_firmware
from .iqk import phy_iq_calibrate, phy_lc_calibrate
from .mac import (
    admit_ack_frames,
    apply_monitor_rx_filter,
    config_endpoints,
    disable_usb_hrpwm,
    drop_ack_frames,
    enable_mgmt_ack,
    enable_rx_data_path,
    init_adaptive_controls,
    init_beacon_params,
    init_hw_sequencer,
    init_rtl8192e_tail,
    invalidate_cam_entries,
    is_chip_warm,
    post_fw_mac_init,
    post_phy_mac_init,
    pre_fw_queue_init,
    set_mac_addr,
    set_nav_upper,
)
from .phy import (
    enable_cck_ofdm_block,
    enable_rf,
    enable_thermal_meter,
    fix_ldpc_rx_hang,
    post_mac_init_phy,
    set_rf_switch_ctrl,
    set_tx_power,
    sync_cck_agc_report,
)
from .rx import Endpoints, iter_bulk_frames, probe_endpoints
from ..rx_reader import RxReaderThread
from .transport import RTL8192EUTransport
from .tx import pick_bulk_out_mgmt, send_mgmt_frame

logger = logging.getLogger(__name__)


class RTL8192EUDriver(Driver):
    """Driver for the Realtek RTL8192EU (TL-WN823N v2/v3, TL-WN822N v4, ...).

    fops flags wired below: 2T2R (`rf_paths = 2`, `8192e.c:490`),
    `.needs_full_init = 1` (`8192e.c:1753`), `.gen2_thermal_meter = 1`
    (`8192e.c:1752`), `.supports_ap = 1` / `.max_macid_num = 128` /
    `.max_sec_cam_num = 64` (`8192e.c:1754-1756`), `.adda_1t_*= 0x0fc01616`
    (`8192e.c:1757-1760`), `.trxff_boundary = 0x3cff` (`8192e.c:1761`),
    `.writeN_block_size = 128` (`8192e.c:1748`). Active monitor
    (`enter_active_monitor`) re-points `REG_MACID` so the MAC HW-ACKs a forged
    address (`rtl8xxxu_set_mac`, `core.c:3558-3582`).
    """

    # 2.4 GHz only — `rtl8xxxu_channels_2g` (`core.c:79-108`), ch 1..14.
    SUPPORTED_CHANNELS = list(range(1, 15))
    FAKE_MAC = FakeMacSupport.SPOOFABLE   # REG_MACID re-point HW-ACKs a forged MAC

    @classmethod
    def from_usb_device(
        cls, dev: usb.core.Device, id_entry: DeviceID
    ) -> "RTL8192EUDriver":
        return cls(dev)

    def __init__(self, dev: usb.core.Device):
        super().__init__()          # base owns the ACK tally (_ack_detect_on / _our_tx_macs / _ack_counts)
        self.dev = dev
        self.transport = RTL8192EUTransport(dev)
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        # Real MAC read back from REG_MACID by enter_active_monitor (bytes), or None
        # before the first call. Register-sourced so the warm path (no EFUSE) restores.
        self._real_mac: Optional[bytes] = None
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._mgmt_bulk_out: Optional[int] = None
        self._bulk_in_ep: Optional[int] = None
        self._bulk_out_eps: list[int] = []
        self._endpoints: Optional[Endpoints] = None
        self._rx_reader: Optional[RxReaderThread] = None
        self._claimed: bool = False
        # Unknown until `set_channel` runs: the cold path leaves the radio at its power-on
        # state (the kernel tunes in `rtl8xxxu_config`, not `init_device`), and a warm chip
        # carries whatever the previous session tuned.
        self.current_channel: Optional[int] = None
        self._efuse: EfuseDefaults = EfuseDefaults()  # fallback defaults until cold path parses
        # `priv->cck_agc_report_type` (`core.c:4348-4350`); selects the CCK LNA
        # gain table used by `rtl8192e_cck_rssi` (`8192e.c:1675`).
        self._cck_agc_report_type: int = 0

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
                logger.info("RTL8192EU is WARM, reattaching to running session")
                return await self._warm_reattach(_update)

            logger.info("RTL8192EU is COLD, running full bring-up")
            return await self._cold_bring_up(_update)
        except BringUpError:
            raise
        except Exception as e:
            raise BringUpError("bring-up", str(e)) from e

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        loop = asyncio.get_running_loop()
        try:
            # `rtl8xxxu_config` re-applies the per-group TX power *before* retuning the
            # radio (`core.c:6791-6793`).
            await loop.run_in_executor(None, set_tx_power, self.transport, channel, self._efuse)
            # `.config_channel = rtl8xxxu_gen2_config_channel` (`8192e.c:1734`).
            await loop.run_in_executor(None, set_channel_2g_20mhz, self.transport, channel)
            self.current_channel = channel
            return True
        except (ValueError, IOError):
            logger.exception("set_channel(%d) failed", channel)
            return False

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Build the 40-byte txdesc40 MGMT TX descriptor (HW ACK-retry limit 6, the
        `rtl8xxxu_fill_txdesc_v2` value — `core.c:5298-5301`) and send ``[desc | frame]``
        once on the bulk-OUT pipe. BMC is derived from addr1's group bit."""
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
        """`rtl8xxxu_fill_txdesc_v2` copies the frame's sequence-control field into
        txdw9 without reassigning it (`core.c:5288-5289`), so the frame goes out unchanged."""
        return frame_bytes

    async def close(self) -> None:
        """Stop the RX reader + release USB. Idempotent."""
        if self._rx_reader is not None:
            await self._rx_reader.stop()
            self._rx_reader = None
        self._release_usb()

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Point REG_MACID at ``mac`` so the hardware HW-ACKs frames addressed to it while
        staying in monitor mode. The accept-all monitor RCR keeps the hardware ACK of
        RA == REG_MACID, so no RCR flip is needed. MAC-only, mirroring the Realtek siblings
        (the 8188eus DKMS part is bench-verified SPOOFABLE in `docs/ACKS.md`); the auto-ACK
        of this part is not yet bench-verified. Reversed by ``exit_active_monitor``.
        ``bssid`` is unused (register-MAC ACK is a pure RA-match)."""
        loop = asyncio.get_running_loop()
        # The kernel's `rtl8xxxu_set_mac` only writes (`core.c:3575-3576`); reading the six
        # bytes first is this port's bookkeeping so `exit` can restore them. Keep the first
        # capture sticky: a second `enter` without an `exit` must not re-read the forged MAC.
        if self._real_mac is None:
            self._real_mac = await loop.run_in_executor(None, self._read_mac_reg)
        await loop.run_in_executor(None, set_mac_addr, self.transport, bytes(mac))
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Write the MAC captured by ``enter_active_monitor`` back to REG_MACID. Safe no-op
        if it was never called. Sourced from REG_MACID, so it works on the warm path too
        (no EFUSE parse)."""
        if self._real_mac is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, set_mac_addr, self.transport, self._real_mac)

    def _read_mac_reg(self) -> bytes:
        """The six bytes currently in `REG_MACID..REG_MACID+5` (zeros until a twin/STA MAC is
        programmed — the kernel only writes this register in `rtl8xxxu_set_mac`)."""
        t = self.transport
        return bytes(t.read8(REG_MACID + i) for i in range(6))

    # ---- bring-up paths -------------------------------------------------

    async def _cold_bring_up(self, _update) -> bool:
        loop = asyncio.get_running_loop()

        # Endpoints drive the TX-queue flags (`config_endpoints`), so probe once and
        # cache; `_finish_attach` reuses the result rather than re-probing.
        _update(0.08, "Probing USB endpoints...")
        self._endpoints = await loop.run_in_executor(None, probe_endpoints, self.dev)
        if not self._endpoints.bulk_out:
            raise BringUpError("endpoints", "no bulk-OUT endpoints on this device")
        queues = await loop.run_in_executor(
            None, config_endpoints, self.transport, len(self._endpoints.bulk_out)
        )
        logger.debug("TX queues: high=%s normal=%s low=%s",
                     queues.high, queues.normal, queues.low)

        _update(0.10, "Reading chip ID...")
        sys_cfg = await loop.run_in_executor(None, self.transport.read32, REG_SYS_CFG)
        logger.debug("REG_SYS_CFG = 0x%08x", sys_cfg)

        # M1: `fops->power_on` (`core.c:3945`). Sets MAC TX/RX in REG_CR.
        _update(0.15, "Power on (LDO/AFE adjust -> emu -> active)...")
        await loop.run_in_executor(None, self._power_on)

        # M1b: queue page partition + DMA lanes + RX page boundary, pre-FW
        # (`core.c:3952-3962`, ahead of `download_firmware` at `core.c:3965`).
        _update(0.20, "Queue pages + DMA lanes + RX page boundary (pre-FW)...")
        await loop.run_in_executor(None, pre_fw_queue_init, self.transport, queues)

        _update(0.25, "Loading firmware blob...")
        fw_blob = load_firmware_blob()

        _update(0.35, f"Uploading firmware ({len(fw_blob)} B)...")
        await loop.run_in_executor(None, download_firmware, self.transport, fw_blob)

        _update(0.50, "Polling for MCU_WINT_INIT_READY...")
        await loop.run_in_executor(None, start_firmware, self.transport)

        # M2: `fops->parse_efuse` (`8192e.c:1723`), read with FW running.
        _update(0.55, "Reading EFUSE (MAC + per-group/per-path TX power)...")
        try:
            self._efuse = await loop.run_in_executor(None, read_and_parse, self.transport)
        except (IOError, OSError) as e:
            raise BringUpError("efuse", f"EFUSE read failed: {e}") from e
        if self._efuse.mac_address:
            self.mac_address = self._efuse.mac_address.hex(":")

        # M3: `rtl8xxxu_init_mac` (`core.c:3982`) — the MAC init table.
        _update(0.60, "MAC init (MAC table)...")
        await loop.run_in_executor(None, post_fw_mac_init, self.transport)

        # M4: `rtl8xxxu_init_phy_bb` (`core.c:3988`) + `fops->init_phy_rf`
        # (`core.c:3993`, path A then path B — `8192e.c:679-690`).
        _update(0.68, "PHY init (BB + AGC + crystal cap + RF paths A/B)...")
        await loop.run_in_executor(None, post_mac_init_phy, self.transport, self._efuse)

        # M4b: RFSW control write (`core.c:4007-4014`), immediately after
        # `init_phy_rf`.
        _update(0.72, "RF switch control (REG_FPGA0_XAB_RF_SW_CTRL)...")
        await loop.run_in_executor(None, set_rf_switch_ctrl, self.transport)

        # M4c: TX buffer boundary + PBP + auto-LLT + gen2 USB quirks
        # (`core.c:4021-4054`), the always-taken `!macpower` block.
        _update(0.74, "TX buffer boundary + auto-LLT + USB quirks...")
        await loop.run_in_executor(None, post_phy_mac_init, self.transport)

        # M5: RX-enable block (`core.c:4096-4154`). The kernel runs this BEFORE
        # `set_tx_power` (`core.c:4242`) and the LC/IQ calibration
        # (`core.c:4290-4292`).
        _update(0.76, "Enable RX data path (HIMR + RCR + DRVINFO_SZ)...")
        await loop.run_in_executor(None, enable_rx_data_path, self.transport)

        # M5b: adaptive controls (`core.c:4156-4197`).
        _update(0.79, "Init adaptive controls (RRSR + SIFS/retry + EDCA + fallback)...")
        await loop.run_in_executor(None, init_adaptive_controls, self.transport)

        # M5c: beacon parameters (`core.c:4199-4211`).
        _update(0.80, "Init beacon parameters...")
        await loop.run_in_executor(None, init_beacon_params, self.transport)

        # M5d: enable CCK and OFDM block (`core.c:4230-4232`).
        _update(0.81, "Enable CCK + OFDM baseband blocks...")
        await loop.run_in_executor(None, enable_cck_ofdm_block, self.transport)

        # M5e: invalidate all CAM entries (`core.c:4237`).
        _update(0.83, "Invalidate CAM entries...")
        await loop.run_in_executor(None, invalidate_cam_entries, self.transport)

        # M6: `fops->set_tx_power(priv, 1, false)` (`core.c:4242`). The kernel does NOT tune
        # the radio here — `rtl8xxxu_config` does that (with the TX power) on the first
        # channel change (`core.c:6785-6793`), so the port leaves the RF mode at its
        # power-on state until `set_channel` runs.
        _update(0.85, "Set TX power (channel group 1)...")
        await loop.run_in_executor(None, set_tx_power, self.transport, 1, self._efuse)

        # M6b: HW sequencer, BAR mode, fast EDCA (`core.c:4252-4259`).
        _update(0.87, "Init HW sequencer / BAR / fast EDCA...")
        await loop.run_in_executor(None, init_hw_sequencer, self.transport)

        # M6c: RTL8192E chip tail (`core.c:4264-4275`).
        _update(0.88, "RTL8192E chip tail (RTS BW + USB mode switch)...")
        await loop.run_in_executor(None, init_rtl8192e_tail, self.transport)

        # M7: `fops->phy_lc_calibrate` (`core.c:4290`) then `fops->phy_iq_calibrate`
        # (`core.c:4292`).
        _update(0.89, "LC calibration (VCO tank, paths A/B)...")
        await loop.run_in_executor(None, phy_lc_calibrate, self.transport)

        _update(0.91, "IQ calibration (2T2R LOK + TX/RX IQK, paths A/B)...")
        await loop.run_in_executor(None, phy_iq_calibrate, self.transport)

        # M7b: thermal meter (`core.c:4297-4305`, `.gen2_thermal_meter = 1`).
        _update(0.93, "Enable thermal meter (RF T_METER_8723B)...")
        await loop.run_in_executor(None, enable_thermal_meter, self.transport)

        # M7c: NAV upper (`core.c:4310-4312`), USB HRPWM (`core.c:4326`) and
        # management-frame ACK (`core.c:4329-4332`).
        await loop.run_in_executor(None, set_nav_upper, self.transport)
        await loop.run_in_executor(None, disable_usb_hrpwm, self.transport)
        await loop.run_in_executor(None, enable_mgmt_ack, self.transport)

        # M7d: LDPC-rx-hang fix (`core.c:4334-4342`) then the CCK AGC report
        # path-A/path-B sync + `REG_AGC_RPT` update (`core.c:4348-4367`).
        _update(0.94, "Fix LDPC RX hang + align CCK AGC report...")
        await loop.run_in_executor(None, fix_ldpc_rx_hang, self.transport)
        await loop.run_in_executor(None, self._read_cck_agc_report_type)

        # M8: `enable_rf` (`core.c:7376`, in `rtl8xxxu_start`).
        _update(0.96, "Enable RF (OFDM RX/TX paths A/B)...")
        await loop.run_in_executor(None, enable_rf, self.transport)

        return await self._finish_attach(_update, from_warm=False)

    async def _warm_reattach(self, _update) -> bool:
        """Reattach to a chip already running FW + MAC-enabled.

        Skip the FW upload + MAC/PHY init + calibration + channel set +
        `enable_rf` chain — the chip is already in that state from a previous
        session. Read the burned calibration back, then resume USB polling. If the
        bulk-IN pipe is wedged from the previous session we surface a "please replug"
        message in :meth:`_finish_attach`'s smoke test.
        """
        loop = asyncio.get_running_loop()
        _update(0.50, "Warm chip — skipping FW + MAC/PHY init")
        # EFUSE must be re-read even on the warm path: `set_channel` re-applies the
        # per-group TX power on every tune (`core.c:6791`), and the `0x22` defaults would
        # clobber the real calibration the previous (cold) session programmed.
        _update(0.52, "Reading EFUSE (MAC + per-group/per-path TX power)...")
        try:
            self._efuse = await loop.run_in_executor(None, read_and_parse, self.transport)
        except (IOError, OSError) as e:
            raise BringUpError("efuse", f"EFUSE read failed: {e}") from e
        if self._efuse.mac_address:
            self.mac_address = self._efuse.mac_address.hex(":")
        return await self._finish_attach(_update, from_warm=True)

    async def _finish_attach(self, _update, *, from_warm: bool) -> bool:
        """Common tail: probe endpoints, clear halts, start RX loop."""
        loop = asyncio.get_running_loop()

        _update(0.97, "Resolving USB endpoints + clearing halts...")
        eps = self._endpoints
        if eps is None:   # warm path: `_cold_bring_up` never probed
            eps = await loop.run_in_executor(None, probe_endpoints, self.dev)
            self._endpoints = eps
        if not eps.bulk_in:
            logger.error("no bulk-IN endpoint discovered")
            return False
        self._bulk_in_ep = eps.primary_bulk_in
        self._bulk_out_eps = list(eps.bulk_out)
        if eps.bulk_out:
            self._mgmt_bulk_out = pick_bulk_out_mgmt(eps.bulk_out)

        await loop.run_in_executor(None, self._reset_bulk_pipes)

        if from_warm:
            _update(0.98, "Smoke-testing bulk-IN (warm reattach)...")
            if not await self._rx_smoke_test():
                logger.error(
                    "RTL8192EU: warm reattach succeeded but bulk-IN is wedged "
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
            loop, self._rx_read_once, self._rx_dispatch, name="rtl8192eu-rx",
            on_fatal=lambda e: self._on_lost and self._on_lost(e)
        )
        self._rx_reader.start()
        self.is_warm = True
        _update(1.00, "RTL8192EU online.")
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
        for _desc, mpdu, rssi in iter_bulk_frames(
            buf, cck_agc_report_type=self._cck_agc_report_type
        ):
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
        AP's ACKs to us. The 8192e fops leaves RXFLTMAP at the HW default (ACKs filtered), so
        this bit must be opened explicitly (the base arms the tally). Not enter_active_monitor,
        which makes the chip EMIT ACKs."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, admit_ack_frames, self.transport)

    async def _disable_rx_acks(self) -> None:
        """Restore the default monitor RX filter (clear RXFLTMAP1 bit13)."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, drop_ack_frames, self.transport)

    def _read_cck_agc_report_type(self) -> None:
        """Port of the RTL8192E CCK-AGC-report block (`core.c:4348-4367`).

        Snapshots path A's `FPGA0_HSSI_PARM2_CCK_HIGH_PWR`, forces path B and
        `REG_AGC_RPT` to agree, and stores the 0/1 value that selects the CCK
        LNA gain table in `rtl8192e_cck_rssi` (`8192e.c:1675-1682`).
        """
        self._cck_agc_report_type = sync_cck_agc_report(self.transport)

    # ---- power-on internals --------------------------------------------

    def _power_on(self) -> None:
        """Port of `rtl8192eu_power_on` (`8192e.c:1547-1593`).

        The RTL8192E power-on is not the 8188e one: it adjusts the 1.2 V LDO rail
        (or the SPS-LDO switch), runs a crystal AFE trim, then enables MAC
        TX/RX in `REG_CR` directly — there is no TRXFF_BNDY hold-off on this part.
        """
        t = self.transport

        val32 = t.read32(REG_SYS_CFG)
        if val32 & SYS_CFG_SPS_LDO_SEL:
            t.write8(REG_LDO_SW_CTRL, 0xC3)
        else:
            # Raise 1.2V voltage.
            val32 = t.read32(REG_8192E_LDOV12_CTRL) & 0xFF0FFFFF
            val32 |= 0x00500000
            t.write32(REG_8192E_LDOV12_CTRL, val32)
            t.write8(REG_LDO_SW_CTRL, 0x83)

        # Adjust AFE before enabling PLL.
        self._crystal_afe_adjust()
        self._disabled_to_emu()
        self._emu_to_active()

        t.write16(REG_CR, 0x0000)
        # Enable MAC DMA/WMAC/SCHEDULE/SEC + MAC TX/RX, bit10 = 32k calibration.
        val16 = t.read16(REG_CR) | CR_INIT_POWER_ON | CR_MAC_TX_ENABLE | CR_MAC_RX_ENABLE
        t.write16(REG_CR, val16)

    def _crystal_afe_adjust(self) -> None:
        """Port of `rtl8192e_crystal_afe_adjust` (`8192e.c:1330-1360`)."""
        t = self.transport

        # 40 MHz crystal source: 0x28[2]=0, 0x78[10:7]=0.
        val8 = t.read8(REG_AFE_PLL_CTRL) & 0xFB
        t.write8(REG_AFE_PLL_CTRL, val8)
        val32 = t.read32(REG_AFE_CTRL4) & 0xFFFFFC7F
        t.write32(REG_AFE_CTRL4, val32)

        # AFE PLL KVCO selection: 0x28[6]=0, 0x78[21]=0.
        val8 = t.read8(REG_AFE_PLL_CTRL) & 0xBF
        t.write8(REG_AFE_PLL_CTRL, val8)
        val32 = t.read32(REG_AFE_CTRL4) & 0xFFDFFFFF
        t.write32(REG_AFE_CTRL4, val32)

    def _disabled_to_emu(self) -> None:
        """Port of `rtl8192e_disabled_to_emu` (`8192e.c:1362-1370`).

        Clear suspend-enable and power-down-enable: `0x05[4:3] = 0`.
        """
        t = self.transport
        val8 = t.read8(REG_APS_FSMCO + 1) & ~0x18 & 0xFF
        t.write8(REG_APS_FSMCO + 1, val8)

    def _emu_to_active(self) -> None:
        """Port of `rtl8192e_emu_to_active` (`8192e.c:1372-1445`)."""
        t = self.transport

        # Disable HWPDN 0x04[15]=0, SW LPS 0x04[10]=0, WL suspend 0x04[12:11]=0
        # — all three via the byte at REG_APS_FSMCO + 1 (``8192e.c:1378-1391``).
        val8 = t.read8(REG_APS_FSMCO + 1) & ~0x80 & 0xFF
        t.write8(REG_APS_FSMCO + 1, val8)
        val8 = t.read8(REG_APS_FSMCO + 1) & ~0x04 & 0xFF
        t.write8(REG_APS_FSMCO + 1, val8)
        val8 = t.read8(REG_APS_FSMCO + 1) & ~0x18 & 0xFF
        t.write8(REG_APS_FSMCO + 1, val8)

        # Wait till 0x04[17] = 1 power ready.
        for _ in range(RTL8XXXU_MAX_REG_POLL):
            if t.read32(REG_APS_FSMCO) & (1 << 17):
                break
            time.sleep(0.00001)
        else:
            raise IOError("emu_to_active: power-ready bit (APS_FSMCO[17]) never set")

        # Release WLON reset 0x04[16]=1 (byte at REG_APS_FSMCO + 2).
        t.write8(REG_APS_FSMCO + 2, t.read8(REG_APS_FSMCO + 2) | 0x01)

        # Set MAC_ENABLE, then poll until it self-clears.
        val32 = t.read32(REG_APS_FSMCO) | APS_FSMCO_MAC_ENABLE
        t.write32(REG_APS_FSMCO, val32)
        for _ in range(RTL8XXXU_MAX_REG_POLL):
            if (t.read32(REG_APS_FSMCO) & APS_FSMCO_MAC_ENABLE) == 0:
                break
            time.sleep(0.00001)
        else:
            raise IOError("emu_to_active: MAC_ENABLE bit never cleared")
