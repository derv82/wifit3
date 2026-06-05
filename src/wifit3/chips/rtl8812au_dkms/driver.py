"""RTL8812AU / 2T2R driver — vendor (morrownr DKMS) cleanroom port.

Status: 2.4 GHz monitor RX complete and hardware-confirmed (real beacons off the
antenna). ``connect()`` runs the deterministic cold-boot bring-up exactly as the
byte-for-byte gate verifies it (``scripts/rtl8812au_dkms/verify_pcap.py``): EFUSE probe
-> firmware download -> MAC -> BB/RF (both paths) -> 2.4 GHz channel tune -> per-rate TX
power -> the phydm InitHalDm DIG/AGC/EDCCA seed (incl. the live PWDB-EDCCA search) ->
morrownr's monitor opmode + set-channel RX-START tail. It then starts the bulk-IN RX
reader (promiscuous monitor frames + per-frame 8812a RSSI) and the runtime 2-path
DIG/AGC watchdog. No IQK — morrownr receives without it.

The RX reader is started **before** ``monitor.set_monitor_mode`` opens the RX gate: the
kernel posts RX URBs before the gate, and an undrained bulk-IN pipe wedges the chip's RX
FIFO (see ``rx_reader.py``).

Registered in ``wlan/manager.py`` for 0bda:8812 alongside the mainline
``chips/rtl8812au/``. The mainline driver is the DEFAULT; ``WIFIT3_RTL8812=dkms`` selects
this port (the inverse of the 8821/8814 envs, where the DKMS port is the default) until
an A/B proves this port matches or beats mainline. ``inject_frame`` (2.4 GHz TX: deauth /
fake-auth / WEP ARP replay) rides bulk-OUT 0x02 — a source port of the vendor fake-txdesc,
live-verified (no TX pcap exists), not byte-for-byte. ``set_channel`` is 2.4 GHz only (M7
adds 5 GHz).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, ClassVar, List, Optional

import usb.core
import usb.util

from wifit3.engine.protocols import DeviceID, ProgressCallback
from wifit3.wlan.packet import WlanFrameParser

from ..rtl88xxau_base.transport import Rtl88xxauTransport
from ..rtl88xxau_base.tx import build_mgmt_txdesc
from ..rx_reader import RxReaderThread
from . import bb, chan, dig, efuse, firmware, mac, monitor, rf, txpower
from .constants import USB_PID_AWUS036ACH, USB_VID_REALTEK
from .rx import iter_frames

logger = logging.getLogger(__name__)

_DEFAULT_CHANNEL = 1     # connect-time tune target (matches morrownr's cold-boot capture)
_BULK_OUT_EP_TX = 0x02   # the 8812's 3-out-EP map (0x02/0x03/0x04); TX (M6) sends on 0x02
# 2.4 GHz only, 20 MHz primary. 5 GHz is M7 (chan._switch_band_5g raises), so it is NOT
# listed here — the channel hopper must never hand a 5 GHz channel to set_channel yet.
CHANNELS_2G = list(range(1, 14))


class Rtl8812auDkmsDriver:
    SUPPORTED_IDS: ClassVar[List[DeviceID]] = [
        DeviceID(USB_VID_REALTEK, USB_PID_AWUS036ACH,
                 "Realtek RTL8812AU 2T2R (ALFA AWUS036ACH) — vendor/DKMS port"),
    ]
    SUPPORTED_CHANNELS: ClassVar[List[int]] = CHANNELS_2G   # TODO(M7): + 5 GHz

    def __init__(self, transport: Rtl88xxauTransport):
        self.transport = transport
        self.mac_address: Optional[str] = None            # efuse 0xD7 (ALFA OUI)
        self._params: Optional[efuse.ChipParams] = None   # EFUSE board params (set_channel re-tune)
        self._channel: Optional[int] = None
        self.is_warm: bool = False                        # TODO(warm-reattach): cold-plug only
        # Runtime 2-path DIG/AGC watchdog. Toggleable so a fixed-channel A/B can isolate
        # the watchdog's effect on RX breadth.
        self.enable_dig: bool = True
        self._rx_cb: Optional[Callable[[dict], None]] = None
        self._reader: Optional[RxReaderThread] = None
        self._dig_task: Optional[asyncio.Task] = None
        # Serializes control-transfer batches (DIG watchdog vs set_channel) so two
        # executor threads never drive EP0 at once; the RX reader uses bulk-IN.
        self._io_lock = asyncio.Lock()

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "Rtl8812auDkmsDriver":
        # bulk_out_ep 0x02 = the 8812's 3-out-EP MGMT queue (TX rides this at M6).
        return cls(Rtl88xxauTransport(dev, bulk_out_ep=_BULK_OUT_EP_TX))

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_cb = cb

    def _claim(self) -> None:
        """Detach any kernel driver, set the configuration, claim interface 0. This is
        OS-level USB plumbing — outside the vendor op stream the byte-for-byte gate
        reproduces (std enumeration is OS-level), so it does not affect gate faithfulness.
        On Windows+WinUSB the device is already configured; on Linux this is the rmmod-
        equivalent that frees the device from the kernel rtw88/rtl8xxxu driver."""
        dev = self.transport.dev
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
                logger.info("detached kernel driver from interface 0")
        except (NotImplementedError, usb.core.USBError) as e:
            logger.debug("kernel-driver detach skipped: %s", e)
        try:
            dev.set_configuration()
        except usb.core.USBError as e:
            raise IOError(f"set_configuration failed: {e}") from e
        usb.util.claim_interface(dev, 0)
        logger.info("claimed USB interface 0")

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        loop = asyncio.get_running_loop()
        # All bring-up is blocking synchronous USB I/O; keep it off the event loop.
        await loop.run_in_executor(None, self._claim)

        # EFUSE read (probe phase, before power-on, like the vendor): crystal_cap, the
        # 2-path per-rate TX power, bb_swing, rfe_type, the MAC address, and the raw
        # REG_SYS_CFG (its single 0xF0 read) that seeds the phy_cond cut_version.
        if progress_cb:
            progress_cb(0.0, "Reading EFUSE / chip parameters")
        params = await loop.run_in_executor(None, efuse.read_chip_params, self.transport)
        self._params = params
        self.mac_address = params.mac_address
        if params.sys_cfg in (0, 0xFFFFFFFF):
            logger.error("RTL8812AU: implausible REG_SYS_CFG=0x%08x — card wedged or the USB "
                         "plug fell out; unplug ~5 s, replug, retry.", params.sys_cfg)
            if progress_cb:
                progress_cb(1.0, "Card not responding — replug and retry")
            return False
        jp = efuse.build_jaguar_params(params, params.sys_cfg)
        logger.info("RTL8812AU efuse: sys_cfg=0x%08x cut=%d rfe_type=%d crystal_cap=0x%02x "
                    "mac=%s bb_swing 2g=%s", params.sys_cfg, jp.cut_version, params.rfe_type,
                    params.crystal_cap, params.mac_address or "<blank>",
                    "/".join(f"0x{v:03x}" for v in params.bb_swing_2g))

        if progress_cb:
            progress_cb(0.2, "Uploading firmware")
        fw = firmware.load_firmware_blob()
        ready = await loop.run_in_executor(None, firmware.bring_up, self.transport, fw)
        if not ready:
            logger.error("RTL8812AU firmware download did not reach FW-ready")
            if progress_cb:
                progress_cb(1.0, "Firmware NOT ready")
            return False

        if progress_cb:
            progress_cb(0.6, "Configuring MAC / BB / RF + channel tune + TX power + phydm seed")

        # Deterministic init chain — byte-for-byte with verify_pcap.py. No IQK (morrownr
        # receives without it). The only deviations from the gate's op stream are the
        # OS-level USB claim above and bulk-IN RX, neither of which is a vendor control op.
        def _init(t):
            mac.phy_mac_config(t)                                           # M2: MAC reg table
            mac.mac_init_misc(t)                                            # M2: queue/MISC + CR
            bb.phy_bb_config(t, crystal_cap=params.crystal_cap, params=jp)  # M3: BB+AGC+xtal
            rf.phy_rf_config(t, params=jp)                                  # M3: RadioA + RadioB
            chan.set_chnl_bw(t, ch=_DEFAULT_CHANNEL, bb_swing_2g_a=params.bb_swing_2g[0],
                             bb_swing_2g_b=params.bb_swing_2g[1], rfe_type=params.rfe_type)  # M4
            txpower.set_tx_power(t, _DEFAULT_CHANNEL, params.tx_power_2g)    # M-TXPWR: per-rate
            mac.hal_init_misc_pre(t)                                        # M5 §1a
            dig.init_hal_dm(t, search_edcca=True)                          # M5 §2: DIG/AGC/EDCCA
            mac.hal_init_misc_post(t)                                       # M5 §1b: turn-on tail

        await loop.run_in_executor(None, _init, self.transport)
        self._channel = _DEFAULT_CHANNEL

        # Start the bulk-IN RX reader BEFORE the monitor RX-START tail opens the RX gate:
        # the kernel posts URBs before the gate, and an undrained bulk-IN pipe wedges RX.
        self._reader = RxReaderThread(
            loop, self._read_once, self._dispatch, name="8812au-dkms-rx")
        self._reader.start()

        # morrownr's monitor opmode + set-channel RX-START tail: the channel re-tune that
        # restores a clean RX state after the EDCCA search, then the monitor RCR 0x90000001.
        await loop.run_in_executor(None, monitor.set_monitor_mode, self.transport,
                                   _DEFAULT_CHANNEL, params)

        # morrownr's tail leaves RCR = 0x90000001 (AAP|APP_PHYST|APPFCS — airmon's exact
        # state, leaning on RXFLTMAP). That value does NOT deliver management/broadcast
        # frames (beacons) into wifit3's RX pipeline, so re-open the filter to wifit3's
        # monitor RCR (0x9000382F: accept all good frame classes; CRC/ICV-error frames
        # still dropped). The RF re-tune inside the tail above is the actual RX fix, not
        # this RCR value. (Outside the byte-for-byte gate, which stops at the tail.)
        await loop.run_in_executor(None, self.transport.write32,
                                   monitor.REG_RCR, monitor.RCR_MONITOR_VALUE)

        # Runtime 2-path DIG/AGC watchdog: adapt the InitHalDm IGI seed to the live false-
        # alarm rate every ~2 s (kernel cadence), writing both path IGI regs. RX-side only.
        if self.enable_dig:
            self._dig_task = loop.create_task(self._dig_watchdog())
        else:
            logger.info("RTL8812AU DIG watchdog disabled (IGI stays at the InitHalDm seed)")

        if progress_cb:
            progress_cb(1.0, f"Tuned to channel {_DEFAULT_CHANNEL} @ 20 MHz (monitor)")
        return True

    async def _dig_watchdog(self) -> None:
        """Periodic 2-path DIG watchdog. Serialized with set_channel via _io_lock."""
        loop = asyncio.get_running_loop()
        try:
            while True:
                await asyncio.sleep(dig.WATCHDOG_PERIOD_S)
                async with self._io_lock:
                    tick = await loop.run_in_executor(None, dig.watchdog_tick, self.transport)
                logger.debug("RTL8812AU DIG: IGI=0x%02x fa=%d (ofdm=%d cck=%d)",
                             tick.igi, tick.fa_cnt, tick.ofdm_fa, tick.cck_fa)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — a watchdog fault must not kill RX
            logger.exception("RTL8812AU DIG watchdog stopped on error")

    # --- RX path -----------------------------------------------------------
    def _read_once(self) -> Optional[bytes]:
        """Reader-thread side: one blocking bulk-IN read (None on no traffic)."""
        return self.transport.bulk_in()

    def _dispatch(self, buf: bytes) -> None:
        """Loop side: split the aggregated bulk-IN buffer into (frame, rssi) pairs and fan
        each parsed dict to the rx callback. FCS already stripped by the base RX walk."""
        cb = self._rx_cb
        if cb is None:
            return
        for frame, rssi in iter_frames(buf):
            parsed = WlanFrameParser.parse_80211_frame(frame, rssi)
            if parsed is not None:
                cb(parsed)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to a 2.4 GHz channel at 20 MHz primary (M7 adds 5 GHz).

        Runs the runtime tune (``set_channel_bw``) — the same re-tune morrownr's monitor
        tail runs, proven byte-identical to the capture. On a settle (``scan=False``) it
        also re-applies the per-rate txagc so a later deauth/WEP run transmits at the
        EFUSE-calibrated power; ``scan=True`` (the hopper) skips that TX-only re-apply to
        buy back dwell time for RX.
        """
        if channel > 14:
            logger.warning("RTL8812AU: channel %d is 5 GHz (M7, not yet ported); ignoring",
                           channel)
            return False
        params = self._params
        if params is None:
            return False
        loop = asyncio.get_running_loop()

        def _tune(t):
            chan.set_channel_bw(t, channel, bb_swing_2g_a=params.bb_swing_2g[0],
                                bb_swing_2g_b=params.bb_swing_2g[1], rfe_type=params.rfe_type)
            if not scan:
                txpower.set_tx_power(t, channel, params.tx_power_2g)

        async with self._io_lock:   # don't race the DIG watchdog's control I/O
            await loop.run_in_executor(None, _tune, self.transport)
        self._channel = channel
        return True

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        """Transmit one 802.11 frame (M6) — deauth, fake-auth, or WEP ARP replay.

        Builds the vendor fake TX descriptor (``rtl8812a_fill_fake_txdesc``, the shared
        base builder — it IS the 8812a function) and sends ``[desc | frame]`` on bulk-OUT
        0x02 (the 8812's MGMT queue). ``frame_bytes`` is the MPDU *without* FCS (the HW
        appends it). A WEP ARP-replay frame is already encrypted and injected raw (the
        descriptor's SEC_TYPE = 0). Serialized with set_channel / the DIG watchdog via
        ``_io_lock`` so a frame is never emitted mid-retune. Explicit-action only
        (passive-by-default): nothing on the scan/connect path calls this.

        No byte-for-byte gate backs this path — morrownr's cold-boot captures contain no
        successful card TX (the DKMS build used to record them never injected — aireplay
        sent 0 frames to the bus). It is a source port of the vendor fake-txdesc; the live
        gate is ``scripts/rtl8812au_dkms/deauth_hw.py`` (watch for the client's reconnect
        EAPOL). ``use_no_ack`` is accepted for API compatibility; the minimal descriptor
        uses the HW-default ACK/retry policy. TX power is the BB default (per-rate EFUSE TX
        power is a separate deferred milestone), adequate for a nearby target.
        """
        if len(frame_bytes) < 10:           # need addr1 (bytes [4:10]) to read the BMC bit
            return False
        loop = asyncio.get_running_loop()
        bmc = bool(frame_bytes[4] & 0x01)   # addr1 group-address (multicast/broadcast) bit
        desc = build_mgmt_txdesc(len(frame_bytes), bmc=bmc)
        async with self._io_lock:           # don't TX mid-retune (set_channel / DIG)
            await loop.run_in_executor(None, self.transport.bulk_out, desc + frame_bytes)
        return True

    async def close(self) -> None:
        # Stop the DIG watchdog and the reader before releasing the USB handle.
        if self._dig_task is not None:
            self._dig_task.cancel()
            try:
                await self._dig_task
            except asyncio.CancelledError:
                pass
            self._dig_task = None
        if self._reader is not None:
            await self._reader.stop()
            self._reader = None
        try:
            usb.util.release_interface(self.transport.dev, 0)
        except usb.core.USBError as e:
            logger.debug("release_interface(0): %s", e)
        self.transport.close()
