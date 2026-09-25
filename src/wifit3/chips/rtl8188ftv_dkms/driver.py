"""RTL8188FTV DKMS (vendor) driver — no-name 0bda:f179 dongles.

Cleanroom port of ``kelebek333/rtl8188fu`` (``v4.3.23.6_20964.20170110``,
RTL871X stack). Cold bring-up only (a replug resets the chip; no warm
path yet). Milestone map and open items in ``RTL8188FTV_DKMS.md``;
``verify_pcap.py`` replays the control flow byte-for-byte.

    connect()
      ├─ claim USB interface
      ├─ M1 probe (chip version) + M2 EFUSE parse
      ├─ M3 power on + M4 FW download (#2, open)
      ├─ M5a-f (MAC/BB/RF/queues/misc/ch1 tune + TX power)
      ├─ M5h (CAM/MISC11/GPIO) + DM-init + LC + IQK + thermal trigger
      ├─ station opmode + monitor entry
      ├─ start RxReaderThread (bulk-IN pump)
      └─ start DM watchdog thread (2 s tick, lock-serialized)

``set_channel`` reuses the verified switch unit with hal remnants.
Injection sends MGMT/DATA frames (``tx.inject_frame``, monitor template)
over bulk-OUT; control frames raise. A 2 s watchdog thread
(``dm.watchdog_tick``: FA + DIG + adaptivity + CCK-PD + RA retry +
thermal, lock-serialized with channel/inject) keeps gain and TX power
tracking live; ``WIFIT3_RTL8188FTV_WATCHDOG=off`` disables it.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, ClassVar, List, Optional

import usb.core
import usb.util

from wifit3.chips.driver import Driver, FakeMacSupport, ProgressCallback
from wifit3.chips.rx_reader import RxReaderThread
from wifit3.dot11.parser import WlanFrameParser
from wifit3.errors import BringUpError
from wifit3.models.device_id import DeviceID

from . import bb as bb_mod
from . import c2h as c2h_mod
from . import cal as cal_mod
from . import chan as chan_mod
from . import dm as dm_mod
from . import firmware as firmware_mod
from . import info as info_mod
from . import iqk as iqk_mod
from . import llt as llt_mod
from . import mac as mac_mod
from . import misc as misc_mod
from . import mode as mode_mod
from . import power as power_mod
from . import prom as prom_mod
from . import queues as queues_mod
from . import rf as rf_mod
from . import rx as rx_mod
from . import sec as sec_mod
from . import track as track_mod
from . import tx as tx_mod
from . import txpower as txpower_mod
from .transport import Rtl8188ftvDkmsTransport

logger = logging.getLogger(__name__)

BULK_IN_EP = 0x81
OUT_EP_NUMBER = 2
OUT_EP_QUEUE_SEL = 0x05
WATCHDOG_PERIOD_S = 2.0


class Rtl8188ftvDkmsDriver(Driver):
    SUPPORTED_CHANNELS: ClassVar[List[int]] = list(range(1, 14))
    FAKE_MAC: ClassVar[FakeMacSupport] = FakeMacSupport.NONE

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "Rtl8188ftvDkmsDriver":
        return cls(dev)

    def __init__(self, dev: usb.core.Device):
        super().__init__()
        self.dev = dev
        self.transport = Rtl8188ftvDkmsTransport(dev)
        self.hal: dict = {}
        self.params = None
        self.by_rate = None
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self.current_channel: int = 1
        self._rx_callback: Optional[Callable] = None
        self._on_lost: Optional[Callable] = None
        self._rx_reader = None
        self._claimed: bool = False
        self._c2h_count: int = 0
        self._io_lock = asyncio.Lock()
        self._watchdog_task = None

    def register_rx_callback(self, cb: Callable) -> None:
        self._rx_callback = cb

    def register_disconnect_callback(self, cb: Callable) -> None:
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
            logger.info("RTL8188FTV DKMS cold bring-up")
            ok = await self._cold_bring_up(_update)
            if not ok:
                return False
            self._rx_reader = RxReaderThread(
                loop, self._rx_read_once, self._rx_dispatch,
                name="rtl8188ftv-dkms-rx",
                on_fatal=lambda e: self._on_lost and self._on_lost(e),
            )
            self._rx_reader.start()
            self.is_warm = True
            if os.environ.get("WIFIT3_RTL8188FTV_WATCHDOG") != "off":
                self._watchdog_task = loop.create_task(self._watchdog_loop())
            else:
                logger.info("RTL8188FTV DKMS watchdog disabled "
                            "(gain/tracking frozen at bring-up values)")
            _update(1.00, "RTL8188FTV DKMS online.")
            return True
        except Exception as e:
            raise BringUpError("bring-up", str(e)) from e

    async def _cold_bring_up(self, _update) -> bool:
        t = self.transport
        loop = asyncio.get_running_loop()
        hal = self.hal

        if await loop.run_in_executor(None, power_mod.is_chip_warm, t):
            mcufwdl, cr = await loop.run_in_executor(
                None, power_mod.warm_state, t)
            logger.warning(
                "chip already initialized (warm state: MCUFWDL=0x%02x, "
                "CR=0x%04x); attempting cold bring-up over it, replug "
                "if the scanner stays empty", mcufwdl, cr)

        _update(0.05, "Probing chip version + EFUSE...")
        version = await loop.run_in_executor(None, info_mod.read_chip_version, t)
        smic = info_mod.is_smic(version)
        params, _table = await loop.run_in_executor(
            None, prom_mod.read_adapter_info, t, smic)
        if (params.vid, params.pid) != (0x0BDA, 0xF179):
            raise BringUpError("probe", f"unexpected VID:PID {params.vid:04x}:{params.pid:04x}")
        self.params = params
        self.mac_address = params.mac.hex(":")
        hal.update({"cur_cck": 0, "th_l2h_ini": 0xF5,
                    "adaptivity_ability": False, "tm_trigger": False,
                    "channel": 1, "mgnt_seq": 0})
        hal.update(track_mod.tracking_init_state(params.thermal))
        hal["params"] = params

        _update(0.07, "Probe power on...")
        if not await loop.run_in_executor(None, power_mod.power_on, t):
            raise BringUpError("power", "probe power_on failed")

        _update(0.08, "Probe firmware + hidden report...")
        blob = firmware_mod.load_firmware_blob()
        ver = await loop.run_in_executor(
            None, firmware_mod.download_firmware, t, blob)
        if ver != (4, 0, 0x88F1):
            raise BringUpError("fw", f"unexpected probe version {ver}")
        await loop.run_in_executor(None, c2h_mod.request_hidden_report, t)
        ident, report = await loop.run_in_executor(
            None, c2h_mod.collect_hidden_report, t)
        logger.info("Probe hidden report: id 0x%02x, %dB (%s)",
                    ident, len(report), report.hex())
        if not await loop.run_in_executor(None, power_mod.card_disable,
                                          t, False):
            raise BringUpError("power", "probe power_off failed")

        _update(0.10, "Power on...")
        if not await loop.run_in_executor(None, power_mod.power_on, t):
            raise BringUpError("power", "power_on failed")

        _update(0.15, "Downloading firmware...")
        await loop.run_in_executor(None, power_mod.check_powered, t)
        if not await loop.run_in_executor(None, llt_mod.init_llt, t):
            raise BringUpError("llt", "init_llt failed")
        await loop.run_in_executor(None, llt_mod.enable_tx_report, t)
        ver = await loop.run_in_executor(
            None, firmware_mod.download_firmware, t, blob)
        if ver != (4, 0, 0x88F1):
            raise BringUpError("fw", f"unexpected version {ver}")
        self.by_rate = txpower_mod.load_default_pg_tables()
        hal["by_rate"] = self.by_rate

        _update(0.30, "MAC/BB/RF init...")
        await loop.run_in_executor(None, mac_mod.init_antenna_selection, t)
        await loop.run_in_executor(None, mac_mod.mac_config, t)
        await loop.run_in_executor(
            None, bb_mod.bb_config, t, params.crystal)
        await loop.run_in_executor(None, rf_mod.rf_config, t)

        _update(0.45, "Queues/misc/ch1 tune...")
        await loop.run_in_executor(
            None, queues_mod.init_queue_reserved_page, t, OUT_EP_QUEUE_SEL)
        await loop.run_in_executor(None, queues_mod.init_tx_buffer_boundary, t)
        await loop.run_in_executor(
            None, queues_mod.init_queue_priority, t, OUT_EP_NUMBER,
            OUT_EP_QUEUE_SEL)
        await loop.run_in_executor(None, queues_mod.init_page_boundary, t)
        await loop.run_in_executor(None, queues_mod.init_transfer_page_size, t)
        await loop.run_in_executor(None, queues_mod.init_driver_info_size, t)
        await loop.run_in_executor(None, queues_mod.init_macaddr, t, params.mac)
        await loop.run_in_executor(None, queues_mod.init_network_type, t)
        await loop.run_in_executor(None, queues_mod.init_wmac_setting, t)
        await loop.run_in_executor(None, queues_mod.init_adaptive_ctrl, t)
        await loop.run_in_executor(None, queues_mod.init_edca, t)
        await loop.run_in_executor(None, misc_mod.init_beacon_params, t, hal)
        await loop.run_in_executor(None, misc_mod.init_burst, t)
        await loop.run_in_executor(None, misc_mod.agg_tx_update, t)
        await loop.run_in_executor(None, misc_mod.agg_rx_update, t)
        await loop.run_in_executor(None, misc_mod.init_hw_led, t)
        await loop.run_in_executor(None, misc_mod.drop_incorrect_bulk_out, t)
        await loop.run_in_executor(None, misc_mod.mcast2uni_lifetime, t)
        await loop.run_in_executor(None, misc_mod.turn_on_block, t)
        hal["rf_chnl_val"] = await loop.run_in_executor(
            None, chan_mod.tune_20, t, 1, 0, hal)
        await loop.run_in_executor(
            None, txpower_mod.set_level, t, 1, 0, params, self.by_rate)

        _update(0.65, "CAM/DM-init/LC/IQK...")
        await loop.run_in_executor(None, sec_mod.invalidate_cam_all, t)
        await loop.run_in_executor(None, misc_mod.misc11_tail, t)
        await loop.run_in_executor(None, misc_mod.init_gpio_setting, t)
        await loop.run_in_executor(None, dm_mod.common_info_self_init, t)
        hal["cur_ig"] = await loop.run_in_executor(
            None, dm_mod.dig_init_igi, t) & 0xFF
        await loop.run_in_executor(None, dm_mod.nhm_init, t)
        await loop.run_in_executor(None, dm_mod.adaptivity_init, t)
        await loop.run_in_executor(None, dm_mod.cfo_init_atc, t)
        await loop.run_in_executor(None, dm_mod.thermal_swing_index, t)
        await loop.run_in_executor(None, dm_mod.tracking_init_second, t)
        await loop.run_in_executor(None, cal_mod.lc_calibrate, t)
        hal["iqk"] = await loop.run_in_executor(None, iqk_mod.iq_calibrate, t)
        final = hal["iqk"]["final"]
        if final != 0xFF:
            hal["iqk_x"], hal["iqk_y"] = hal["iqk"]["result"][final][:2]
        else:
            hal["iqk_x"], hal["iqk_y"] = 0, 0
        await loop.run_in_executor(None, track_mod.thermal_trigger, t)
        hal["tm_trigger"] = True

        _update(0.75, "hal_init tail + mlme ch1...")
        await loop.run_in_executor(None, misc_mod.hal_init_tail, t)
        await loop.run_in_executor(
            None, chan_mod.switch_channel, t, 1, hal, params,
            self.by_rate, 0, 0)

        _update(0.85, "Station opmode + monitor entry...")
        await loop.run_in_executor(None, mode_mod.set_station_opmode, t, hal)
        await loop.run_in_executor(None, track_mod.kfree_gain_offset, t)
        await loop.run_in_executor(None, mode_mod.enter_monitor, t)
        self.current_channel = 1
        hal["channel"] = 1
        return True

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        loop = asyncio.get_running_loop()
        try:
            async with self._io_lock:
                await loop.run_in_executor(
                    None, chan_mod.switch_channel, self.transport, channel,
                    self.hal, self.params, self.by_rate, self.hal.get("rem_cck", 0),
                    self.hal.get("rem_ofdm", 0))
            self.current_channel = channel
            return True
        except (ValueError, IOError):
            logger.exception("set_channel(%d) failed", channel)
            return False

    async def _watchdog_loop(self) -> None:
        """Periodic DM watchdog: FA + DIG + adaptivity + CCK-PD + RA retry +
        thermal trigger/callback, every ``WATCHDOG_PERIOD_S``. Serialized
        with set_channel/_inject_frame via ``_io_lock`` (shared hal: cur_ig,
        cur_cck, remnants, tm_trigger). The hal seeds carry bring-up state
        (no re-reads: the vendor does not re-read at tick start either). A
        per-tick fault skips the tick, never the loop."""
        loop = asyncio.get_running_loop()
        try:
            while True:
                await asyncio.sleep(WATCHDOG_PERIOD_S)
                try:
                    async with self._io_lock:
                        fa = await loop.run_in_executor(
                            None, dm_mod.watchdog_tick,
                            self.transport, self.hal)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.debug("RTL8188FTV DKMS watchdog: tick skipped",
                                 exc_info=True)
                    continue
                logger.debug("RTL8188FTV DKMS watchdog: fa=%d rem=(%+d,%+d)",
                             fa["all"], self.hal.get("rem_cck", 0),
                             self.hal.get("rem_ofdm", 0))
        except asyncio.CancelledError:
            pass

    def _rx_read_once(self) -> bytes | None:
        return self.transport.bulk_in(BULK_IN_EP)

    def _rx_dispatch(self, buf: bytes) -> None:
        callback = self._rx_callback
        for attrib, payload in rx_mod.iter_rx(buf):
            if attrib["c2h"]:
                self._c2h_count += 1
                continue
            if payload[0] == 0xD4 and len(payload) in (10, 14):
                self.record_ack(payload)   # 14 = 10-byte ACK + appended FCS (monitor RCR BIT31)
                continue
            if callback is None:
                continue
            try:
                parsed = WlanFrameParser.parse_80211_frame(
                    payload, attrib["rssi"]
                    if attrib["rssi"] is not None else -100)
            except Exception:  # noqa: BLE001
                continue
            if parsed is not None:
                callback(parsed)

    async def close(self) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None
        if self._rx_reader is not None:
            await self._rx_reader.stop()
            self._rx_reader = None
        self._release_usb()

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Send one pre-stamped MGMT/DATA frame via ``tx.inject_frame``
        (monitor template, descriptor sequence follows the frame;
        ``mgnt_seq`` advances). Control frames raise."""
        loop = asyncio.get_running_loop()
        try:
            async with self._io_lock:
                await loop.run_in_executor(
                    None, tx_mod.inject_frame,
                    self.transport, self.hal, bytes(frame_bytes))
        except (ValueError, IOError):
            logger.exception("inject failed")
            return False
        return True

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        if len(frame_bytes) >= 24:
            return tx_mod.stamp_seqnum(bytes(frame_bytes),
                                       self.hal.get("mgnt_seq", 0))
        return bytes(frame_bytes)

    async def _enable_rx_acks(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, rx_mod.admit_ack_frames, self.transport)

    async def _disable_rx_acks(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, rx_mod.drop_ack_frames, self.transport)

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
