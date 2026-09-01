"""RTL8822CU USB monitor-mode receiver and management-frame injector."""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, ClassVar, Optional

import usb.core
import usb.util

from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.dot11.parser import WlanFrameParser
from wifit3.chips.rx_reader import RxReaderThread

from .cal import DackState, odm_dm_init
from .kfree import PowerTrimState
from .dm import PhydmState
from .txgapk import TxGapKState
from .chipid import ChipInfo, read_chip_info
from .constants import (
    C2H_DEFEATURE_RSVD,
    CHIP_ID_RTL8822CU,
    HALMAC_RF_1T1R,
    REG_C2HEVT_MSG_NORMAL,
    REG_PMC_DBG_CTRL1,
    REG_USB_HRPWM,
)
from .efuse import EfuseInfo, RfPath, hal_rfpath_init, read_efuse
from .firmware import (
    H2cState,
    download_firmware,
    load_firmware,
    MacHiddenRpt,
    read_mac_hidden_rpt,
    send_general_info,
)
from .mac import (
    arm_monitor,
    config_rx_info,
    cut_mask_from_sys_cfg1,
    enable_bb_rf,
    enter_monitor_mode,
    init_mac_cfg,
    init_mac_flow_tail,
    btcoex_wifionly_hw_config,
    init_misc,
    init_usb_cfg,
    mac_power_off,
    mac_power_on,
    set_mac_addr,
    sync_rcr,
)
from .phy import (
    BB_PATH_A,
    TrxPathState,
    config_bb_rf,
    config_trx_path,
    phy_bf_init,
    switch_channel,
)
from .rx import iter_bulk_frames, read_rx_burst
from .transport import EndpointLayout, RTL8822CUTransport
from .txpwr_index import TxPwrIdxState, txpwr_idx_state
from .tx import TX_DESC_QSEL_MGMT, build_tx_desc_inject, pick_bulk_out_ep, write_bulk

logger = logging.getLogger(__name__)

_DEFAULT_CHANNEL = 1


