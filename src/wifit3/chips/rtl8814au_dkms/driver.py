"""RTL8814AU driver — vendor (morrownr DKMS) cleanroom port.

Status: 2.4 GHz RX + TX complete and hardware-verified. ``connect()`` runs the full
deterministic init (EFUSE -> firmware -> MAC/BB/RF -> channel tune -> TX power ->
InitHalDm seed -> hal_init turn-on tail -> monitor opmode entry), all pcap-verified,
then starts the bulk-IN RX reader (promiscuous monitor frames + per-frame RSSI) and
the runtime phydm DIG/AGC watchdog. ``inject_frame`` builds the mgmt TX descriptor and
transmits — deauth (M4c) and WEP ARP replay (M4d) are live-verified, and monitor RX is
confirmed promiscuous in both directions (captures client->AP, incl. WPA M2/M4).
5 GHz @ 20 MHz is ported (M5a band switch / M5b channel select / M5c runtime / M5d TX
power — ``set_channel`` tunes 2.4 GHz + 5 GHz with correct per-rate TX power). Pending:
the ch153 spur notch (M5f, minor RX polish).

``wlan/discovery.py`` selects this DKMS port for 0bda:8813 by DEFAULT; the mainline
``rtw88_8814au`` port loads only when ``WIFIT3_RTL8814=mainline`` is set in the
environment (the ``env_or_none(..., "mainline", RTL8814AUDriver) or Rtl8814auDkmsDriver``
fallthrough). So ``beacon_watch`` / the app exercise THIS driver unless that env var
is set; ``scripts/chips/rtl8814au_dkms/`` drives the bring-up + handlers directly.
"""
from __future__ import annotations

import asyncio
import logging
from importlib import resources
from typing import Callable, ClassVar, List, Optional

import usb.core

from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.dot11.parser import WlanFrameParser

from ..rx_reader import RxReaderThread
from .bb import phy_bb_config
from .chan import init_tune, set_channel_bw, set_rfe_reg_init
from .constants import (
    BAND_MAX, BBSWING_DEFAULT, CHANNELS_2G, CHANNELS_5G_NON_DFS,
)
from .dm import init_hal_dm
from .watchdog import WATCHDOG_PERIOD_S, WatchdogState
from .watchdog import tick as watchdog_tick
from .efuse import read_chip_params
from .firmware import bring_up
from .mac import hal_init_turn_on, mac_init_misc, phy_mac_config
from .monitor import _set_macaddr, enable_rx_bar, enter_monitor, set_sta_opmode
from .powertrack import on_channel_switch
from .rf import phy_rf_config
from .rx import iter_frames
from .transport import Rtl8814auTransport
from .tx import DESC_RATE1M, RATEID_IDX_B, TXBF_GID_NONE, build_mgmt_txdesc

logger = logging.getLogger(__name__)

_FW_ASSET = "rtl8814au_fw.bin"
_DEFAULT_CHANNEL = 1  # connect-time tune target (matches the cold-boot capture)


def _load_firmware() -> bytes:
    return (resources.files(__package__) / "assets" / _FW_ASSET).read_bytes()


# rf_path value -> name, for the connect-time config log [SRC] rtw_sta_info.h rf_type enum.
_RF_PATH_NAMES = {2: "RF_2T2R", 4: "RF_2T4R", 5: "RF_3T3R", 7: "RF_4T4R"}


def _detect_super_speed(transport: Rtl8814auTransport) -> bool:
    """USB SuperSpeed (USB 3.x) link? [SRC] IS_SUPER_SPEED_USB -> usb_speed == RTW_USB_SPEED_3.

    libusb speed codes: 4=SUPER, 5=SUPER_PLUS. Unknown/None (or no backend support) -> assume
    not super-speed, which resolves rf_path to RF_2T4R — the safe, capture-matching default."""
    try:
        speed = transport.dev.speed
    except Exception:  # noqa: BLE001 — any backend hiccup means "treat as USB 2"
        return False
    return speed is not None and speed >= 4


