"""RTL8821AU / RTL8811AU driver — vendor (Lucid-Duck DKMS) cleanroom port.

Status: 2.4 GHz monitor RX complete (M1–M5). ``connect()`` runs the deterministic
bring-up — firmware download (M1) -> MAC init (M2) -> BB/RF init (M3) -> 2.4 GHz
channel tune (M4) -> the post-tune hal_init tail + phydm InitHalDm DIG/AGC/EDCCA
seed (M5 §1/§2) -> monitor opmode entry (M5 §3) — then starts the bulk-IN RX reader
(promiscuous monitor frames + per-frame 8821a RSSI) and the runtime phydm DIG/AGC
watchdog. All steps except the live EDCCA PSD search are pcap-verified byte-for-byte.

The RX reader is started **before** the monitor RCR write, as the vendor does: ``_netdev_open``
posts the bulk-IN URBs (os_intfs.c:4026 ``rtw_intf_start``) before monitor opmode sets RCR.

``inject_frame`` (M6) transmits one fake-descriptor frame on bulk-OUT — deauth,
fake-auth, and WEP ARP replay all ride this one path; it is explicit-action only
(passive-by-default). 2.4 GHz + 5 GHz (M7) RX/TX with EFUSE-calibrated per-rate power
are complete. Paired with the mainline ``chips/rtl8821au/`` in ``device/manager.py``
``_DKMS_BY_DIR``, ordered by ``$WIFIT3_RTL8821`` — **this DKMS port is the default**;
``=mainline`` falls back to the mainline driver. Sibling to the untouched mainline driver.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from importlib import resources
from typing import Callable, ClassVar, List, Optional

import usb.core
import usb.util

from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.dot11.parser import WlanFrameParser

from ..rx_reader import RxReaderThread
from . import bb, chan, dig, efuse, firmware, led, mac, monitor, phy_cond, rf, txpower, watchdog
from .rx import iter_frames
from .transport import RTL8821AUDkmsTransport
from .tx import DESC_RATE1M, DESC_RATE6M, RATEID_IDX_B, RATEID_IDX_G, RATEID_IDX_VHT_1SS, update_txdesc

logger = logging.getLogger(__name__)

_FW_ASSET = "rtl8821au_fw.bin"
_DEFAULT_CHANNEL = 1          # connect-time tune target (matches the cold-boot capture)
_FALLBACK_CRYSTAL_CAP = 0x20  # EEPROM default if the efuse xtal byte reads blank
# 20 MHz primary, both bands (M4 = 2.4 GHz, M7 = 5 GHz). The 5 GHz set matches the
# channels the cold-boot capture tuned (UNII-1/2/2e/3). # TODO(8812au): path-B radio.
CHANNELS_2G = list(range(1, 15))
CHANNELS_5G = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124,
               128, 132, 136, 140, 144, 149, 153, 157, 161, 165]
# Scan set excludes the DFS band (52-144): passive-scan-only, radar-shared, home APs avoid it.
# set_channel + verify_channels still drive the full CHANNELS_5G above, byte-for-byte vs the capture.
CHANNELS_5G_NON_DFS = [36, 40, 44, 48, 149, 153, 157, 161, 165]


def _load_firmware() -> bytes:
    return (resources.files(__package__) / "assets" / _FW_ASSET).read_bytes()


class Rtl8821auDkmsDriver(Driver):
    SUPPORTED_CHANNELS: ClassVar[List[int]] = CHANNELS_2G + CHANNELS_5G_NON_DFS
    FAKE_MAC = FakeMacSupport.SPOOFABLE

    def __init__(self, transport: RTL8821AUDkmsTransport):
        super().__init__()          # base owns the ACK tally (_ack_detect_on / _our_tx_macs / _ack_counts)
        self.transport = transport
        self.mac_address: Optional[str] = None   # efuse 0x107 (M-TXPWR)
        self._crystal_cap: int = _FALLBACK_CRYSTAL_CAP
        self._tx_power = None                    # efuse PathTxPwr (path A, 2.4 GHz)
        self._tx_power_5g = None                 # efuse PathTxPwr (path A, 5 GHz)
        self._bb_swing_2g: int = chan.BB_SWING_DEFAULT
        self._bb_swing_5g: int = chan.BB_SWING_DEFAULT
        # Runtime fuse-derived branch selectors (default = the reference AWUS036ACS values,
        # so a value-less construction reproduces the recorded card byte-for-byte).
        self._ext_lna_2g: bool = False           # efuse LNAType_2G[3] — RFE pinmux branch
        self._interface_sel: int = 0             # efuse board option [7:5] — LED register branch
        self._thermal_meter: int = 0xFF          # efuse thermal base (power tracking)
        self._dm: Optional[watchdog.DmState] = None
        self._jaguar_params = phy_cond.JaguarParams()  # phy_cond walker inputs (board_type)
        self._channel: Optional[int] = None
        self.is_warm: bool = False
        # Runtime DIG/AGC watchdog. Toggleable so a fixed-channel A/B can isolate the
        # watchdog's effect on RX breadth (scan_hw.py --no-dig).
        self.enable_dig: bool = True
        self._rx_cb: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._reader: Optional[RxReaderThread] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._led_task: Optional[asyncio.Task] = None
        # Serializes control-transfer batches (DIG watchdog vs set_channel) so two
        # executor threads never drive EP0 at once; the RX reader uses bulk-IN.
        self._io_lock = asyncio.Lock()

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "Rtl8821auDkmsDriver":
        return cls(RTL8821AUDkmsTransport(dev))

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_cb = cb

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        """Sink for a terminal RX-reader failure (unplug). Forwarded to the RxReaderThread's
        on_fatal; resolved at call time so registration order vs connect() can't strand it."""
        self._on_lost = cb

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        loop = asyncio.get_running_loop()
        # All bring-up is blocking synchronous USB I/O; keep it off the event loop.

        # Hold interface 0 for the session: unclaimed, WinUSB claims and releases it around every
        # EP0 transfer, and that churn intermittently fails with "Entity not found".
        await loop.run_in_executor(None, usb.util.claim_interface, self.transport.dev, 0)

        # EFUSE read (probe phase, before power-on, like the vendor): crystal_cap (AFE
        # trim) + the per-rate TX-power base/diffs + the MAC address.
        if progress_cb:
            progress_cb(0.0, "Reading EFUSE / chip parameters")
        params = await loop.run_in_executor(None, efuse.read_chip_params, self.transport)
        self.mac_address = params.mac_address
        self._crystal_cap = params.crystal_cap
        self._tx_power = params.tx_power
        self._tx_power_5g = params.tx_power_5g
        self._bb_swing_2g = params.bb_swing_2g
        self._bb_swing_5g = params.bb_swing_5g
        self._ext_lna_2g = params.ext_lna_2g
        self._interface_sel = params.interface_sel
        self._thermal_meter = params.thermal_meter
        self._jaguar_params = efuse.build_jaguar_params(params)
        logger.info("RTL8821AU efuse: valid_id=%d autoload_fail=%d ver=0x%02x crystal=0x%02x "
                    "thermal=0x%02x mac=%s cck_base[0]=0x%02x bb_swing 2g=0x%03x 5g=0x%03x",
                    params.eeprom_id_valid, params.autoload_fail, params.eeprom_version,
                    params.crystal_cap, params.thermal_meter, params.mac_address or "<blank>",
                    params.tx_power.cck_base[0], params.bb_swing_2g, params.bb_swing_5g)
        logger.info("RTL8821AU ids: vid=0x%04x pid=0x%04x customer=0x%02x/0x%02x resolved=%d "
                    "regulatory=%d iface=%d chplan=0x%02x country=%s remote_wakeup=%d usb_switch=%d",
                    params.eeprom_vid, params.eeprom_pid, params.eeprom_customer_id,
                    params.eeprom_subcustomer_id, params.customer_id, params.regulatory,
                    params.interface_sel, params.channel_plan, params.country_code or "--",
                    params.remote_wakeup, params.usb_mode_switch)
        logger.info("RTL8821AU board: pa/lna types 2g=0x%02x/0x%02x 5g=0x%02x/0x%02x "
                    "ext_pa_lna 2g=%d/%d 5g=%d/%d bt=%d ant=%d board_type=0x%02x "
                    "hidden_usb antenna=%d wmode=%d disable_11ac=%d%s",
                    params.pa_type_2g, params.lna_type_2g, params.pa_type_5g, params.lna_type_5g,
                    params.external_pa_2g, params.external_lna_2g, params.external_pa_5g,
                    params.external_lna_5g, params.bt_coexist, params.bt_ant_num, params.board_type,
                    params.usb_type_antenna, params.usb_type_wmode, params.usb_type_disable_11ac,
                    "" if params.board_type == 0 else " (untested variant: external PA/LNA/BT board)")

        if progress_cb:
            progress_cb(0.2, "Uploading firmware")
        fw = _load_firmware()
        ready = await loop.run_in_executor(None, firmware.bring_up, self.transport, fw)
        if not ready:
            raise BringUpError("firmware", "MCU never signalled ready (WINTINI_RDY timeout)")

        if progress_cb:
            progress_cb(0.6, "Configuring MAC / BB / RF + channel tune + TX power + phydm seed")

        # Deterministic init chain M2 -> M5 §2 (all pcap-verified except the live
        # EDCCA search). Keep in sync with scripts/chips/rtl8821au_dkms/verify_pcap.py.
        def _init(t):
            mac.phy_mac_config(t)                     # M2: MAC register table
            mac.mac_init_misc(t)                      # M2: queue/MISC + REG_CR
            bb.phy_bb_config(t, crystal_cap=self._crystal_cap,
                             params=self._jaguar_params)        # M3: BB PHY_REG + AGC + xtal
            rf.phy_rf_config(t, self._jaguar_params)  # M3: RadioA
            chan.set_chnl_bw(t, _DEFAULT_CHANNEL, self._bb_swing_2g,
                             self._ext_lna_2g)        # M4: 2.4 GHz tune
            txpower.set_tx_power(t, _DEFAULT_CHANNEL, self._tx_power)  # M-TXPWR: per-rate txagc
            mac.hal_init_misc_pre(t)                  # M5 §1a: security + MISC11
            seed = dig.init_hal_dm(t, search_edcca=True)  # M5 §2: phydm DIG/AGC/EDCCA seed
            mac.hal_init_misc_post(t)                 # M5 §1b: turn-on tail
            # rtw_hal_init after hal_init [SRC] hal_intf.c:609 rtw_mi_set_mac_addr,
            # :621 init_hw_mlme_ext (rtw_mlme_ext.c:1299-1300: RX BAR + set_channel_bwmode)
            if self.mac_address:
                monitor._write_mac_addr(t, bytes(int(b, 16) for b in self.mac_address.split(":")))
            # HW_VAR_ENABLE_RX_BAR [SRC] hal_com.c:13480-13483
            t.write16(monitor.REG_RXFLTMAP1, t.read16(monitor.REG_RXFLTMAP1) | (1 << 8))
            chan.set_channel_bw(t, _DEFAULT_CHANNEL, self._bb_swing_2g, self._bb_swing_5g,
                                self._ext_lna_2g)
            txpower.set_tx_power(t, _DEFAULT_CHANNEL, self._tx_power)
            return seed

        seed = await loop.run_in_executor(None, _init, self.transport)
        self._dm = watchdog.DmState(
            cur_ig_value=seed.cur_ig_value, nhm_igi=seed.nhm_igi, rrsr_val_init=seed.rrsr_val_init,
            h2l_lb=seed.h2l_lb, l2h_lb=seed.l2h_lb, eeprom_thermal=self._thermal_meter,
            default_ofdm_index=watchdog.default_ofdm_index(self._bb_swing_2g),
            bb_swing_diff_2g=watchdog.BB_SWING_DIFF[self._bb_swing_2g],
            bb_swing_diff_5g=watchdog.BB_SWING_DIFF[self._bb_swing_5g])
        self._channel = _DEFAULT_CHANNEL

        # M5 §5: start the bulk-IN RX reader BEFORE the monitor RCR opens the RX gate
        # (the vendor posts URBs before the gate, os_intfs.c:4026).
        # The reader keeps a blocking bulk read posted on a dedicated thread (off the
        # event loop, so the TUI can't starve RX); each aggregated buffer is split into
        # 802.11 frames (FCS-stripped, per-frame RSSI) and fanned to the rx callback.
        self._reader = RxReaderThread(
            loop, self._read_once, self._dispatch, name="8821au-dkms-rx",
            on_fatal=lambda e: self._on_lost and self._on_lost(e))
        self._reader.start()

        # _netdev_open: rtw_hal_init, rtw_intf_start, then rtw_led_control(LED_CTL_NO_LINK) (os_intfs.c:4033).
        self._led_task = loop.create_task(self._led_blink())

        # M5 §3: monitor opmode entry (Set_MSR NOLINK + RCR accept-all + RXFLTMAP).
        await loop.run_in_executor(None, monitor.enter_monitor, self.transport)

        # M5 §6: the 2 s dynamic-check tick (_netdev_open dynamic_chk_timer, os_intfs.c:4044).
        if self.enable_dig:
            self._watchdog_task = loop.create_task(self._watchdog())
        else:
            logger.info("RTL8821AU watchdog disabled (IGI stays at the InitHalDm seed)")

        if progress_cb:
            progress_cb(1.0, f"Tuned to channel {_DEFAULT_CHANNEL} @ 20 MHz (monitor)")
        return True

    async def _watchdog(self) -> None:
        """rtw_dynamic_check_timer_handlder: re-armed 2 s from its own fire (rtw_mlme.c:4117). Serialized via _io_lock."""
        loop = asyncio.get_running_loop()
        next_fire = loop.time() + watchdog.WATCHDOG_PERIOD_S
        try:
            while True:
                await asyncio.sleep(next_fire - loop.time())
                next_fire = loop.time() + watchdog.WATCHDOG_PERIOD_S
                async with self._io_lock:
                    await loop.run_in_executor(None, watchdog.tick, self.transport, self._dm, self._channel)
                logger.debug("RTL8821AU watchdog: IGI=0x%02x", self._dm.cur_ig_value)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — a watchdog fault must not kill RX
            logger.exception("RTL8821AU watchdog stopped on error")

    async def _led_blink(self) -> None:
        """SW_LED_MODE9 LED0 no-link slow blink: SwLedBlink9 LED_BLINK_SLOWLY (hal_usb_led.c:934)."""
        loop = asyncio.get_running_loop()
        led_on = False
        try:
            while True:
                await asyncio.sleep(led.LED_BLINK_NO_LINK_INTERVAL_ALPHA_S)
                async with self._io_lock:
                    if led_on:
                        await loop.run_in_executor(None, led.turn_off, self.transport, self._interface_sel)
                    else:
                        await loop.run_in_executor(None, led.turn_on, self.transport, self._interface_sel)
                led_on = not led_on
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — an LED fault must not kill RX
            logger.exception("RTL8821AU LED blink stopped on error")

    # --- RX path -----------------------------------------------------------
    def _read_once(self) -> Optional[bytes]:
        """Reader-thread side: one blocking bulk-IN read (None on no traffic)."""
        return self.transport.bulk_in()

    def _dispatch(self, buf: bytes) -> None:
        """Loop side: split the aggregated bulk-IN buffer into (frame, rssi) pairs and
        fan each parsed dict to the rx callback. FCS already stripped."""
        cb = self._rx_cb
        if cb is None and not self._ack_detect_on:
            return
        for frame, rssi in iter_frames(buf):
            # A 10-byte 0xD4 frame is an ACK (the parser drops control frames); the base tallies it
            # iff the ACK tap is armed and RA=frame[4:10] is a MAC we inject as.
            if len(frame) == 10 and frame[0] == 0xD4:
                self.record_ack(frame)
                continue
            if cb is not None:
                parsed = WlanFrameParser.parse_80211_frame(frame, rssi)
                if parsed is not None:
                    cb(parsed)

    async def _enable_rx_acks(self) -> None:
        """No-op: the monitor entry accept-alls RXFLTMAP1 (monitor._hw_var_set_monitor), so the
        recipient's ACK control frames already reach RX. Nothing to enable on the chip (the base
        arms the tally). Not enter_active_monitor, which makes the chip emit ACKs."""
        return

    async def _disable_rx_acks(self) -> None:
        """No-op, matching ``_enable_rx_acks``: the monitor RX filter is left untouched."""
        return

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to a 2.4 GHz or 5 GHz channel at 20 MHz primary.

        Runs the runtime tune (M7 ``set_channel_bw``: phy_SwBand switches band only on a
        2.4<->5 crossing, then channel select + BW). On a settle (``scan=False``) it also
        re-applies the per-rate txagc for the channel's band/power group, so a deauth/WEP
        run transmits at the correct EFUSE-calibrated power.

        ``scan=True`` (the channel hopper) takes the fast path: it SKIPS the ~27-write
        per-rate txagc re-apply. The vendor re-applies it every hop, but txagc is TX-only
        and wifit3 always calls ``set_channel(scan=False)`` before injecting, so skipping
        it on transient passive hops is safe and buys back dwell time for RX.
        """
        loop = asyncio.get_running_loop()

        def _tune(t):
            rrsr = chan.set_channel_bw(t, channel, self._bb_swing_2g, self._bb_swing_5g,
                                       self._ext_lna_2g)
            if rrsr is not None:
                self._dm.rrsr_val_init = rrsr                   # rtw_phydm_set_rrsr (hal_com.c:13241)
                watchdog.set_bb_swing_by_band(self._dm, channel > 14)
            watchdog.clear_txpowertracking_state(self._dm)      # phy_SwChnlAndSetBwMode8812
            if not scan:
                if channel <= 14:
                    txpower.set_tx_power(t, channel, self._tx_power)
                else:
                    txpower.set_tx_power_5g(t, channel, self._tx_power_5g)

        async with self._io_lock:   # don't race the DIG watchdog's control I/O
            await loop.run_in_executor(None, _tune, self.transport)
        self._channel = channel
        return True

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Transmit one 802.11 frame as rtw_monitor_xmit_entry does (rtw_xmit.c:4866), without a
        radiotap header. ``frame_bytes`` is the MPDU without FCS (the HW appends it)."""
        if len(frame_bytes) < 10:           # need addr1 (bytes [4:10]) to read BMC
            return False
        loop = asyncio.get_running_loop()
        # rtw_xmit.c:4973-4987: a data frame takes update_monitor_frame_attrib (1T1R raid,
        # rtw_mlme_ext.c:7639) at MGN_1M; any other takes update_mgntframe_attrib at
        # pmlmeext->tx_rate, 6M on 5 GHz (update_tx_basic_rate rtw_wlan_util.c:3212-3215).
        if len(frame_bytes) >= 24 and frame_bytes[0] & 0x0C == 0x08:
            rate_id, hw_rate = RATEID_IDX_VHT_1SS, DESC_RATE1M     # TODO(8812au): 2T2R is RATEID_IDX_VHT_2SS.
        elif self._channel > 14:
            rate_id, hw_rate = RATEID_IDX_G, DESC_RATE6M
        else:
            rate_id, hw_rate = RATEID_IDX_B, DESC_RATE1M
        payload = update_txdesc(frame_bytes, rate_id=rate_id, hw_rate=hw_rate)
        async with self._io_lock:           # don't TX mid-retune (set_channel/DIG)
            await loop.run_in_executor(None, self.transport.bulk_out, payload)
        self._dm.tx_bytes += len(frame_bytes)   # rtw_monitor_xmit_entry tx_bytes (rtw_xmit.c:5168)
        return True

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        """Realtek HW assigns the 802.11 sequence number (the txdesc sets HWSEQ_EN), so the
        frame goes out unchanged."""
        return frame_bytes

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Re-point REG_MACID to ``mac`` so the hardware HW-ACKs frames to it.
        Reversed by exit_active_monitor."""
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, monitor._write_mac_addr, self.transport, bytes(mac))
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the card's real MAC in REG_MACID."""
        if not self.mac_address:
            return
        real = bytes(int(b, 16) for b in self.mac_address.split(":"))
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, monitor._write_mac_addr, self.transport, real)

    async def close(self) -> None:
        # Stop the watchdog, the LED blink and the reader before releasing the USB handle.
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None
        if self._led_task is not None:
            self._led_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._led_task
            self._led_task = None
        if self._reader is not None:
            await self._reader.stop()
            self._reader = None
        async with self._io_lock:
            self.transport.close()
