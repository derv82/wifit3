"""RTL8922AU driver: Realtek RTL8922A (802.11be) over USB, ported from rtw89-7.2.
See RTL8922AU.md.
"""
import asyncio
import logging
import time
from typing import Callable, Optional

import libusb_package
import usb.core
import usb.util

from wifit3.dot11.parser import WlanFrameParser
from wifit3.errors import BringUpError

from ..driver import Driver, DeviceID, FakeMacSupport, ProgressCallback
from ..rx_reader import RxReaderThread
from . import chan, coex, firmware, mac, phy, rfk, tx
from .rx import iter_bulk_frames, parse_c2h_hdr, RX_RECVBUF_SZ, RX_TYPE_WIFI, RX_TYPE_C2H
from .tx import BULKOUT_ID_B0MG
from .constants import (
    R_BE_PAD_CTRL2, _LIBUSB_SPEED_SUPER, USB_SWITCH_DELAY, B_BE_MATCH_CNT,
    B_BE_RSM_EN_V1, B_BE_NO_PDN_CHIPOFF_V1, B_BE_USB_AUTO_INSTALL_MASK, B_BE_USB23_SW_MODE,
    B_BE_USB3_FORCE, B_BE_USB2_FORCE, B_BE_FORCE_U3_CK, B_BE_FORCE_U2_CK, B_BE_FORCE_CLK_U2,
    B_BE_USB3_GEN_MODE, B_BE_USB3_LANE_MODE, BULKOUT_ID_H2C, RTW89_WIFI_ROLE_MONITOR,
    RTW89_NET_TYPE_NO_LINK, RTW89_BSSID_MATCH_ALL, R_BE_SYS_CHIPINFO, B_BE_HW_ID_MASK,
    DEFAULT_MON_RX_FLTR,
)
from .transport import RTL8922AUTransport

logger = logging.getLogger(__name__)


