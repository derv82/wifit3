"""rt5572 driver — Panda PAU09 N600 (silicon RT5592 / RF5592), 2.4 + 5 GHz, 2T2R.

Standalone RF5592 port, split from the rt2800usb parent (see RT5572.md for why).
The cold register bring-up is ``bring_up.bring_up(transport)`` — ONE function shared
with the acceptance gate (``scripts/chips/rt5572/verify_pcap.py``), so the gate exercises
exactly what ``connect()`` runs on hardware. connect() = claim → bring_up() →
write MAC + monitor filter → set_channel(1) → RX loop.
"""
from __future__ import annotations

import asyncio
import errno
import logging
from collections import deque
from typing import Callable, Optional

import usb.core
import usb.util

from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError

from .constants import (
    IEEE80211_FCTL_FTYPE,
    IEEE80211_FTYPE_DATA,
    IEEE80211_QOS_CTL_ACK_POLICY_NOACK,
    IEEE80211_STYPE_QOS_DATA,
    NUM_TX_QUEUES,
    RT_RT5592,
    TX_QUEUE_LIMIT,
    TX_STA_FIFO,
    TX_STA_FIFO_PID_QUEUE,
    TX_STA_FIFO_VALID,
    TXSTATUS_READ_INTERVAL,
    TXSTATUS_TIMEOUT,
    TXWI_PHYMODE_CCK,
    TXWI_PHYMODE_OFDM,
)
from wifit3.dot11.parser import WlanFrameParser

from .chan import (
    CHANNELS_5G_NON_DFS, default_power as _default_power, hop_channel,
)
from .eeprom import resolve_rf_chip
from .mac import (
    ChipId, disable_radio, write_mac_address,
)
from .monitor import enable_monitor
from .bring_up import bring_up
from .rfcsr import RfFilterCal
from .rx import (
    RssiCal, parse_rx_urb, probe_endpoints, read_rx_burst, rssi_cal_for_channel,
    rxwi_size_for_silicon,
)
from ..rx_reader import RxReaderThread
from .transport import RT5572Transport
from .tx import (
    ieee80211_get_qos_ctl, ieee80211_hdrlen, inject_frame as _tx_inject_frame, select_queue,
    txwi_size_for_silicon,
)

logger = logging.getLogger(__name__)


