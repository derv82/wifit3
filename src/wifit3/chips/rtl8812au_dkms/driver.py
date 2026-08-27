"""RTL8812AU / 2T2R driver — vendor (morrownr DKMS) cleanroom port.

Status: 2.4 GHz monitor RX complete and hardware-confirmed (real beacons off the
antenna). ``connect()`` runs the deterministic cold-boot bring-up exactly as the
byte-for-byte gate verifies it (``scripts/chips/rtl8812au_dkms/verify_pcap.py``): EFUSE probe
-> firmware download -> MAC -> BB/RF (both paths) -> 2.4 GHz channel tune -> per-rate TX
power -> the phydm InitHalDm DIG/AGC/EDCCA seed (incl. the live PWDB-EDCCA search) ->
morrownr's monitor opmode + set-channel RX-START tail. It then starts the bulk-IN RX
reader (promiscuous monitor frames + per-frame 8812a RSSI) and the runtime 2-path
DIG/AGC watchdog. No IQK — morrownr receives without it.

The RX reader is started **before** ``monitor.set_monitor_mode`` opens the RX gate: the
kernel posts RX URBs before the gate, and an undrained bulk-IN pipe wedges the chip's RX
FIFO (see ``rx_reader.py``).

Registered in ``wlan/discovery.py`` for 0bda:8812 alongside the mainline
``chips/rtl8812au/``. **This DKMS port is the default** — it survives the 2.4+5 GHz channel
hop that RF-synth-wedges the mainline driver (A/B-proven on hardware); set
``WIFIT3_RTL8812=mainline`` to fall back to the mainline driver. ``inject_frame`` (2.4 GHz
TX: deauth /
fake-auth / WEP ARP replay) rides bulk-OUT 0x02 — a source port of the vendor fake-txdesc,
live-verified (no TX pcap exists), not byte-for-byte. ``set_channel`` tunes 2.4 GHz + 5 GHz
@ 20 MHz (the band switch is byte-verified against the capture's 5 GHz hops).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, ClassVar, List, Optional

import usb.core
import usb.util

from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.dot11.parser import WlanFrameParser

from ..rtl88xxau_base.transport import Rtl88xxauTransport
from ..rtl88xxau_base.tx import build_mgmt_txdesc
from ..rx_reader import RxReaderThread
from . import bb, chan, dig, efuse, firmware, mac, monitor, rf, txpower
from .rx import iter_frames

logger = logging.getLogger(__name__)

_DEFAULT_CHANNEL = 1     # connect-time tune target (matches morrownr's cold-boot capture)
_BULK_OUT_EP_TX = 0x02   # the 8812's 3-out-EP map (0x02/0x03/0x04); TX (M6) sends on 0x02
# 20 MHz primary, both bands. The 5 GHz set matches the UNII channels the cold-boot
# capture hopped (and verify_channels byte-diffs the band switch + each hop).
CHANNELS_2G = list(range(1, 15))
CHANNELS_5G = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124,
               128, 132, 136, 140, 144, 149, 153, 157, 161, 165]
# Scan set excludes the DFS band (52-144): passive-scan-only, radar-shared, home APs avoid it.
# set_channel + verify_channels still drive the full CHANNELS_5G above, byte-for-byte vs the capture.
CHANNELS_5G_NON_DFS = [36, 40, 44, 48, 149, 153, 157, 161, 165]


class Rtl8812auDkmsDriver(Driver):
    SUPPORTED_CHANNELS: ClassVar[List[int]] = CHANNELS_2G + CHANNELS_5G_NON_DFS
    FAKE_MAC = FakeMacSupport.SPOOFABLE

    def __init__(self, transport: Rtl88xxauTransport):
        super().__init__()          # base owns the ACK tally (_ack_detect_on / _our_tx_macs / _ack_counts)
        self.transport = transport
        self.mac_address: Optional[str] = None            # efuse 0xD7 (ALFA OUI)
        self._params: Optional[efuse.ChipParams] = None   # EFUSE board params (set_channel re-tune)
        self._channel: Optional[int] = None
        self.is_warm: bool = False                        # TODO(warm-reattach): cold-plug only
        # Runtime 2-path DIG/AGC watchdog. Toggleable so a fixed-channel A/B can isolate
        # the watchdog's effect on RX breadth.
        self.enable_dig: bool = True
        self._rx_cb: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
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

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        """Sink for a terminal RX-reader failure (unplug). Forwarded to the RxReaderThread's
        on_fatal; resolved at call time so registration order vs connect() can't strand it."""
        self._on_lost = cb

    def _claim(self) -> None:
        """Detach any kernel driver, set the configuration, claim interface 0. This is
        OS-level USB plumbing — outside the vendor op stream the byte-for-byte gate
        reproduces (std enumeration is OS-level), so it does not affect the byte-for-byte gate.
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
        logger.debug("claimed USB interface 0")

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        loop = asyncio.get_running_loop()
        # All bring-up is blocking synchronous USB I/O; keep it off the event loop. Wrap the claim so a
        # permission failure (set_configuration's IOError, claim_interface's USBError, or a Windows
        # NotImplementedError) reaches WlanInterface.connect on the cause chain to be classified.
        try:
            await loop.run_in_executor(None, self._claim)
        except (IOError, usb.core.USBError, NotImplementedError) as e:
            raise BringUpError("claim", str(e)) from e

        # EFUSE read (probe phase, before power-on, like the vendor): crystal_cap, the
        # 2-path per-rate TX power, bb_swing, rfe_type, the MAC address, and the raw
        # REG_SYS_CFG (its single 0xF0 read) that seeds the phy_cond cut_version.
        if progress_cb:
            progress_cb(0.0, "Reading EFUSE / chip parameters")
        params = await loop.run_in_executor(None, efuse.read_chip_params, self.transport)
        self._params = params
        self.mac_address = params.mac_address
        if params.sys_cfg in (0, 0xFFFFFFFF):
            raise BringUpError(
                "probe", "implausible REG_SYS_CFG=0x%08x — card wedged or the USB plug fell "
                "out; unplug ~5 s, replug, retry" % params.sys_cfg)
        jp = efuse.build_jaguar_params(params, params.sys_cfg)
        # Detected per-card config — an odd card (rfe_type != 3, non-C-cut silicon, other
        # board_type / fuse) is diagnosable from this one line. Only the ALFA AWUS036ACH
        # (rfe_type=3, C-cut) is pcap-gated; other rfe_type / cut branches are ported-from-C
        # but hardware-untested (see RTL8812AU_DKMS.md "EFUSE variants").
        logger.info(
            "RTL8812AU config: sys_cfg=0x%08x cut=%d%s rfe_type=%d board_type=0x%02x "
            "crystal_cap=0x%02x mac=%s bb_swing_2g=%s%s",
            params.sys_cfg, jp.cut_version, " (C-cut)" if params.is_c_cut else "",
            params.rfe_type, params.board_type, params.crystal_cap,
            params.mac_address or "<blank>",
            "/".join(f"0x{v:03x}" for v in params.bb_swing_2g),
            "" if (params.is_c_cut and params.rfe_type == 3) else "  [untested variant]")

        if progress_cb:
            progress_cb(0.2, "Uploading firmware")
        fw = firmware.load_firmware_blob()
        ready = await loop.run_in_executor(None, firmware.bring_up, self.transport, fw)
        if not ready:
            raise BringUpError("firmware", "MCU never signalled ready (FW-ready timeout)")

        if progress_cb:
            progress_cb(0.6, "Configuring MAC / BB / RF + channel tune + TX power + phydm seed")

        # Deterministic init chain — byte-for-byte with verify_pcap.py. No IQK (morrownr
        # receives without it). The only deviations from the gate's op stream are the
        # OS-level USB claim above and bulk-IN RX, neither of which is a vendor control op.
        def _init(t):
            mac.phy_mac_config(t)                                           # M2: MAC reg table
            mac.mac_init_misc(t)                                            # M2: queue/MISC + CR
            bb.phy_bb_config(t, crystal_cap=params.crystal_cap, params=jp)  # M3: BB+AGC+xtal
            rf.phy_rf_config(t, params=jp, is_c_cut=params.is_c_cut)         # M3: RadioA + RadioB
            chan.set_chnl_bw(t, ch=_DEFAULT_CHANNEL, bb_swing_2g_a=params.bb_swing_2g[0],
                             bb_swing_2g_b=params.bb_swing_2g[1], rfe_type=params.rfe_type,
                             is_c_cut=params.is_c_cut)                       # M4
            txpower.set_tx_power(t, _DEFAULT_CHANNEL, params.tx_power_2g)    # M-TXPWR: per-rate
            mac.hal_init_misc_pre(t)                                        # M5 §1a
            dig.init_hal_dm(t, search_edcca=True)                          # M5 §2: DIG/AGC/EDCCA
            mac.hal_init_misc_post(t)                                       # M5 §1b: turn-on tail

        await loop.run_in_executor(None, _init, self.transport)
        self._channel = _DEFAULT_CHANNEL

        # Start the bulk-IN RX reader BEFORE the monitor RX-START tail opens the RX gate:
        # the kernel posts URBs before the gate, and an undrained bulk-IN pipe wedges RX.
        self._reader = RxReaderThread(
            loop, self._read_once, self._dispatch, name="8812au-dkms-rx",
            on_fatal=lambda e: self._on_lost and self._on_lost(e))
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
        """Admit ACK control frames (RXFLTMAP1 bit13) so the tap can see the AP's ACKs to us
        (this chip filters them out at the monitor default). The base arms the tally; here we
        only flip the RX filter. Not enter_active_monitor, which makes the chip emit ACKs."""
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, monitor.admit_ack_frames, self.transport)

    async def _disable_rx_acks(self) -> None:
        """Restore the default monitor RX filter (clear RXFLTMAP1 bit13)."""
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, monitor.drop_ack_frames, self.transport)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to a 2.4 GHz or 5 GHz channel at 20 MHz primary.

        Runs the runtime tune (``set_channel_bw``: phy_SwChnl switches band only on a
        2.4<->5 crossing, then channel select + 20 MHz BW). On a settle (``scan=False``)
        it re-applies the per-rate txagc for the channel's band so a later deauth/WEP run
        transmits at the EFUSE-calibrated power; ``scan=True`` (the hopper) skips that
        TX-only re-apply to buy back dwell time for RX.
        """
        params = self._params
        if params is None:
            return False
        loop = asyncio.get_running_loop()

        def _tune(t):
            chan.set_channel_bw(t, channel, bb_swing_2g_a=params.bb_swing_2g[0],
                                bb_swing_2g_b=params.bb_swing_2g[1],
                                bb_swing_5g_a=params.bb_swing_5g[0],
                                bb_swing_5g_b=params.bb_swing_5g[1], rfe_type=params.rfe_type,
                                is_c_cut=params.is_c_cut)
            if not scan:
                if channel <= 14:
                    txpower.set_tx_power(t, channel, params.tx_power_2g)
                else:
                    txpower.set_tx_power_5g(t, channel, params.tx_power_5g)

        async with self._io_lock:   # don't race the DIG watchdog's control I/O
            await loop.run_in_executor(None, _tune, self.transport)
        self._channel = channel
        return True

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Build the vendor fake TX descriptor (``rtl8812a_fill_fake_txdesc``, the shared base
        builder, the 8812a function; no retry-limit field, HW global retry)
        and send ``[desc | frame]`` once on bulk-OUT 0x02 (the 8812's MGMT queue).
        ``frame_bytes`` is the MPDU *without* FCS (the HW appends it). A WEP ARP-replay frame
        is already encrypted and injected raw (the descriptor's SEC_TYPE = 0). Serialized with
        set_channel / the DIG watchdog via ``_io_lock`` so a frame is never emitted mid-retune.

        No byte-for-byte gate backs this path — morrownr's cold-boot captures contain no
        successful card TX. It is a source port of the vendor fake-txdesc."""
        if len(frame_bytes) < 10:           # need addr1 (bytes [4:10]) to read the BMC bit
            return False
        loop = asyncio.get_running_loop()
        bmc = bool(frame_bytes[4] & 0x01)   # addr1 group-address (multicast/broadcast) bit
        payload = build_mgmt_txdesc(len(frame_bytes), bmc=bmc) + frame_bytes
        async with self._io_lock:           # don't TX mid-retune (set_channel / DIG)
            await loop.run_in_executor(None, self.transport.bulk_out, payload)
        return True

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        """Realtek HW assigns the 802.11 sequence number (the txdesc sets HWSEQ_EN), so the
        frame goes out unchanged."""
        return frame_bytes

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Point REG_MACID at ``mac`` so the hardware HW-ACKs frames to it.
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