class Rtl8814auDkmsDriver(Driver):
    # 2.4 GHz + 5 GHz, 20 MHz primary (M5a band switch / M5b select / M5c runtime / M5d TX
    # power) — both bands tune with correct per-rate TX power for RX and inject. Non-DFS 5 GHz
    # only in the advertised set; set_channel still tunes DFS, we just don't hop it.
    SUPPORTED_CHANNELS: ClassVar[List[int]] = list(CHANNELS_2G + CHANNELS_5G_NON_DFS)
    FAKE_MAC = FakeMacSupport.SPOOFABLE

    def __init__(self, transport: Rtl8814auTransport):
        super().__init__()          # base owns the ACK tally (_ack_detect_on / _our_tx_macs / _ack_counts)
        self.transport = transport
        self.mac_address: Optional[str] = None  # M2: efuse read
        self._channel: Optional[int] = None
        # Vendor lagging current_band_type. init_hw_mlme_ext resets it to BAND_MAX, so 2.4 GHz
        # hops skip CCK txagc until a 5G<->2.4G crossing commits a band (matches the wire).
        self._current_band: int = BAND_MAX
        # EFUSE-decoded per-card config (set in _bringup). rfe_type gates the RFE pinmux +
        # spur/NBI branches (chan.py); is_super_speed only promotes a 4T/3T part to RF_3T3R
        # (efuse rf_path). Default rfe_type=1 is the captured ALFA card.
        self._rfe_type: int = 1
        self._is_super_speed: bool = False
        self._tx_power: tuple = ()  # per-path efuse TX-power info, 2.4 GHz (M2e)
        self._tx_power_5g: tuple = ()  # per-path efuse TX-power info, 5 GHz (M5d)
        # Per-path BB-swing (TxScale) per band — phy_SetBBSwingByBand on a band switch.
        # Both bands are efuse-decoded (2.4 GHz M4e / efuse 0xC6, 5 GHz M5e / efuse 0xC7).
        self._bb_swing_2g: tuple = (BBSWING_DEFAULT,) * 4
        self._bb_swing_5g: tuple = (BBSWING_DEFAULT,) * 4
        self.is_warm: bool = False
        # Runtime phydm watchdog (M3c). Toggleable so a fixed-channel A/B can isolate the
        # watchdog's effect on RX breadth (scan_hw.py --no-dig). _wd_state carries the IGI +
        # CCX state across ticks (seeded from InitHalDm at connect).
        self.enable_dig: bool = True
        self._wd_state: Optional[WatchdogState] = None
        self._rx_cb: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._reader: Optional[RxReaderThread] = None
        self._dig_task: Optional[asyncio.Task] = None
        # Serializes control-transfer batches (DIG watchdog vs set_channel) so two
        # executor threads never drive EP0 at once; the RX reader uses bulk-IN.
        self._io_lock = asyncio.Lock()

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "Rtl8814auDkmsDriver":
        return cls(Rtl8814auTransport(dev))

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_cb = cb

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        """Sink for a terminal RX-reader failure (unplug). Forwarded to the RxReaderThread's
        on_fatal; resolved at call time so registration order vs connect() can't strand it."""
        self._on_lost = cb

    def _bringup(self, t, progress=None):
        """Cold register-I/O bring-up in wire order — the ONE source of truth for the sequence,
        shared verbatim by ``connect()`` (live) and ``scripts/chips/rtl8814au_dkms/verify_pcap.py``
        (replay), so the gate verifies THIS code, not a copy.

        Every op here is in the cold-boot capture byte-for-byte: EFUSE probe, firmware upload,
        MAC/BB/RF config, connect-time tune, InitHalDm DIG/AGC seed, RFE-true, turn-on tail, and
        the airmon STA->monitor dance up to STA opmode. The parts that are NOT register I/O in the
        capture stay in ``connect()``: the libusb setup, the bulk-IN reader thread, the
        ``enter_monitor`` RX gate (deferred so the reader is posted first, else the RX FIFO
        overflows), and the DIG watchdog task. Sets the per-band TX-power / bb-swing / MAC / band /
        channel + runtime watchdog state the operational path reads; returns the ChipParams.
        """
        progress = progress or (lambda *a: None)   # the gate calls _bringup(t) with no progress
        progress(0.0, "Reading EFUSE / chip parameters")
        params = read_chip_params(t, self._is_super_speed)            # EFUSE probe (pre power-on)
        self.mac_address = params.mac_address
        self._rfe_type = params.rfe_type
        self._tx_power, self._tx_power_5g = params.tx_power, params.tx_power_5g
        self._bb_swing_2g, self._bb_swing_5g = params.bb_swing, params.bb_swing_5g
        progress(0.1, "Uploading firmware (3081 IDDMA)")
        if not bring_up(t, _load_firmware()):                         # M1: pwr-on -> FW ready
            raise BringUpError("firmware", "MCU never signalled ready (CPU_DL_READY timeout)")
        progress(0.55, "Configuring MAC / BB / RF registers")
        phy_mac_config(t)                                             # MAC register table
        mac_init_misc(t)                                             # hal_init MISC stage
        phy_bb_config(t, params.rfe_type, params.crystal_cap)        # PHY_BBConfig8814
        phy_rf_config(t, params.rfe_type)                            # PHY_RFConfig8814A
        progress(0.75, "Channel tune + TX power")
        init_tune(t, _DEFAULT_CHANNEL, params.tx_power, params.tx_power_5g,
                  self._bb_swing_2g, self._bb_swing_5g, params.rfe_type)  # ch tune + TX power (2.4G)
        progress(0.88, "DIG/AGC seed + turn-on tail")
        igi_seed = init_hal_dm(t)                                    # InitHalDm DIG/AGC seed (IGI)
        set_rfe_reg_init(t, params.rfe_type)                         # PHY_SetRFEReg8814A(TRUE)
        hal_init_turn_on(t, self.mac_address)                        # turn-on tail + MAC addr
        # airmon STA->monitor dance: init_hw_mlme_ext resets the band to BAND_MAX (so the retune
        # skips CCK). enter_monitor is deferred to connect() (RX-FIFO ordering).
        progress(0.94, "Monitor setup (STA -> monitor)")
        enable_rx_bar(t)                                             # init_hw_mlme_ext RX-BAR
        self._current_band = set_channel_bw(
            t, _DEFAULT_CHANNEL, params.tx_power, params.tx_power_5g,
            self._bb_swing_2g, self._bb_swing_5g, current_band=BAND_MAX, rfe_type=params.rfe_type)
        set_sta_opmode(t, self.mac_address)                         # hw_var_set_opmode(STATION)
        self._channel = _DEFAULT_CHANNEL
        # Runtime watchdog state seeded from the DIG IGI + EFUSE thermal/bb-swing (M3c). Carried
        # (not re-read) across ticks; the CCX nhm_igi also starts at the AGC-default IGI.
        self._wd_state = WatchdogState(cur_ig_value=igi_seed, nhm_igi=igi_seed,
                                       eeprom_thermal=params.eeprom_thermal,
                                       bb_swing_diff_2g=params.bb_swing_diff_2g,
                                       bb_swing_diff_5g=params.bb_swing_diff_5g,
                                       rfe_type=params.rfe_type)
        return params

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        loop = asyncio.get_running_loop()
        # USB link speed decides rf_path (RF_2T4R over USB 2, RF_3T3R for a SuperSpeed 3T/4T
        # part). Read it once here (the pcap gate never calls connect(), so its _bringup keeps
        # the __init__ default False -> RF_2T4R, byte-matching the capture).
        self._is_super_speed = _detect_super_speed(self.transport)
        # The cold bring-up is blocking synchronous USB I/O; run the shared _bringup off the
        # event loop (the exact sequence the pcap gate replays). _bringup emits phase progress
        # from the executor thread — marshal each call back onto the loop so the UI callback runs
        # where it expects to; the loop processes them live while awaiting the executor.
        def _emit(pct: float, msg: str) -> None:
            if progress_cb:
                loop.call_soon_threadsafe(progress_cb, pct, msg)
        params = await loop.run_in_executor(None, self._bringup, self.transport, _emit)
        # Detected per-card config — an odd card (rfe_type != 1, other antenna/rf_path, unusual
        # fuse) is diagnosable from this one line. rfe_type != 1 and rf_path != RF_2T4R run
        # ported-but-hardware-untested branches (see RTL8814AU_DKMS.md "Untested variants").
        logger.debug(
            "RTL8814AU config: rfe_type=%d rf_path=%s antenna=0x%02x max_tx=%d link=%s "
            "crystal_cap=0x%02x mac=%s bb_swing=%s%s",
            params.rfe_type, _RF_PATH_NAMES.get(params.rf_path, f"0x{params.rf_path:x}"),
            params.antenna_option, params.max_tx_cnt,
            "USB3" if self._is_super_speed else "USB2",
            params.crystal_cap, params.mac_address or "<none>",
            "/".join(f"0x{v:03x}" for v in params.bb_swing),
            "" if params.rfe_type == 1 else "  [untested variant]")

        # M3b-3a: start the bulk-IN RX reader BEFORE the monitor RX gate opens. It keeps a
        # blocking bulk read posted on a dedicated thread (off the event loop, so the TUI
        # can't starve RX); each aggregated buffer is split into 802.11 frames and fanned to
        # the rx callback. Reads before enter_monitor just time out harmlessly.
        self._reader = RxReaderThread(
            loop, self._read_once, self._dispatch, name="8814au-dkms-rx",
            on_fatal=lambda e: self._on_lost and self._on_lost(e))
        self._reader.start()

        # Now open the monitor RX gate (accept-all RCR + RXFLTMAP). The reader is already
        # draining the bulk-IN pipe, so the chip RX FIFO has a host read posted the moment
        # frames are accepted — the kernel's order (post URBs, then write the RCR). Doing the
        # reverse overflows the FIFO at startup and leaves 2.4 GHz RX degraded.
        await loop.run_in_executor(None, enter_monitor, self.transport)

        # M3c: the runtime phydm DIG/AGC watchdog — adapt the M3a IGI seed to the
        # live false-alarm rate every ~2 s (the kernel cadence). RX-side only.
        if self.enable_dig:
            self._dig_task = loop.create_task(self._dig_watchdog())
        else:
            logger.info("RTL8814AU DIG watchdog disabled (IGI stays at the M3a seed)")

        if progress_cb:
            progress_cb(1.0, f"Tuned to channel {_DEFAULT_CHANNEL} @ 20 MHz")
        return True

    async def _dig_watchdog(self) -> None:
        """Periodic phydm watchdog (M3c). Serialized with set_channel via _io_lock.

        Runs the full dynamic-check tick (sreset poll + phydm_watchdog members: FA-stats,
        DIG, adaptivity, CCX env-monitor) carrying ``_wd_state`` across fires, so the IGI +
        EDCCA thresholds adapt to the live RF environment instead of freezing at the seed.
        The TX-power thermal-delta correction is a no-op here (RX-irrelevant).
        """
        loop = asyncio.get_running_loop()
        st = self._wd_state
        try:
            while True:
                await asyncio.sleep(WATCHDOG_PERIOD_S)
                async with self._io_lock:
                    ch = self._channel or _DEFAULT_CHANNEL
                    fa = await loop.run_in_executor(None, watchdog_tick, self.transport, st, ch)
                logger.debug("RTL8814AU watchdog: IGI=0x%02x fa=%d", st.cur_ig_value, fa)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — a watchdog fault must not kill RX
            logger.exception("RTL8814AU phydm watchdog stopped on error")

    # --- RX path (M3b-3a) --------------------------------------------------
    def _read_once(self) -> Optional[list]:
        """Reader-thread side: one blocking bulk-IN read, fully decoded to parsed frames.

        The 8814AU in monitor mode delivers a heavy frame stream (every data/control frame
        on the channel, 4T4R), so iter_frames + 802.11 parse is done HERE, on the reader
        thread, not on the event loop. Decoding on the loop made the loop the bottleneck: a
        burst of frames held the GIL long enough to gap the next bulk read, the chip's RX FIFO
        overflowed, and beacons were lost (the reference AP — a fixed 9.8 beacons/s — dropped
        to a fraction). Keeping decode on the reader (like a tight single-thread reader) leaves
        the loop only the lightweight callback fan-out below. Returns parsed dicts, or None on
        a benign no-traffic timeout.
        """
        buf = self.transport.bulk_in()
        if not buf:
            return None
        frames = []
        for frame, rssi in iter_frames(buf):
            # A 10-byte 0xD4 frame is an ACK. The parser drops control frames, so the TX-ACK
            # tap runs HERE (raw frame, reader thread) — not _dispatch, which only sees parsed
            # dicts. The base tallies it iff the ACK tap is armed and RA=frame[4:10] is ours.
            if len(frame) == 10 and frame[0] == 0xD4:
                self.record_ack(frame)
                continue
            parsed = WlanFrameParser.parse_80211_frame(frame, rssi)
            if parsed is not None:
                frames.append(parsed)
        return frames or None

    def _dispatch(self, frames: list) -> None:
        """Loop side: fan each already-parsed frame to the rx callback (registry update runs
        on the loop, so it never races the UI's reads). Decode happened on the reader thread."""
        cb = self._rx_cb
        if cb is None:
            return
        for parsed in frames:
            cb(parsed)

    async def _enable_rx_acks(self) -> None:
        """No-op: the monitor default already accept-alls RXFLTMAP1, so the recipient's ACK
        control frames already reach RX. Nothing to enable on the chip (the base arms the tally).
        Not enter_active_monitor, which makes the chip emit ACKs."""
        return

    async def _disable_rx_acks(self) -> None:
        """No-op, matching ``_enable_rx_acks``: the monitor RX filter is left untouched."""
        return

    def _tune(self, t, channel: int) -> int:
        """One channel tune in wire order — the body ``set_channel``'s executor runs, shared
        verbatim with the pcap gate. ``set_channel_bw`` does the PHY retune (band switch on a
        2.4<->5 GHz crossing + the per-rate TX power); ``on_channel_switch`` mirrors
        phy_SwChnlAndSetBwMode8814A's per-set TX-power-track clear + band-rebase. Advances the
        tracked band + channel; returns the (possibly updated) software band."""
        band = set_channel_bw(t, channel, self._tx_power, self._tx_power_5g,
                              self._bb_swing_2g, self._bb_swing_5g, self._current_band,
                              self._rfe_type)
        if self._wd_state is not None:
            on_channel_switch(self._wd_state, self._channel, channel)
        self._current_band = band
        self._channel = channel
        return band

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to a 2.4 GHz or 5 GHz channel at 20 MHz (band-switches on a crossing).

        Sets the per-rate TX power for the channel's band (M2e / M5d), so both RX and
        inject/deauth use correct power on either band. The register I/O is ``_tune`` (shared
        with the pcap gate); this serializes it off the event loop against the DIG watchdog.

        A deliberate (non-``scan``) tune pauses the RX reader across the switch. The band switch
        gates the RX clock off, reconfigures the CCK/OFDM path, and gates it back on
        (``PHY_SwitchWirelessBand8814A``); a concurrent bulk-IN read corrupts that reconfig and
        strands 2.4 GHz RX deaf. A one-shot dwell has no later switch to re-land it (a scan hop
        self-heals, so it skips the pause), so it would otherwise wedge ~15 s until the DIG
        watchdog happens to re-kick RX. verify_pcap can't catch this — it replays the writes
        single-threaded with no concurrent reader. Mirrors the rtl8821cu_dkms fix."""
        loop = asyncio.get_running_loop()
        async with self._io_lock:   # don't race the DIG watchdog's control I/O
            reader = self._reader
            pause = reader is not None and not scan
            if pause:
                await loop.run_in_executor(None, reader.pause)
            try:
                await loop.run_in_executor(None, self._tune, self.transport, channel)
                if pause:      # band switch re-enabled RX with no read posted -> re-prime the pipe
                    await loop.run_in_executor(None, self.transport.reset_rx_pipe)
            finally:
                if pause:
                    reader.resume()
        return True

    def _inject(self, t, frame_bytes: bytes, *, hw_rate: int = DESC_RATE1M,
                rate_id: int = RATEID_IDX_B) -> None:
        """One monitor-injected mgmt frame in wire order — the body ``_inject_frame``'s executor
        runs, shared with the pcap gate. Builds the update_txdesc mgmt descriptor (M4a; HW
        ACK-retry limit 12, the vendor MGMT value) and sends ``[desc | frame]`` on the
        bulk-OUT pipe. ``frame_bytes`` is the MPDU without FCS (HW appends it); BMC is read from
        addr1's group bit, matching update_txdesc.

        ``hw_rate``/``rate_id`` default to the fixed CCK-1M management rate wifite's own deauths
        ride; a userspace injector (aireplay-ng) picks them per-frame via radiotap, so the pcap
        gate reads the recorded pair back and passes it here to byte-verify the descriptor. (The
        gate's inject byte-check matches the capture, including DATA_RETRY_LIMIT=12.)"""
        bmc = bool(frame_bytes[4] & 0x01)   # addr1 group-address (multicast) bit
        # GID is the target psta's txbf_g_id: a broadcast pseudo-STA keeps the SU-default 63,
        # a real unicast STA (no beamforming here) is 0 (matches the wire across probe/RTS/auth).
        gid = TXBF_GID_NONE if bmc else 0
        desc = build_mgmt_txdesc(len(frame_bytes), hw_rate=hw_rate, rate_id=rate_id,
                                 bmc=bmc, gid=gid)
        t.bulk_out(desc + frame_bytes)

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Transmit one 802.11 management frame once (e.g. a deauth).

        The descriptor + bulk-OUT is ``_inject`` (shared with the pcap gate, which replays the
        recorded aireplay injection against it). Sends on the same RtOutPipe[0] the firmware
        download uses, where the MGMT queue maps. Serialized with ``set_channel`` / the DIG
        watchdog via ``_io_lock`` so the frame is never emitted mid-retune."""
        if len(frame_bytes) < 10:           # need addr1 (bytes [4:10]) to read BMC
            return False
        loop = asyncio.get_running_loop()
        async with self._io_lock:           # don't TX mid-retune (set_channel/DIG)
            await loop.run_in_executor(None, self._inject, self.transport, frame_bytes)
        return True

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        """Realtek HW assigns the 802.11 sequence number (the txdesc sets HWSEQ_EN), so the
        frame goes out unchanged."""
        return frame_bytes

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Point REG_MACID to ``mac`` so the hardware HW-ACKs frames to it.
        Reversed by exit_active_monitor."""
        mac_str = ":".join(f"{b:02x}" for b in mac)
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, _set_macaddr, self.transport, mac_str)
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the card's real MAC in REG_MACID."""
        if not self.mac_address:
            return
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, _set_macaddr, self.transport, self.mac_address)

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
        self.transport.close()