class RT5572Driver(Driver):
    """Driver for the Panda PAU09 N600 (silicon RT5592 / RF5592), 2.4 + 5 GHz 2T2R."""

    SUPPORTED_CHANNELS = list(range(1, 15)) + list(CHANNELS_5G_NON_DFS)
    FAKE_MAC = FakeMacSupport.SPOOFABLE

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RT5572Driver":
        chip_id_hint = id_entry.extras.get("chip_id", "")
        return cls(dev, chip_id_hint=chip_id_hint)

    def __init__(self, dev: usb.core.Device, *, chip_id_hint: str = ""):
        super().__init__()          # base owns the ACK tally (_ack_detect_on / _our_tx_macs / _ack_counts)
        self.dev = dev
        self.transport = RT5572Transport(dev)
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._rx_reader: Optional[RxReaderThread] = None
        self._bulk_in_ep: Optional[int] = None
        self._tx_queue_eps: list[int] = []    # bulk-OUT endpoint per TX queue id
        self._tx_entry_index: list[int] = [0] * NUM_TX_QUEUES   # Q_INDEX per TX queue [SRC rt2x00queue.c:670]
        # Per TX queue, the DMA-done time of each entry still waiting for its TX_STA_FIFO status.
        self._tx_status_pending: list[deque[float]] = [deque() for _ in range(NUM_TX_QUEUES)]
        self._txstatus_fifo: deque[int] = deque()
        self._tx_status_reading = False
        self._txdone_work_queued = False
        self._txstatus_timer: Optional[asyncio.Task] = None
        self._conf_lock = asyncio.Lock()    # rt2x00dev->conf_mutex
        self._rxwi_size: int = 16          # set at connect-time from silicon_id
        self._claimed = False
        self._eeprom = None                 # EepromValues post-EFUSE-read
        # RT3572-only: filter calibration values + saved BBP25/26 from
        # init_rfcsr_3572, replayed on every channel tune.
        self._rf_cal: Optional[RfFilterCal] = None
        # Debug instrumentation: snapshot TX-side regs on first inject + on
        # every inject (TX_STA_CNT0/1 are read-to-clear so we get TX-event
        # deltas across the burst). One-shot snapshot covers TX_PIN_CFG,
        # TX_BAND_CFG, RFCSR1/12/13 — these don't change frame-to-frame.
        self._first_inject_dumped: bool = False

        # Driver Protocol surface area.
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self.current_channel: int = 1
        self._rssi_cal = RssiCal()   # refreshed per channel once EEPROM is parsed
        self.chip_id: Optional[ChipId] = None
        self.chip_id_hint = chip_id_hint   # from VID:PID; "rt5572"
        # Probed at connect() time from MAC_DEBUG_INDEX.XTAL; picks which of
        # rf_vals_5592_xtal20 / xtal40 the channel tune consults (PAU09 N600's
        # actual xtal isn't documented).
        # Picks which of rf_vals_5592_xtal20 / xtal40 the channel tune
        # consults (PAU09 N600's actual xtal isn't documented).
        self._xtal_40mhz: bool = False
        self._tx_seq: int = 0            # running 802.11 seq stamped into injected frames

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_callback = cb

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        """Sink for a terminal RX-reader failure (unplug). Forwarded to the RxReaderThread's
        on_fatal; resolved at call time so registration order vs connect() can't strand it."""
        self._on_lost = cb

    # ---- USB claim helpers ----------------------------------------------
    def _claim(self) -> None:
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
            raise IOError(f"set_configuration failed: {e}") from e
        usb.util.claim_interface(self.dev, 0)
        self._claimed = True

    def _release(self) -> None:
        if not self._claimed:
            return
        try:
            usb.util.release_interface(self.dev, 0)
            usb.util.dispose_resources(self.dev)
        except usb.core.USBError as e:
            logger.warning("USB release warning: %s", e)
        self._claimed = False

    # ---- connect --------------------------------------------------------
    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """M1 connect: claim → identify → exit.

        M2 will wire in the real cold_bring_up.
        """
        loop = asyncio.get_event_loop()

        def _progress(pct: float, msg: str) -> None:
            if progress_cb:
                progress_cb(pct, msg)
            logger.info("[%3d%%] %s", int(pct * 100), msg)

        try:
            _progress(0.10, "Claiming USB interface")
            await loop.run_in_executor(None, self._claim)

            # THE cold register bring-up — one call to the shared bring_up(), the
            # exact sequence the acceptance gate (scripts/chips/rt5572/verify_pcap.py)
            # replays byte-for-byte. No inline step list here: connect() and the
            # gate run the same code, so they cannot drift.
            state = await loop.run_in_executor(
                None,
                lambda: bring_up(self.transport,
                                 progress=lambda p, m: _progress(0.10 + 0.85 * p, m)),
            )
            self.chip_id = state.chip
            self._eeprom = state.eeprom
            self._rf_cal = state.rf_cal
            self._xtal_40mhz = state.xtal_40mhz
            self.mac_address = ":".join(f"{b:02x}" for b in state.eeprom.mac_address)
            logger.info(
                "chip_id: %s rev=0x%04x  EFUSE MAC=%s lna_gain_bg=%d freq_offset=%d "
                "xtal=%sMHz", self.chip_id.name, self.chip_id.revision, self.mac_address,
                state.eeprom.lna_gain_bg, state.eeprom.freq_offset,
                "40" if self._xtal_40mhz else "20",
            )
            if not self.chip_id.is_supported:
                raise BringUpError(
                    "chip-id",
                    f"silicon ID 0x{self.chip_id.silicon_id:04x} not supported "
                    "(rt5572 expects RT5592)",
                )

            # Kernel rt2800_init_eeprom RF-chip identification, ported so this
            # driver runs on ANY 148f:5572 card regardless of the EEPROM's
            # antenna/cal contents. RT5592 silicon hardcodes RF5592, so the
            # decode confirms (not infers) the RF; the antenna path + all
            # per-unit cal (freq_offset/lna/iq/xtal/txpower) are already read
            # from the runtime EEPROM and threaded through bring_up + the tune.
            # [SRC] rt2800lib.c:11182-11243.
            rf = resolve_rf_chip(self.chip_id.silicon_id, self._eeprom)
            logger.info(
                "detected config: silicon=%s rf=%s antenna=%dT%dR freq_off=%d "
                "ext_lna(bg/a)=%s/%s bt_coex=%s xtal=%sMHz eeprom=%s",
                self.chip_id.name, rf.name, self._eeprom.txpath, self._eeprom.rxpath,
                self._eeprom.freq_offset, self._eeprom.has_cap_external_lna_bg,
                self._eeprom.has_cap_external_lna_a, self._eeprom.has_cap_bt_coexist,
                "40" if self._xtal_40mhz else "20",
                "unburned" if self._eeprom.looks_unburned else "burned",
            )
            if not rf.ported and rf.rf_id != 0:
                logger.warning(
                    "untested variant: EEPROM RF chip %s on %s silicon has no "
                    "ported config_channel path — running the silicon default "
                    "tune (kernel would too)", rf.name, self.chip_id.name,
                )

            if self._rf_cal is not None:
                logger.debug(
                    "RF filter cal: bw20=0x%02x bw40=0x%02x bbp25=0x%02x bbp26=0x%02x",
                    self._rf_cal.calibration_bw20, self._rf_cal.calibration_bw40,
                    self._rf_cal.bbp25, self._rf_cal.bbp26,
                )

            # Kernel-faithful monitor entry — the mac80211/airmon monitor bring-up
            # (configure_filter 0x97→0x93 + config_txpower/retry/ps/ant + reset_tuner),
            # the exact sequence the acceptance gate replays byte-for-byte. Replaces the
            # old RX_FILTER_CFG=0x11 monitor-first shortcut: 0x93 is still promiscuous
            # (DROP_NOT_TO_ME clear) but additionally drops PHY-error + duplicate frames,
            # and this path also lands TX_RTY_CFG (retry 7/4) which the 0x11 shortcut
            # left at the init default (2/2). RX only, no TX. [[feedback_passive_by_default]]
            await loop.run_in_executor(
                None,
                lambda: enable_monitor(
                    self.transport, self.chip_id.silicon_id,
                    self._eeprom, self._xtal_40mhz,
                ),
            )

            _progress(0.99, "Tuning to default channel 1")
            try:
                # Full hop_channel bracket (stop RX → survey → reconfig → start RX) —
                # the same per-hop unit set_channel() uses, so the default tune matches
                # the capture's first channel change. The RX reader isn't running yet,
                # so the bare register bracket needs no reader pause here.
                await loop.run_in_executor(
                    None,
                    lambda: hop_channel(
                        self.transport, self.chip_id.silicon_id, 1,
                        **self._channel_kwargs(1),
                    ),
                )
                self.current_channel = 1
            except (ValueError, IOError, usb.core.USBError, NotImplementedError) as e:
                logger.warning("default-channel tune failed: %s", e)

            _progress(1.00, "Probing endpoints + starting RX loop")
            self._rxwi_size = rxwi_size_for_silicon(self.chip_id.silicon_id)
            eps = probe_endpoints(self.dev)
            self._bulk_in_ep = eps.primary_bulk_in
            # Bulk-OUTs go to the TX queues in descriptor order; a queue past the last one shares
            # it [SRC rt2x00usb.c:599-647 rt2x00usb_find_endpoints].
            self._tx_queue_eps = []
            if eps.bulk_out:
                for qid in range(NUM_TX_QUEUES):
                    if qid < len(eps.bulk_out):
                        self._tx_queue_eps.append(eps.bulk_out[qid])
                    else:
                        self._tx_queue_eps.append(eps.bulk_out[-1])
            self._rx_reader = RxReaderThread(
                loop, self._rx_read_once, self._rx_dispatch, name="rt2800usb-rx",
                on_fatal=lambda e: self._on_lost and self._on_lost(e))
            self._rx_reader.start()

            self.is_warm = True
            return True

        except (IOError, usb.core.USBError, NotImplementedError) as e:
            raise BringUpError("init", str(e)) from e

    # ---- RX loop --------------------------------------------------------
    # ---- RX callables for the shared RxReaderThread ---------------------
    # read_once runs on the reader thread; dispatch runs on the event loop.

    def _rx_read_once(self) -> Optional[bytes]:
        """One blocking bulk-IN read; None on a benign timeout."""
        return read_rx_burst(self.dev, self._bulk_in_ep)

    def _rx_dispatch(self, buf: bytes) -> None:
        """Decode one RX URB → parse → rx callback (on the loop)."""
        rx = parse_rx_urb(buf, rxwi_size=self._rxwi_size, rssi_cal=self._rssi_cal)
        if rx is None or rx.has_fcs_error:
            return
        # A 10-byte 0xD4 frame is an ACK (the parser drops control frames); the base tallies it
        # iff the ACK tap is armed and RA=mpdu[4:10] is a MAC we inject as.
        if len(rx.mpdu) == 10 and rx.mpdu[0] == 0xD4:
            self.record_ack(rx.mpdu)
            return
        parsed = WlanFrameParser.parse_80211_frame(rx.mpdu, rx.rssi_dbm)
        if parsed is not None and self._rx_callback is not None:
            try:
                self._rx_callback(parsed)
            except Exception as e:
                logger.exception("rx_callback raised: %s", e)

    # ---- channel tune (M4) ----------------------------------------------
    def _channel_kwargs(self, channel: int = 1) -> dict:
        """Bundle the per-silicon kwargs that set_channel needs.

        RT5392 just wants freq_offset + lna_gain. RT3572 also needs
        the filter calibration + chain counts + per-band LNA gain +
        external-LNA flags from NIC_CONF1. RT5592 needs chain counts +
        BT-coex + xtal selection (but NOT cal_result — RF5592 has no
        rt2800_rx_filter_calibration step). The ``channel`` arg lets us
        pick the right per-band fields (lna_a vs lna_bg).
        """
        if self._eeprom is None:
            return {"lna_gain": 0, "freq_offset": 0}
        is_2g = channel <= 14
        lna_gain = self._eeprom.lna_gain_bg if is_2g else self._eeprom.lna_gain_a
        kwargs = {
            "lna_gain": lna_gain,
            "freq_offset": self._eeprom.freq_offset,
        }
        if self.chip_id is not None and self.chip_id.silicon_id == 0x3572:
            # RT3572's _set_channel writes RFCSR12/13.TX_POWER per tune. On a
            # BURNED EFUSE the kernel sources these per-channel from TXPOWER_BG/A;
            # on an UNBURNED one (the user's AWUS051NH v2, NIC_CONF0=0x0000) there
            # is nothing to decode, so keep the wire-derived defaults the in-tree
            # driver programs: RFCSR12=0x6b (chain-0 TX_POWER=11) on 2.4 GHz,
            # chain-1=0 (single TX chain); 5 GHz at 12 pending a burned-unit 5 GHz
            # capture. [WIRE] captures_rt3572_tx_diff/aireplay.pcap.
            is_2g = channel <= 14
            # Chain count from the EEPROM default (the unburned fallback returns
            # txpath=1). The in-tree driver transmits on this card with a single
            # TX chain: [WIRE] aireplay.pcap configures RFCSR1/13 + TX_PIN_CFG for
            # one chain (RFCSR13 TX_POWER=0). Match it — 2T2R is not used here.
            txpath = self._eeprom.txpath
            if self._eeprom.looks_unburned:
                default_power1, default_power2 = (11, 0) if is_2g else (12, 12)
            else:
                default_power1, default_power2 = _default_power(
                    self._eeprom, 0x3572, channel
                )
            kwargs.update(
                cal_result=self._rf_cal,
                tx_chain_num=txpath,
                rx_chain_num=self._eeprom.rxpath,
                has_cap_bt_coexist=self._eeprom.has_cap_bt_coexist,
                has_cap_external_lna_a=self._eeprom.has_cap_external_lna_a,
                default_power1=default_power1,
                default_power2=default_power2,
            )
        elif self.chip_id is not None and self.chip_id.silicon_id == RT_RT5592:
            # RF55xx analog PA gain (RFCSR49/50) comes from the per-channel EEPROM
            # TXPOWER_BG1/BG2 (2.4 GHz) / A1/A2 (5 GHz), clamped to the device
            # range. The PAU09's EFUSE is always burned; if a future unburned
            # RF55xx unit appears this decodes to ~0 (the pre-fix behaviour) and
            # would need its own wire-derived fallback. Passing ``eeprom`` also
            # arms the per-rate config_txpower (TX_PWR_CFG_0..4) in set_channel.
            default_power1, default_power2 = _default_power(
                self._eeprom, RT_RT5592, channel, self._xtal_40mhz
            )
            kwargs.update(
                tx_chain_num=self._eeprom.txpath,
                rx_chain_num=self._eeprom.rxpath,
                has_cap_bt_coexist=self._eeprom.has_cap_bt_coexist,
                has_cap_external_lna_a=self._eeprom.has_cap_external_lna_a,
                has_cap_external_lna_bg=self._eeprom.has_cap_external_lna_bg,
                xtal_40mhz=self._xtal_40mhz,
                iq_cal=self._eeprom.iq_cal,
                default_power1=default_power1,
                default_power2=default_power2,
                eeprom=self._eeprom,
            )
        return kwargs

    def _tune_bracketed(self, channel: int, kwargs: dict) -> None:
        """Channel change via the shared hop_channel bracket (runs on the executor
        thread). hop_channel is the gate-verified per-hop unit — stop_queue(RX) →
        update_survey → reconfig_channel → start_queue(RX). The kernel disables RX
        around config_channel or the RF/BBP writes don't latch (the focus-mode
        dead-radio / band-transition bug); we additionally pause the RX reader
        thread across it so no URB is in flight during the quiesce. Mirrors the
        8821cu 'serialize + pause RX reader across deliberate tunes' fix."""
        paused = self._rx_reader.pause() if self._rx_reader is not None else False
        try:
            hop_channel(self.transport, self.chip_id.silicon_id, channel, **kwargs)
        finally:
            if paused:
                self._rx_reader.resume()

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        if self.chip_id is None:
            logger.error("set_channel(%d): connect() must run first", channel)
            return False
        kwargs = self._channel_kwargs(channel)
        try:
            async with self._conf_lock:
                await asyncio.get_event_loop().run_in_executor(
                    None, self._tune_bracketed, channel, kwargs)
        except ValueError as e:
            logger.warning("rt5572 set_channel: %s", e)
            return False
        except (IOError, usb.core.USBError, NotImplementedError) as e:
            logger.error("rt5572 set_channel(%d): %s", channel, e)
            return False
        self.current_channel = channel
        if self._eeprom is not None:
            self._rssi_cal = rssi_cal_for_channel(self._eeprom, channel)
        return True

    # ---- TX inject (M5) -------------------------------------------------
    def _stamp_tx_seq(self, frame: bytes) -> bytes:
        """Stamp the next running sequence number into the frame's seqctl (bytes 22-23,
        ``seqnum << 4`` little-endian). The TXWI sets NSEQ=0, so the chip transmits the
        frame's own seqctl; without this every inject shares seq=0 and a receiver's
        duplicate filter (or a retransmit histogram) folds them into one. Returns a copy;
        the caller's bytes are untouched."""
        if len(frame) < 24:
            return frame
        seq = self._tx_seq & 0xFFF
        self._tx_seq = (self._tx_seq + 1) & 0xFFF
        buf = bytearray(frame)
        buf[22:24] = ((seq << 4) & 0xFFFF).to_bytes(2, "little")
        return bytes(buf)

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Build TXINFO + TXWI (TXWI ACK bit ON for a unicast addr1 so the chip retries up to the
        global TX_RTY_CFG SHORT_RTY_LIMIT (mac80211 default 7), set at monitor entry)
        and bulk-OUT ``frame_bytes`` once. The seq is already stamped by the base."""
        if self.chip_id is None or not self._tx_queue_eps:
            logger.error("inject_frame: connect() must run first")
            return False
        # ieee80211_monitor_start_xmit / ieee80211_tx drop [SRC net/mac80211/tx.c:2348-2349, 1941-1942]
        if len(frame_bytes) < 10 or len(frame_bytes) < ieee80211_hdrlen(frame_bytes[0] | frame_bytes[1] << 8):
            return False
        txwi_sz = txwi_size_for_silicon(self.chip_id.silicon_id)
        # 5 GHz has no CCK modulation, so a CCK-tagged frame is armed but never
        # emitted; 5 GHz TX must be OFDM (MCS 0 = 6 Mbps OFDM).
        phymode = TXWI_PHYMODE_OFDM if self.current_channel > 14 else TXWI_PHYMODE_CCK
        loop = asyncio.get_event_loop()
        if logger.isEnabledFor(logging.DEBUG) and not self._first_inject_dumped:
            await loop.run_in_executor(None, self._dump_tx_state, "pre-first-inject")
            self._first_inject_dumped = True
        qid = select_queue(frame_bytes)
        entry_index = self._tx_entry_index[qid]
        use_no_ack = bool(frame_bytes[4] & 0x01)    # multicast addr1 [SRC net/mac80211/tx.c:1273-1275]
        if ((frame_bytes[0] & (IEEE80211_FCTL_FTYPE | IEEE80211_STYPE_QOS_DATA))
                == IEEE80211_FTYPE_DATA | IEEE80211_STYPE_QOS_DATA
                and frame_bytes[ieee80211_get_qos_ctl(frame_bytes)] & IEEE80211_QOS_CTL_ACK_POLICY_NOACK):
            use_no_ack = True                       # ieee80211_set_qos_hdr [SRC net/mac80211/wme.c:208-211]
        try:
            sent = await loop.run_in_executor(
                None,
                lambda: _tx_inject_frame(
                    self.dev, frame_bytes,
                    txwi_size=txwi_sz, ep=self._tx_queue_eps[qid], qid=qid, entry_index=entry_index,
                    use_no_ack=use_no_ack, phymode=phymode,
                ),
            )
            # rt2x00queue_index_inc(entry, Q_INDEX) [SRC rt2x00queue.c:703, 874-876]
            self._tx_entry_index[qid] += 1
            if self._tx_entry_index[qid] >= TX_QUEUE_LIMIT:
                self._tx_entry_index[qid] = 0
        except ValueError as e:
            logger.warning("rt5572 inject_frame bad frame: %s", e)
            return False
        except usb.core.USBError as e:
            logger.error("rt5572 inject_frame USBError: %s", e)
            return False
        # rt2x00usb_interrupt_txdone [SRC rt2x00usb.c:274-294]
        self._tx_status_pending[qid].append(loop.time())    # rt2x00lib_dmadone
        self._async_read_tx_status()                        # rt2800usb_tx_dma_done
        if self._txstatus_fifo:
            self._queue_txdone_work()
        logger.trace("inject_frame: ch=%d len=%d txwi=%dB phymode=%d bulk-OUT accepted %d bytes",
                     self.current_channel, len(frame_bytes), txwi_sz, phymode, sent)
        if logger.isEnabledFor(logging.DEBUG):
            await loop.run_in_executor(None, self._dump_tx_counters, "post-inject")
        return True

    def _async_read_tx_status(self) -> None:
        """rt2800usb_async_read_tx_status [SRC rt2800usb.c:143-153]."""
        if self._tx_status_reading:
            return
        self._tx_status_reading = True
        self._txstatus_timer = asyncio.get_running_loop().create_task(self._tx_sta_fifo_timeout())

    async def _tx_sta_fifo_timeout(self) -> None:
        """rt2800usb_tx_sta_fifo_timeout: each hrtimer expiry reads TX_STA_FIFO; rt2800usb_tx_sta_fifo_read_completed
        resubmits the read or re-arms the hrtimer [SRC rt2800usb.c:94-169]."""
        loop = asyncio.get_running_loop()
        hrtimer_delay: Optional[float] = 2 * TXSTATUS_READ_INTERVAL
        while hrtimer_delay is not None:
            await asyncio.sleep(hrtimer_delay)
            hrtimer_delay = None
            queue_txdone_work = False
            resubmit = True
            while resubmit:
                try:
                    tx_status = await loop.run_in_executor(None, self.transport.read32, TX_STA_FIFO)
                except usb.core.USBError as e:
                    if e.errno == errno.ENODEV:            # usb_submit_urb fails once the device is gone
                        self._tx_status_reading = False
                        return
                    logger.warning("TX status read failed %s", e)
                    resubmit = self._stop_reading_tx_status()
                    continue
                if tx_status & TX_STA_FIFO_VALID:
                    self._txstatus_fifo.append(tx_status)
                    queue_txdone_work = True
                    continue
                if self._txstatus_timeout():
                    queue_txdone_work = True
                if self._txstatus_pending():
                    hrtimer_delay = TXSTATUS_READ_INTERVAL
                    resubmit = False
                else:
                    resubmit = self._stop_reading_tx_status()
            # txdone_work runs after the resubmitted reads complete, so each one still sees its entry pending.
            if queue_txdone_work:
                self._queue_txdone_work()

    def _stop_reading_tx_status(self) -> bool:
        """The stop_reading tail of rt2800usb_tx_sta_fifo_read_completed [SRC rt2800usb.c:129-140]."""
        self._tx_status_reading = False
        if self._txstatus_pending() and not self._tx_status_reading:
            self._tx_status_reading = True
            return True
        return False

    def _queue_txdone_work(self) -> None:
        """queue_work(txdone_work): a no-op while the work is already queued."""
        if self._txdone_work_queued:
            return
        self._txdone_work_queued = True
        asyncio.get_running_loop().call_soon(self._work_txdone)

    def _work_txdone(self) -> None:
        """rt2800usb_work_txdone [SRC rt2800usb.c:456-476]: rt2800_txdone, then rt2800_txdone_nostatus
        [SRC rt2800lib.c:1121-1156, 1212-1238]."""
        self._txdone_work_queued = False
        while self._txstatus_fifo or self._txstatus_timeout():
            while self._txstatus_fifo:
                qid = (self._txstatus_fifo.popleft() & TX_STA_FIFO_PID_QUEUE) >> 1
                if not self._tx_status_pending[qid]:
                    logger.debug("Got TX status for an empty queue %u, dropping", qid)
                    break
                self._tx_status_pending[qid].popleft()
            for pending in self._tx_status_pending:
                while pending and self._entry_txstatus_timeout(pending[0]):
                    pending.popleft()
            if self._txstatus_pending():
                self._async_read_tx_status()

    def _entry_txstatus_timeout(self, last_action: float) -> bool:
        """rt2800_entry_txstatus_timeout [SRC rt2800lib.c:1158-1178]."""
        return asyncio.get_running_loop().time() > last_action + TXSTATUS_TIMEOUT

    def _txstatus_timeout(self) -> bool:
        """rt2800_txstatus_timeout [SRC rt2800lib.c:1180-1193]."""
        for pending in self._tx_status_pending:
            if pending and self._entry_txstatus_timeout(pending[0]):
                return True
        return False

    def _txstatus_pending(self) -> bool:
        """rt2800_txstatus_pending [SRC rt2800lib.c:1199-1210]."""
        for pending in self._tx_status_pending:
            if pending:
                return True
        return False

    async def _enable_rx_acks(self) -> None:
        """No-op: the Ralink monitor RX filter (RX_FILTER_CFG=0x93, DROP_ACK + DROP_NOT_TO_ME
        clear) already admits the AP's ACK control frames to any RA, so there is nothing to
        enable on the chip (the base arms the tally). Not enter_active_monitor, which makes
        the chip EMIT ACKs."""
        return

    async def _disable_rx_acks(self) -> None:
        """No-op, matching ``_enable_rx_acks``: the monitor RX filter is left untouched."""
        return

    def _dump_tx_state(self, tag: str) -> None:
        """One-shot dump of TX-side register state. Called on the first
        inject_frame so we can see what the chip thinks PA enables / band
        config / chain power-down state look like AFTER channel tune."""
        from .bbp import bbp_read
        from .constants import (
            LDO_CFG0, MAC_STATUS_CFG, MAC_SYS_CTRL, TX_BAND_CFG_REG,
            TX_PIN_CFG_REG, TX_PWR_CFG_0, TX_SW_CFG0, WPDMA_GLO_CFG,
        )
        from .rfcsr import rfcsr_read
        try:
            mac_sys = self.transport.read32(MAC_SYS_CTRL)
            tx_pin = self.transport.read32(TX_PIN_CFG_REG)
            tx_band = self.transport.read32(TX_BAND_CFG_REG)
            tx_sw0 = self.transport.read32(TX_SW_CFG0)
            mac_status = self.transport.read32(MAC_STATUS_CFG)
            wpdma = self.transport.read32(WPDMA_GLO_CFG)
            tx_pwr_cfg_0 = self.transport.read32(TX_PWR_CFG_0)
            ldo_cfg0 = self.transport.read32(LDO_CFG0)
            rfcsr1 = rfcsr_read(self.transport, 1)
            rfcsr2 = rfcsr_read(self.transport, 2)
            rfcsr3 = rfcsr_read(self.transport, 3)
            rfcsr6 = rfcsr_read(self.transport, 6)
            rfcsr12 = rfcsr_read(self.transport, 12)
            rfcsr13 = rfcsr_read(self.transport, 13)
            bbp1 = bbp_read(self.transport, 1)
            logger.debug(
                "[%s] MAC_SYS=0x%08x MAC_STATUS=0x%08x WPDMA=0x%08x "
                "TX_PIN=0x%08x TX_BAND=0x%08x TX_SW0=0x%08x "
                "TX_PWR_0=0x%08x LDO_CFG0=0x%08x BBP1=0x%02x "
                "RFCSR1=0x%02x RFCSR2=0x%02x RFCSR3=0x%02x RFCSR6=0x%02x "
                "RFCSR12=0x%02x RFCSR13=0x%02x",
                tag, mac_sys, mac_status, wpdma,
                tx_pin, tx_band, tx_sw0, tx_pwr_cfg_0, ldo_cfg0, bbp1,
                rfcsr1, rfcsr2, rfcsr3, rfcsr6, rfcsr12, rfcsr13,
            )
        except (IOError, usb.core.USBError) as e:
            logger.debug("[%s] register read failed: %s", tag, e)

    def _dump_tx_counters(self, tag: str) -> None:
        """Read the read-to-clear TX_STA_CNT0/1 aggregates [SRC rt2800.h:1889-1898]."""
        from .constants import TX_STA_CNT0, TX_STA_CNT1
        try:
            cnt0 = self.transport.read32(TX_STA_CNT0)
            cnt1 = self.transport.read32(TX_STA_CNT1)
            logger.debug(
                "[%s] CNT0=0x%08x CNT1=0x%08x → ok=%d fail=%d retry=%d beacon=%d",
                tag, cnt0, cnt1, cnt1 & 0xFFFF, cnt0 & 0xFFFF, (cnt1 >> 16) & 0xFFFF, (cnt0 >> 16) & 0xFFFF,
            )
        except (IOError, usb.core.USBError) as e:
            logger.debug("[%s] TX counter read failed: %s", tag, e)

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Program ``mac`` as the self-MAC with UNICAST_TO_ME_MASK=0xff so the
        autoresponder HW-ACKs frames to it. RX stays promiscuous and the AP's replies
        are addressed to ``mac``, so they still arrive. Reversed by exit_active_monitor."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: write_mac_address(self.transport, bytes(mac), u2me_mask=0xFF),
        )
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the monitor baseline: re-program the real MAC with
        UNICAST_TO_ME_MASK=0 (promiscuous capture, autoresponder matches nothing)."""
        if self._eeprom is None:
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: write_mac_address(self.transport, self._eeprom.mac_address),
        )

    async def close(self) -> None:
        if self._txstatus_timer is not None:
            self._txstatus_timer.cancel()          # hrtimer_cancel [SRC rt2x00usb.c:749]
            try:
                await self._txstatus_timer
            except asyncio.CancelledError:
                pass
            self._txstatus_timer = None
        if self._rx_reader is not None:
            await self._rx_reader.stop()
            self._rx_reader = None
        if self._eeprom is not None and self._claimed:
            try:
                async with self._conf_lock:
                    await asyncio.get_event_loop().run_in_executor(
                        None, lambda: disable_radio(self.transport, self._eeprom))
            except (IOError, usb.core.USBError) as e:
                logger.warning("rt5572 disable_radio: %s", e)
        self._release()
        logger.debug("rt2800usb driver closed")