class RTL8822CUDriver(Driver):
    SUPPORTED_CHANNELS: ClassVar[list[int]] = list(range(1, 15)) + [36, 40, 44, 48, 149, 153, 157, 161, 165]
    FAKE_MAC = FakeMacSupport.SPOOFABLE

    def __init__(self, dev: usb.core.Device):
        super().__init__()
        self.dev = dev
        self.transport = RTL8822CUTransport(dev)
        self.mac_address: Optional[str] = None
        self.is_warm = False
        self.layout: Optional[EndpointLayout] = None
        self.chip_id: Optional[int] = None
        self.chip_info: Optional[ChipInfo] = None
        self.efuse: Optional[EfuseInfo] = None
        self.rfpath: Optional[RfPath] = None   # trx_path_bmp + max_tx_cnt, EFUSE x hidden report
        self.mac_hidden_rpt = MacHiddenRpt()   # C2H report; PackageType feeds send_general_info
        self.txpwr: Optional[TxPwrIdxState] = None
        self.trx_path = TrxPathState()
        self.dack = DackState()
        self.h2c = H2cState()
        self.power_trim = PowerTrimState()
        self.txgapk = TxGapKState()
        self.phydm = PhydmState()
        self._claimed = False
        self._rx_callback: Optional[Callable] = None
        self._on_lost: Optional[Callable] = None
        self._rx_reader: Optional[RxReaderThread] = None
        self._bulk_in_ep: Optional[int] = None
        self._bulk_out_eps: list[int] = []
        self.current_band_is_2g = True
        self._current_channel = 1
        self._rx_regs_after_tune_logged = False

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RTL8822CUDriver":
        return cls(dev)

    def register_rx_callback(self, cb: Callable) -> None:
        self._rx_callback = cb

    def register_disconnect_callback(self, cb: Callable) -> None:
        self._on_lost = cb

    def _claim(self) -> None:
        if self._claimed:
            return
        try:
            self.dev.set_configuration()
        except usb.core.USBError:
            pass
        usb.util.claim_interface(self.dev, self.layout.interface if self.layout else 0)
        self._claimed = True

    def _cold_cycle(self, t: RTL8822CUTransport, bulk_out: int, *, beacon: bool = False) -> None:
        """Firmware download, MAC config, general info. ``beacon`` selects the second cycle's
        ``dump_mgntframe`` TX descriptor and its resolved ``get_trx_path`` config."""
        download_firmware(t.dev, t, bulk_out, load_firmware(), beacon=beacon,
                          rsvd_boundary=0x792 if beacon else 0)
        self.h2c.box_num = 0                    # [SRC hal_halmac.c:3465]
        init_mac_cfg(t)
        init_mac_flow_tail(t)
        if beacon:
            # cycle 2: rtw_hal_get_trx_path answers from the now populated trx_path_bmp, and the
            # PackageType comes from the MAC hidden report. [SRC hal/hal_halmac.c:3151-3157]
            send_general_info(t.dev, t, bulk_out, self.h2c, self.efuse.rfe_type,
                              self.chip_info.chip_ver, rf_type=self.rfpath.halmac_rf_type,
                              tx_ant=self.rfpath.tx_path, rx_ant=self.rfpath.rx_path,
                              package=self.mac_hidden_rpt.package_type)
        else:
            # cycle 1: trx_path_bmp is still the zeroed hal_data, so rtw_hal_get_trx_path falls to
            # rf_type_to_default_trx_bmp(RF_1T1R) [SRC hal/hal_com.c:17571-17572, core/rtw_rf.c:2077-2086].
            send_general_info(t.dev, t, bulk_out, self.h2c, self.efuse.rfe_type,
                              self.chip_info.chip_ver, rf_type=HALMAC_RF_1T1R,
                              tx_ant=BB_PATH_A, rx_ant=BB_PATH_A, package=0)

    def _bringup(self, t: RTL8822CUTransport, bulk_out: int = 0, progress=None) -> None:
        """Cold register init: power-on, firmware, MAC, BB/RF. It runs twice because the first
        cycle exists only to read the MAC-hidden report, and ends by powering the chip back off
        [SRC hal/hal_com.c:1560]. Bulk writes go through ``t.dev``."""
        progress = progress or (lambda *a: None)
        progress(0.1, "Reading RTL8822CU chip id / EFUSE")
        self.chip_info = read_chip_info(t)
        self.chip_id = self.chip_info.chip_id
        if self.chip_id != CHIP_ID_RTL8822CU:
            raise BringUpError("chip-id", f"expected 0x{CHIP_ID_RTL8822CU:02x}, got 0x{self.chip_id:02x}")
        self.efuse = read_efuse(t)
        if self.efuse.map_valid:
            self.mac_address = ":".join(f"{octet:02x}" for octet in self.efuse.mac_address)
        cut_mask = cut_mask_from_sys_cfg1(self.chip_info.raw_cfg1)

        progress(0.2, "Uploading RTL8822C firmware (cycle 1 of 2)")
        mac_power_on(t, cut_mask=cut_mask, chip_ver=self.chip_info.cut)
        t.write8(REG_C2HEVT_MSG_NORMAL, C2H_DEFEATURE_RSVD)   # ask FW for the report
        self._cold_cycle(t, bulk_out)
        progress(0.45, "Reading RTL8822C MAC capabilities")
        self.mac_hidden_rpt = read_mac_hidden_rpt(t)
        # rtw_hal_rfpath_init [SRC hal/hal_intf.c:318-390] consumes the report that
        # [SRC hal/rtl8822c/rtl8822c_ops.c:867] has just read, and the TX power index state follows
        # the EFUSE, not the tune, so both are built once here
        # [SRC rtw_hal_dm_init hal/hal_intf.c:199-201].
        self.rfpath = hal_rfpath_init(self.efuse, ant_num=self.mac_hidden_rpt.ant_num,
                                      hw_stype=self.mac_hidden_rpt.hw_stype,
                                      rf_2t2r=self.chip_info.rf_2t2r)
        self.txpwr = txpwr_idx_state(self.efuse, self.rfpath)
        if self.txpwr is None:
            logger.error("RTL8822CU EFUSE 0xC8 tpt_mode=%u selects TSSI TX power, which is not "
                         "ported; the TX AGC table keeps its power on defaults",
                         self.efuse.tpt_mode)
        # hal_btcoex_PowerOffSetting: clear the WiFi on/off bit in the BT scoreboard register.
        # [SRC hal/hal_btcoex.c:5949-5956]
        t.write16(REG_PMC_DBG_CTRL1 + 2, 0x8000)
        mac_power_off(t, cut_mask=cut_mask)
        # rtw_set_ps_mode -> rtw_set_rpwm(PS_STATE_S4), which truncates to 0.
        # [SRC core/rtw_pwrctrl.c:1183, hal/rtl8822c/usb/rtl8822cu_ops.c:106]
        t.write8(REG_USB_HRPWM, 0x00)

        progress(0.5, "Uploading RTL8822C firmware (cycle 2 of 2)")
        mac_power_on(t, cut_mask=cut_mask, chip_ver=self.chip_info.cut)
        self._cold_cycle(t, bulk_out, beacon=True)
        config_rx_info(t)
        enable_bb_rf(t)
        progress(0.85, "Loading RTL8822C BB/RF tables")
        # The BB/RF FW-offload blocks per batch on its CFG_PARAM_ACK C2H, read synchronously off
        # the RX bulk-IN. Safe here: the async RX reader is not started until after monitor entry.
        config_bb_rf(t, t.dev, bulk_out, self._bulk_in_ep, cut=self.chip_info.cut,
                     rfe_type=self.efuse.rfe_type, crystal_cap=self.efuse.crystal_cap,
                     dis_dpd_rate=self.efuse.dis_dpd_rate)
        init_usb_cfg(t)
        sync_rcr(t)

        progress(0.9, "RTL8822C PHY calibration")
        config_trx_path(t, self.trx_path, tx_path=self.rfpath.tx_path,
                        rx_path=self.rfpath.rx_path, max_tx_cnt=self.rfpath.max_tx_cnt,
                        rfe_type=self.efuse.rfe_type)
        odm_dm_init(t, self.h2c, self.dack, self.efuse, self.power_trim, self.txgapk, self.phydm)
        phy_bf_init(t)
        btcoex_wifionly_hw_config(t)
        init_misc(t)
        self._log_state("bringup")

    def _log_state(self, phase: str) -> None:
        """Aggregate debug dump of the significant values read from the chip so far. No new chip
        reads (register reads can mutate state); logs only what bringup already collected. Silent
        unless logging is at DEBUG. Never raises: logging must not break bringup."""
        if not logger.isEnabledFor(logging.DEBUG):
            return
        try:
            parts = []
            ci = self.chip_info
            if ci is not None:
                parts.append(
                    f"chip_id=0x{ci.chip_id:02x} cut={ci.cut} rf_2t2r={ci.rf_2t2r} "
                    f"rom_ver={ci.rom_version} chip_ver=0x{ci.chip_ver:x} "
                    f"cfg1=0x{ci.raw_cfg1:08x} status1=0x{ci.raw_status1:08x}")
            ef = self.efuse
            if ef is not None:
                parts.append(
                    f"mac={self.mac_address} efuse_autoload_ok={ef.autoload_ok} "
                    f"map_valid={ef.map_valid} rfe_type=0x{ef.rfe_type:02x} "
                    f"xtal_cap=0x{ef.crystal_cap:02x}")
            rp = self.rfpath
            if rp is not None:
                parts.append(f"trx_path_bmp=0x{rp.trx_path_bmp:02x} max_tx_cnt={rp.max_tx_cnt}")
            parts.append(
                f"package=0x{self.mac_hidden_rpt.package_type:x} band_2g={self.current_band_is_2g} "
                f"ch={self._current_channel}")
            logger.debug("rtl8822cu state[%s] | %s", phase, "  ".join(parts))
        except Exception as exc:                    # logging must never break bringup
            logger.debug("rtl8822cu state[%s]: dump failed: %r", phase, exc)

    def _log_rx_registers(self, phase: str) -> None:
        """Read back and DEBUG-log the receive-governing config registers as one aggregate block,
        so a live capture can be compared against what the vendor monitor path requires. Only
        config registers are read (no FIFO read ports, no C2H mailbox, no data-popping status
        regs); every one of these addresses is outside the transport's page-echo section, so each
        read is a plain register read. Never raises: diagnostics must not break RX."""
        if not logger.isEnabledFor(logging.DEBUG):
            return
        t = self.transport
        try:
            rcr = t.read32(0x0608)                   # REG_RCR, monitor target 0x90000001
            cr = t.read16(0x0100)                    # REG_CR: bit3 RXDMA_EN, bit7 MACRXEN
            msr = t.read8(0x0102)                    # media status / opmode, port0 = bits[1:0]
            fltmap0 = t.read16(0x06A0)               # mgmt accept map, beacon = subtype 8 = bit 8
            fltmap1 = t.read16(0x06A2)               # ctrl accept map
            fltmap2 = t.read16(0x06A4)               # data accept map
            txpause = t.read16(0x0522)               # per-queue TX pause (0x0522/0x0523)
            rxdma_mode = t.read8(0x0290)             # REG_RXDMA_MODE burst cfg (bit1 DMA_MODE)
            rxdma_agg_th = t.read32(0x0280)          # RXDMA_AGG_PG_TH: low16 size|timeout, bit29 EN_PRE_CALC
            rxdma_agg_mode = t.read8(0x0283)         # USB(0)/DMA(bit7) aggregation mode
            txdma_pq_map = t.read8(0x010C)           # REG_TXDMA_PQ_MAP: bit2 RXDMA_AGG_EN
            rxpsf = t.read16(0x1610)                 # REG_RXPSF_CTRL
            logger.debug(
                "rtl8822cu rxregs[%s] | RCR=0x%08x CR=0x%04x MSR=0x%02x "
                "RXFLTMAP0=0x%04x RXFLTMAP1=0x%04x RXFLTMAP2=0x%04x TXPAUSE=0x%04x "
                "RXDMA_MODE=0x%02x RXDMA_AGG_TH=0x%08x RXDMA_AGG_MODE=0x%02x "
                "TXDMA_PQ_MAP=0x%02x RXPSF=0x%04x | "
                "CR.RXDMA_EN=%d CR.MACRXEN=%d MSR.port0=%d RXFLTMAP0.beacon(bit8)=%d "
                "RXDMA_AGG_EN=%d RXDMA_AGG_ENPRECALC=%d",
                phase, rcr, cr, msr, fltmap0, fltmap1, fltmap2, txpause,
                rxdma_mode, rxdma_agg_th, rxdma_agg_mode, txdma_pq_map, rxpsf,
                (cr >> 3) & 1, (cr >> 7) & 1, msr & 0x03, (fltmap0 >> 8) & 1,
                (txdma_pq_map >> 2) & 1, (rxdma_agg_th >> 29) & 1)
        except Exception as exc:                    # diagnostics must never break RX
            logger.debug("rtl8822cu rxregs[%s]: readback failed: %r", phase, exc)

    def _monitor_entry(self) -> None:
        """The vendor operational tail after cold init: arm the monitor interface, tune the first
        channel (which runs the per-hop TSSI flush), enter monitor opmode / RX-enable. The following
        channel tune re-latches the BB/RF RX chain. This is the exact sequence the pcap harness
        drives and byte-verifies (ops 9333-9570)."""
        arm_monitor(self.transport, self.h2c, bytes(self.efuse.mac_address))
        switch_channel(self.transport, _DEFAULT_CHANNEL, self.txpwr)
        enter_monitor_mode(self.transport)
        self._log_state("monitor_entry")
        self._log_rx_registers("monitor_entry")

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        loop = asyncio.get_running_loop()
        if progress_cb:
            progress_cb(0.05, "Inspecting RTL8822CU USB endpoints")
        self.layout = self.transport.endpoints()
        self._claim()
        if not self.layout.bulk_out:
            raise BringUpError("endpoints", "RTL8822CU has no bulk OUT endpoint for firmware upload")
        if not self.layout.bulk_in:
            raise BringUpError("endpoints", "RTL8822CU has no bulk IN endpoint for RX")

        def _emit(pct: float, msg: str) -> None:
            if progress_cb:
                loop.call_soon_threadsafe(progress_cb, pct, msg)

        self._bulk_in_ep = self.layout.bulk_in[0]
        self._bulk_out_eps = list(self.layout.bulk_out)
        try:
            await loop.run_in_executor(
                None, self._bringup, self.transport, self.layout.bulk_out[0], _emit)
            await loop.run_in_executor(None, self._monitor_entry)
        except (IOError, usb.core.USBError) as exc:
            raise BringUpError("firmware", str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise BringUpError("phy", str(exc)) from exc
        # Start the RX reader only AFTER monitor entry. arm_monitor + the first tune +
        # enter_monitor_mode is what enables MAC RX; starting it before bringup (the prior "sibling
        # parity" position, commit 7333e4e5) ran the bulk IN pipe through the power cycle and both
        # firmware downloads while the RX chain was not latched, and hardware saw zero frames.
        # rtl8821au and the last known good 1a445c1f both start the reader here, last.
        self._rx_reader = RxReaderThread(
            loop, self._rx_read_once, self._rx_dispatch, name="rtl8822cu-rx",
            on_fatal=lambda exc: self._on_lost and self._on_lost(exc),
        )
        self._rx_reader.start()
        logger.debug("rtl8822cu RX reader started (bulk IN ep 0x%02x) after monitor entry",
                     self._bulk_in_ep)
        self._current_channel = _DEFAULT_CHANNEL
        self.is_warm = True
        if progress_cb:
            progress_cb(1.0, "RTL8822CU monitor receiver online")
        return True

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Runtime channel tune: the vendor ``config_phydm_switch_channel_8822c`` +
        ``switch_bandwidth`` sequence (``phy.switch_channel``), the same call the pcap harness
        verifies per hop. ``scan`` no longer selects a different path: every hop on the wire is a
        plain ``switch_channel``."""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, switch_channel, self.transport, channel,
                                       self.txpwr)
            self._current_channel = channel
            self.current_band_is_2g = channel <= 14
            if not self._rx_regs_after_tune_logged:
                self._rx_regs_after_tune_logged = True
                await loop.run_in_executor(None, self._log_rx_registers, "after_tune")
            return True
        except (IOError, ValueError, usb.core.USBError) as exc:
            logger.warning("RTL8822CU channel %d failed: %s", channel, exc)
            return False

    async def close(self) -> None:
        if self._rx_reader:
            await self._rx_reader.stop()
            self._rx_reader = None
        if self._claimed:
            try:
                usb.util.release_interface(self.dev, self.layout.interface if self.layout else 0)
            except usb.core.USBError:
                pass
            usb.util.dispose_resources(self.dev)
            self._claimed = False

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Point REG_MACID at ``mac`` so the hardware HW-ACKs frames addressed to it while
        staying in monitor mode. The accept-all monitor RCR (AAP) still HW-ACKs
        RA == REG_MACID, so no RCR flip is needed. MAC-only, mirroring the proven Realtek
        siblings. Reversed by ``exit_active_monitor``. ``bssid`` is unused (register-MAC
        ACK is a pure RA-match)."""
        await self._write_mac(bytes(mac))
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the card's real EFUSE MAC in REG_MACID (stop ACKing the forged MAC)."""
        if self.mac_address:
            await self._write_mac(bytes(int(x, 16) for x in self.mac_address.split(":")))

    async def _write_mac(self, mac6: bytes) -> None:
        """Program ``mac6`` into REG_MACID, offloaded so the blocking control transfer
        never stalls the RX dispatch."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: set_mac_addr(self.transport, mac6))

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        if not self._bulk_out_eps:
            return False
        try:
            desc = build_tx_desc_inject(frame_bytes, band_is_2g=self.current_band_is_2g)
            ep = pick_bulk_out_ep(self._bulk_out_eps, queue=TX_DESC_QSEL_MGMT)
            loop = asyncio.get_running_loop()
            sent = await loop.run_in_executor(None, lambda: write_bulk(self.dev, ep, desc + frame_bytes))
            return sent == len(desc) + len(frame_bytes)
        except (ValueError, IOError, usb.core.USBError) as exc:
            logger.warning("RTL8822CU TX failed: %s", exc)
            return False

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        return frame_bytes

    async def _enable_rx_acks(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self.transport.write16(0x06A2, self.transport.read16(0x06A2) | (1 << 13)))

    async def _disable_rx_acks(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self.transport.write16(0x06A2, self.transport.read16(0x06A2) & ~(1 << 13)))

    def _rx_read_once(self) -> bytes | None:
        if self._bulk_in_ep is None:
            return None
        return read_rx_burst(self.dev, self._bulk_in_ep, max_size=16384, timeout_ms=100)

    def _rx_dispatch(self, buf: bytes) -> None:
        callback = self._rx_callback
        if callback is None and not self._ack_detect_on:
            return
        for _stat, mpdu, rssi in iter_bulk_frames(buf):
            if len(mpdu) == 10 and mpdu[0] == 0xD4:
                self.record_ack(mpdu)
                continue
            if callback is None:
                continue
            parsed = WlanFrameParser.parse_80211_frame(mpdu, rssi if rssi is not None else -100)
            if parsed:
                callback(parsed)