class RTL8922AUDriver(Driver):
    """Realtek RTL8922A (802.11be) USB driver, ported from the rtw89 vendor source."""

    # Auto-ACKs an arbitrary forged MAC by programming it as the addr-cam SMA (enter_active_monitor);
    # bench-confirmed the card still monitors foreign/toDS traffic and ACKs only the armed MAC.
    # [SRC] cam.c:819 (SMA = the vif's own mac_addr, matched against a received frame's addr1).
    FAKE_MAC = FakeMacSupport.SPOOFABLE

    # On a USB-2 port the rtw89 mode switch re-enumerates the card mid-connect(); connect() re-acquires
    # its own handle, so no user action is needed. [SRC] usb.c rtw89_usb_switch_mode_be.
    DEVICE_REENUMERATES = True

    # 2.4 GHz + 5 GHz (non-DFS only) at 20 MHz. DFS channels (52-64, 100-144) are excluded: wifite
    # ships non-DFS only, and a DFS hop hears nothing without a CAC dwell. TODO: 6 GHz (8922a
    # support_bands includes it). [SRC] rtw8922a.c:3210.
    SUPPORTED_CHANNELS = (
        list(range(1, 15))
        + [36, 40, 44, 48, 149, 153, 157, 161, 165]
    )

    def __init__(self) -> None:
        super().__init__()
        self.dev: Optional[usb.core.Device] = None
        self.transport: Optional[RTL8922AUTransport] = None
        self._rx_cb: Optional[Callable] = None
        self._disconnect_cb: Optional[Callable[[Exception], None]] = None
        self._h2c_ep: Optional[int] = None
        self._bulk_in_ep: Optional[int] = None
        self._rx_reader: Optional[RxReaderThread] = None
        self._mgmt_ep: Optional[int] = None
        self._tx_seq: int = 0
        self._band_is_2g: bool = True
        # entity_force_hw model for the prehdl double-tune: True = next pass is the forced-PHY_0 (2+0)
        # pass, False = the cleared (1+1) pass. A hop resets it True. [SRC] core.c:501-512.
        self._prehdl_force_phy0: bool = True
        self._active_mac: Optional[bytes] = None
        self.mac_address: Optional[str] = None
        self._vid: Optional[int] = None      # for re-finding the card after the mode-switch re-enum
        self._pid: Optional[int] = None

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RTL8922AUDriver":
        self = cls()
        self.dev = dev
        self._vid, self._pid = id_entry.vid, id_entry.pid   # to re-find the card after the re-enum
        self.transport = RTL8922AUTransport(dev)
        return self

    def register_rx_callback(self, cb: Callable) -> None:
        self._rx_cb = cb

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        self._disconnect_cb = cb

    def _claim_vendor_interface(self) -> Optional[int]:
        """Claim the vendor-specific (class 0xFF) interface that owns the bulk endpoints."""
        for intf in self.dev.get_active_configuration():
            if intf.bInterfaceClass != 0xFF:
                continue
            try:
                if self.dev.is_kernel_driver_active(intf.bInterfaceNumber):
                    self.dev.detach_kernel_driver(intf.bInterfaceNumber)
            except (NotImplementedError, usb.core.USBError):
                pass
            usb.util.claim_interface(self.dev, intf.bInterfaceNumber)
            self._discover_bulkout(intf)
            return intf.bInterfaceNumber
        return None

    def _discover_bulkout(self, intf) -> None:
        """Map DMA channels to bulk-OUT endpoints: out_pipe is the interface's bulk-OUT endpoint
        addresses in order; the H2C channel uses out_pipe[bulkout_id[DMA_H2C]]. [SRC] usb.c:1030-1056,
        rtw8922au.c:27 (bulkout_id[DMA_H2C]=2)."""
        out_pipe = [ep.bEndpointAddress for ep in intf
                    if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_OUT
                    and usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK]
        if len(out_pipe) > BULKOUT_ID_H2C:
            self._h2c_ep = out_pipe[BULKOUT_ID_H2C]
        if len(out_pipe) > BULKOUT_ID_B0MG:
            self._mgmt_ep = out_pipe[BULKOUT_ID_B0MG]     # B0MG queue = out_pipe[0]. rtw8922au.c:23
        # in_pipe[0] is the RX pipe. [SRC] usb.c:1030-1043, 543.
        in_pipe = [ep.bEndpointAddress for ep in intf
                   if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN
                   and usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK]
        if in_pipe:
            self._bulk_in_ep = in_pipe[0]

    async def _p(self, progress_cb: Optional[ProgressCallback], pct: float, msg: str) -> None:
        """Report a bring-up step and yield so the UI loop can repaint (connect() runs its heavy
        synchronous phases on the loop thread)."""
        logger.info("RTL8922AU connect [%3d%%] %s", int(pct * 100), msg)
        if progress_cb:
            progress_cb(pct, msg)
        await asyncio.sleep(0)

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """Public bring-up. Claims the vendor USB interface (a PyUSB action with no wire bytes, so it
        never appears in a capture), then runs _bringup(), which is everything that DOES appear on
        the wire. verify_pcap drives _bringup() directly, so it exercises the driver's real bring-up
        ordering minus the interface claim. [SRC] usb.c rtw89_usb_probe."""
        await self._p(progress_cb, 0.02, "Claiming USB interface")
        iface = self._claim_vendor_interface()
        logger.info("RTL8922AU: claimed vendor interface %s", iface)
        return await self._bringup(progress_cb)

    async def _bringup(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """The cold-boot bring-up exactly as it appears in the capture: USB mode-switch check, chip
        version, MAC power-on, firmware download, efuse/phycap, MAC/BB/RF init, RFK, monitor up.
        Everything here emits wire bytes; verify_pcap replays against this. [SRC] core.c
        rtw89_read_chip_ver, rtw89_core_start."""
        # On USB 2 this re-enumerates the card and re-acquires the handle (a few seconds).
        await self._p(progress_cb, 0.05, "USB mode switch")
        self._switch_usb_mode()
        await self._p(progress_cb, 0.10, "Reading chip version")
        ver = mac.read_chip_ver(self.transport)
        self.transport.cv = ver["cv"]
        logger.debug("RTL8922AU: cv=0x%x acv=0x%x cid=0x%x aid=0x%x",
                    ver["cv"], ver["acv"], ver["cid"], ver["aid"])
        await self._p(progress_cb, 0.15, "MAC power-on")
        mac.mac_pwr_on(self.transport, ver["cv"])
        # rtw89_chip_info_setup continues: wait_firmware_completion + fw_recognize are file-side
        # (no wire ops), then chip_efuse_info_setup -> mac_partial_init. [SRC] core.c:7367-7423.
        await self._p(progress_cb, 0.25, "Firmware download + DMAC init")
        mac.partial_init(self.transport, self._h2c_ep, ver["cv"])
        # chip_efuse_info_setup continues after partial_init: dump the logical efuse + phycap.
        # [SRC] core.c:7268-7291.
        await self._p(progress_cb, 0.35, "Reading efuse + PHY caps")
        efuse = mac.parse_efuse_map(self.transport, ver["cv"])
        self.mac_address = ":".join(f"{b:02x}" for b in efuse["mac_addr"])
        mac.parse_phycap_map(self.transport, ver["cv"])
        mac.setup_phycap(self.transport)          # H2C phy-capability query to the running fw
        # chip_info_setup's out: path powers the MAC back off. rtw89_core_start re-powers it.
        # [SRC] core.c:7419-7422.
        mac.mac_pwr_off(self.transport)
        # rtw89_core_register_hw tail: the rfkill GPIO polling init closes out probe.
        # [SRC] core.c:7582.
        mac.rfkill_polling_init(self.transport)
        # Interface-up path: rtw89_ops_start -> rtw89_core_start -> rtw89_mac_preinit (the second
        # pwr_on, then mac_func_en). [SRC] core.c:6626-6635, mac.c:4341-4357.
        await self._p(progress_cb, 0.45, "MAC re-power + init")
        mac.mac_preinit(self.transport, ver["cv"])
        # phy_init_bb_afe applies a firmware AFE table; this card ships no afe element, so it is a
        # no-op. Then rtw89_mac_init: partial_init(include_bb=True). [SRC] core.c:6640-6648, phy.c:1968.
        mac.mac_init(self.transport, self._h2c_ep, ver["cv"])
        # core_start resumes after mac_init: btc_ntfy_poweron + chip_reset_bb_rf are no-ops on BE,
        # then phy_init_bb_reg writes the firmware BB register tables. [SRC] core.c:6648-6659.
        await self._p(progress_cb, 0.60, "BB/RF register init")
        phy.init_bb_reg(self.transport, ver["cv"])
        phy.chip_bb_postinit(self.transport)      # rtw8922a_bb_postinit PHY_0+PHY_1. core.c:6660
        phy.init_rf_reg(self.transport, self._h2c_ep, ver["cv"])   # RF radio tables. core.c:6662
        coex.ntfy_init(self.transport, self._h2c_ep, ver["cv"])    # btc_ntfy_init. core.c:6664
        phy.dm_init(self.transport, ver["cv"])    # phy_dm_init BB inits (pre-RFK). core.c:6665
        phy.rfk_hw_init(self.transport)           # chip_rfk_hw_init (syn/ktbl/pll). phy.c:8256
        phy.init_rf_nctl(self.transport, ver["cv"])   # preinit + RF_NCTL fw table. phy.c:8257
        # rfk_init is software-only. Then set_txpwr_ctrl + power_trim + cfg_txrx_path. phy.c:8259-8262.
        phy.set_txpwr_ctrl(self.transport)
        phy.power_trim(self.transport)
        phy.bb_cfg_txrx_path(self.transport)
        # core_start tail: edcca-bands (8922A no-op), ppdu/phy-rpt/rts band cfgs, rfk_init_late.
        # [SRC] core.c:6667-6685.
        mac.cfg_ppdu_status_bands(self.transport)
        mac.cfg_phy_rpt_bands(self.transport)
        mac.update_rts_threshold(self.transport)
        # Start the bulk-IN reader BEFORE rfk_init_late so the firmware's RFK completion C2H reports
        # (pkt_type=10) are received and the per-step waits land. The reader signals RFK completions
        # on its own thread (_scan_rfk_c2h), so this works even while connect() blocks the loop.
        self._start_rx_reader()
        await self._p(progress_cb, 0.75, "RF calibration (RFK)")
        rfk.rfk_init_late(self.transport, self._h2c_ep)
        # core_start tail: btc radio-state WL_ON re-runs btc_init_cfg, then fw_log (disabled here);
        # init_ba_cam + tas_fw_timer are no-ops at cold boot. [SRC] core.c:6687-6690.
        coex.ntfy_radio_state_wl_on(self.transport, ver["cv"])
        firmware.h2c_fw_log(self.transport, self._h2c_ep, enable=False)
        # mac80211 add-interface (airmon-ng monitor vif): rtw89_mac_vif_init -> port_update
        # (port-config regs) then the H2C burst, plus btc_ntfy_role_info. [SRC] mac.c:5044.
        await self._p(progress_cb, 0.90, "Monitor interface up")
        mac.port_update(self.transport)
        ep = self._h2c_ep
        firmware.h2c_macid_pause(self.transport, ep, sh=0, grp=0, pause=False)  # set_macid_pause(false)
        firmware.h2c_role_maintain(self.transport, ep, macid=0, wifi_role=RTW89_WIFI_ROLE_MONITOR)
        firmware.h2c_join_info(self.transport, ep, macid=0, wifi_role=RTW89_WIFI_ROLE_MONITOR,
                               dis_conn=True)
        firmware.h2c_cam(self.transport, ep)                    # rtw89_cam_init is software-only
        firmware.h2c_default_cmac_tbl(self.transport, ep, macid=0)
        firmware.h2c_default_dmac_tbl(self.transport, ep, macid=0)
        # __rtw89_ops_add_iface_link tail: btc_ntfy_role_info(BTC_ROLE_START). [SRC] mac80211.c:154.
        coex.ntfy_role_info(self.transport, ep)
        await self._p(progress_cb, 1.00, f"Online ({self.mac_address})")
        return True

    def _start_rx_reader(self) -> None:
        if self._bulk_in_ep is None:
            logger.warning("RTL8922AU: no bulk-IN endpoint; RX disabled")
            return
        self._rx_reader = RxReaderThread(
            asyncio.get_running_loop(), self._rx_read_once, self._rx_dispatch,
            name="rtl8922au-rx",
            on_fatal=lambda e: self._disconnect_cb(e) if self._disconnect_cb else None,
        )
        # The reader IS the RFK-report C2H receiver, so the per-step RFK waits are only meaningful
        # once it runs. verify_pcap never starts it, so those waits stay no-ops there.
        self.transport.rfk_wait.enabled = True
        self._rx_reader.start()

    def _rx_read_once(self) -> Optional[bytes]:
        """One blocking bulk-IN read; None on a benign timeout. Runs on the reader thread. While an
        RFK offload is pending, scan the buffer for the firmware's completion C2H here (on the reader
        thread, off the loop) so the tuner's wait lands even while the loop is blocked in connect()."""
        try:
            buf = bytes(self.dev.read(self._bulk_in_ep, RX_RECVBUF_SZ, 100))
        except usb.core.USBError as e:
            err = getattr(e, "errno", None)
            if err in (110, 10060) or "timeout" in str(e).lower():
                return None
            raise
        if self.transport.rfk_wait.armed:
            self._scan_rfk_c2h(buf)
        return buf

    def _scan_rfk_c2h(self, buf: bytes) -> None:
        """Reader-thread scan for the firmware RFK-report C2H (OUTSRC / RFK_REPORT / STATE) and
        signal the pending tuner wait with the report's state byte. [SRC] phy.c:4093
        rtw89_phy_c2h_rfk_report_state, fw.h:5254 rtw89_c2h_rfk_report (state at byte 8)."""
        for pkt_type, payload, _rssi in iter_bulk_frames(buf):
            if pkt_type != RX_TYPE_C2H:
                continue
            if parse_c2h_hdr(payload) == (2, 0x9, 0) and len(payload) >= 9:  # OUTSRC, RFK_REPORT, STATE
                self.transport.rfk_wait.signal(payload[8])

    def _rx_dispatch(self, buf: bytes) -> None:
        """Split a bulk buffer into MPDUs, tally ACKs, parse the rest to the RX callback. C2H
        firmware reports are handled on the reader thread (_scan_rfk_c2h), not here."""
        cb = self._rx_cb
        if not cb and not self._ack_detect_on:
            return
        for pkt_type, mpdu, rssi in iter_bulk_frames(buf):
            if pkt_type != RX_TYPE_WIFI:
                continue
            if len(mpdu) == 10 and mpdu[0] == 0xD4:       # an ACK control frame
                self.record_ack(mpdu)
                continue
            if not cb:
                continue
            parsed = WlanFrameParser.parse_80211_frame(mpdu, rssi if rssi is not None else -100)
            if parsed:
                try:
                    cb(parsed)
                except Exception:                          # noqa: BLE001
                    logger.exception("RTL8922AU RX callback raised")

    def _switch_usb_mode(self) -> None:
        """rtw89_usb_switch_mode: SuperSpeed (USB 3 / USB-C) needs no switch; USB 2 runs the
        BE mode switch. [SRC] usb.c:1172-1189."""
        if getattr(self.dev, "speed", None) == _LIBUSB_SPEED_SUPER:
            return
        self._switch_mode_be()

    def _switch_mode_be(self) -> None:
        """rtw89_usb_switch_mode_be: read PAD_CTRL2; return if already switched (a USB 2 port that ran
        this before), else force the mode switch. On USB 2 the force-write RE-ENUMERATES the card
        (new address, same VID:PID), killing this handle, so re-acquire it before connect() continues.
        [SRC] usb.c:1143-1170."""
        pad = self.transport.read32(R_BE_PAD_CTRL2)
        if mac.field_get(B_BE_MATCH_CNT, pad) == USB_SWITCH_DELAY:
            return
        pad = (pad & ~B_BE_MATCH_CNT) | mac.field_prep(B_BE_MATCH_CNT, USB_SWITCH_DELAY)
        pad |= (B_BE_RSM_EN_V1 | B_BE_NO_PDN_CHIPOFF_V1
                | B_BE_USB_AUTO_INSTALL_MASK | B_BE_USB23_SW_MODE)
        pad &= ~(B_BE_USB3_FORCE | B_BE_USB2_FORCE | B_BE_FORCE_U3_CK | B_BE_FORCE_U2_CK
                 | B_BE_FORCE_CLK_U2 | B_BE_USB3_GEN_MODE | B_BE_USB3_LANE_MODE)
        old_addr = getattr(self.dev, "address", None)
        self.transport.write32_quiet(R_BE_PAD_CTRL2, pad)   # triggers the re-enumeration
        self._reacquire_after_reenum(old_addr)

    def _reacquire_after_reenum(self, old_addr: Optional[int]) -> None:
        """The mode-switch write re-enumerated the card, so the current handle is dead. Release it,
        wait for the card to re-appear (same VID:PID, usually a new address), verify it answers as the
        8922A, and rebuild the transport + interface claim on the fresh handle. Mirrors the kernel
        re-probe; ar9271_v2 does the same after its firmware re-enum."""
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:                                   # noqa: BLE001
            pass
        backend = libusb_package.get_libusb1_backend()
        for i in range(40):                                 # up to ~10 s for the re-enum to settle
            time.sleep(0.25)
            dev = usb.core.find(idVendor=self._vid, idProduct=self._pid, backend=backend)
            if dev is None:
                continue
            # Skip the stale pre-switch device (same address) for the first ~2 s so the re-enum can
            # move it; after that accept a same-address device (address reuse / no-op switch).
            if getattr(dev, "address", None) == old_addr and i < 8:
                continue
            try:
                t = RTL8922AUTransport(dev)
                if mac.field_get(B_BE_HW_ID_MASK, t.read32(R_BE_SYS_CHIPINFO)) != 0x71:
                    continue                                # present but not answering as 8922A yet
            except usb.core.USBError:
                continue
            self.dev, self.transport = dev, t
            self._claim_vendor_interface()                  # detach kernel + claim + rediscover eps
            logger.info("RTL8922AU: re-acquired after mode-switch re-enum (addr %s -> %s)",
                        old_addr, getattr(dev, "address", None))
            return
        raise BringUpError("re-enumeration",
                           "card did not re-appear after the USB mode switch; please replug")

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """One monitor hop = rtw89_chip_rfk_channel's prehdl double-tune. First a forced-PHY_0 pass
        (MLO_2_PLUS_0_1RF) so the per-channel RFK calibrates the active path, then a pass with the
        force cleared (MLO_1_PLUS_1_1RF), which is the operating state: both BB/RF chains up. Ending
        in 1+1 keeps PHY_1's RX chain on (the ~2x beacon yield). The prehdl double-tune is active
        because airmon-ng removes the station vif, nulling pure_monitor_mode_vif (mon=false). The
        driver derives both modes from the modelled entity force; it is not handed them. Runs off the
        event loop (each pass blocks on firmware RFK completions). [SRC] core.c:489-513."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._tune_hop, channel)
        self._band_is_2g = channel <= 14                  # picks the mgmt TX basic rate (CCK1 vs OFDM6)
        return True

    def _tune_hop(self, channel: int) -> None:
        """The two set_channel passes of one hop, off the event loop: reset the entity force to PHY_0,
        then two passes -> 2+0 then 1+1 (ends 1+1, both chains). [SRC] core.c:501-512."""
        self._prehdl_force_phy0 = True
        self._tune_pass(channel)          # forced PHY_0 -> 2+0 (+ RFK calibrates the active path)
        self._tune_pass(channel)          # force cleared -> 1+1 (operating state)

    def _tune_pass(self, channel: int) -> None:
        """One rtw89_set_channel pass. Derives the MLO mode from the modelled entity force
        (rtw89_entity_sel_mlo_dbcc_mode): forced PHY_0 -> MLO_2_PLUS_0_1RF, cleared ->
        MLO_1_PLUS_1_1RF; then clears the force, as rtw89_chip_rfk_channel does after the RFK pass.
        No wire peek. verify drives this per pass so the mac80211 monitor-setup ops can interleave
        between the two passes as they do on the wire. [SRC] core.c:501-512, chan.c:490-506."""
        mlo_1_1 = not self._prehdl_force_phy0       # forced PHY_0 => 2+0 (False); cleared => 1+1 (True)
        chan.set_channel(self.transport, channel, self._h2c_ep, mlo_1_1=mlo_1_1)
        self._prehdl_force_phy0 = not self._prehdl_force_phy0

    def configure_filter(self) -> None:
        """rtw89_ops_configure_filter tail: write the monitor RX-filter policy to both MACs (dbcc_en).
        The driver derives the value (DEFAULT_MON_RX_FLTR) from the pure-monitor filter flags rather
        than being handed it; set_rx_fltr's RMW re-adds each MAC's MPDU-max-len. [SRC] mac80211.c:388."""
        mac.set_rx_fltr(self.transport, 0, DEFAULT_MON_RX_FLTR)
        mac.set_rx_fltr(self.transport, 1, DEFAULT_MON_RX_FLTR)

    def config_monitor(self) -> None:
        """rtw89_ops_config on a CONF_CHANGE_MONITOR change: re-run physts parsing with monitor IEs
        for both PHYs. [SRC] mac80211.c:109."""
        phy.physts_parsing_init(self.transport, monitor=True)

    def dm_watchdog(self) -> None:
        """One firing of the periodic DM watchdog (rtw89_track_work): the env-monitor / dig / edcca
        tracking that runs on a timer while monitoring. [SRC] core.c:5473."""
        phy.dm_watchdog(self.transport)

    async def close(self) -> None:
        # Stop the reader before releasing USB: it is still calling dev.read() until stopped.
        if self._rx_reader is not None:
            await self._rx_reader.stop()
            self._rx_reader = None
        if self.dev is not None:
            usb.util.dispose_resources(self.dev)

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Prepend the BE mgmt TX descriptor and bulk-OUT the frame once on the B0MG pipe (0x05)."""
        if self._mgmt_ep is None:
            logger.error("RTL8922AU inject_frame: no mgmt bulk-OUT endpoint (not connected?)")
            return False
        desc = tx.build_tx_desc_mgmt(frame_bytes, band_is_2g=self._band_is_2g)
        payload = desc + frame_bytes
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.transport.bulk_out(self._mgmt_ep, payload))
            return True
        except usb.core.USBError as e:
            logger.error("RTL8922AU inject_frame: bulk-OUT failed: %s", e)
            return False

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        """Write an incrementing 12-bit sequence into the frame's seq_ctrl (BE has no HW seq
        override), so the on-air seq matches the descriptor's WIFI_SEQ. [SRC] core.c:1255."""
        if len(frame_bytes) < 24:
            return frame_bytes
        self._tx_seq = (self._tx_seq + 1) & 0xFFF
        b = bytearray(frame_bytes)
        b[22:24] = ((self._tx_seq << 4) & 0xFFFF).to_bytes(2, "little")
        return bytes(b)

    async def _enable_rx_acks(self) -> None:
        """No-op: monitor mode already admits the AP's ACKs. rx_fltr_init sets R_BE_CTRL_FLTR to
        RX_FLTR_FRAME_TO_HOST (all control subtypes to host) and RX_FLTR_OPT has no ACK-drop bit,
        so FC=0xD4 frames reach RX. The base arms the tally. [SRC] mac.c rx_fltr_init_be."""
        return

    async def _disable_rx_acks(self) -> None:
        """Matches _enable_rx_acks: nothing to undo (monitor always admits ACKs)."""
        return

    async def enter_active_monitor(self, mac: bytes,
                                   bssid: Optional[bytes] = None) -> bytes:
        """Arm HW auto-ACK for ``mac`` by programming it as addr-cam entry 0's SMA (the RX addr1 the
        responder auto-ACKs). Programming the SMA is the whole trigger: the card keeps monitoring all
        traffic and ACKs only this MAC (bench-confirmed). TMA/bssid-cam = ``bssid`` (exact match) or
        match-all when no peer is given. Return the MAC armed. [SRC] cam.c:819."""
        if self._h2c_ep is None:
            raise RuntimeError("RTL8922AU enter_active_monitor: not connected")
        sma = bytes(mac)
        tma = bytes(bssid) if bssid is not None else b"\x00" * 6
        bssid_mask = RTW89_BSSID_MATCH_ALL if bssid is None else 0
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: firmware.h2c_addr_cam(
                self.transport, self._h2c_ep, sma=sma, tma=tma,
                net_type=RTW89_NET_TYPE_NO_LINK, bssid=tma, bssid_mask=bssid_mask))
        self._active_mac = sma
        return sma

    async def exit_active_monitor(self) -> None:
        """Re-zero the addr-cam SMA (the connect-time h2c_cam baseline) so the responder stops
        auto-ACKing."""
        if self._h2c_ep is None:
            return
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: firmware.h2c_cam(self.transport, self._h2c_ep))
        self._active_mac = None
