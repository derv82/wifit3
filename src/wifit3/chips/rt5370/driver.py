"""RT5370Driver — clean-room Ralink RT5370 (RT5390, RF5370, 1T1R, 148f:5370) driver.

``connect()`` runs the pcap-verified cold bring-up (probe → EFUSE → firmware → MAC/BBP/
RFCSR init → radio-on), then the monitor entry (``enable_monitor``: interface-up filter →
post-radio config → monitor filter) and the default channel tune, and starts the bulk-IN
RX reader. Every wire op through the channel hops is byte-diffed against the cold-boot
capture by ``scripts/verify_pcap.py rt5370`` (the operational gate).

TX (``inject_frame`` → ``tx.send_frame``) is wired but **never fired by
the driver** — injection/deauth is the user's explicit action [[passive_by_default]].

Standalone port (NOT a ``chips/rt2800usb`` DeviceID delta — that shared base is an
imitation port, highly inaccurate per the Pcap Replay, with a confirmed EFUSE addressing
bug). It mirrors the module shape of the 2T2R sibling ``chips/rt5372/`` but shares no
code; see ``chips/rt5370/RT5370.md`` for the ground-truth facts. RT5370 is RT5390
silicon, 1T1R (txpath=rxpath=1), RF5370, rev 0x0502 (REV_RT5390F).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable, ClassVar, List, Optional

import usb.core
import usb.util

from wifit3.engine.protocols import DeviceID, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.wlan.packet import WlanFrameParser

from ..rx_reader import RxReaderThread
from . import bbp, chan, eeprom, firmware, link_tuner, mac, monitor, rfcsr, tx
from .rx import iter_frames, probe_endpoints, read_rx_burst
from .transport import RT5370Transport

logger = logging.getLogger(__name__)

_VID_RALINK = 0x148F
_PID_RT5370 = 0x5370
_SCAN_START_CHANNEL = 1            # first channel tuned at connect
_AGC_INTERVAL_S = 1.0            # link-tuner cadence (kernel rt2x00link runs ~1 Hz)
_RSSI_EWMA_N = 8                 # EWMA window: ewma = (ewma*(N-1) + rssi)/N


class RT5370Driver:
    SUPPORTED_IDS: ClassVar[List[DeviceID]] = [
        DeviceID(_VID_RALINK, _PID_RT5370, "Ralink RT5370 (RT5390) 1T1R"),
    ]
    SUPPORTED_CHANNELS: ClassVar[List[int]] = list(range(1, 15))   # 2.4 GHz, 20 MHz
    FAKE_MAC = FakeMacSupport.SPOOFABLE

    def __init__(self, transport: RT5370Transport):
        self.transport = transport
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self._chip = None
        self._eeprom: Optional[eeprom.EepromValues] = None
        self._drv = None                 # RT5390 threads no init-derived calibration (None)
        self._channel: Optional[int] = None
        self._lna_gain: int = 0          # current-channel LNA gain (RSSI conversion)
        # Monitor-mode AGC (link tuner). The kernel runs rt2800_link_tuner only for STA/AP
        # vifs, so in monitor BBP66 is pinned to the per-tune default and a very-close AP
        # compresses the front-end (measured: a -36 dBm AP self-strangled 4→8 bcn/s on ch9
        # until BBP66 backed off). We run the kernel's own heuristic (+0x10 when the RX RSSI
        # EWMA beats -80 dBm) on a ~1 Hz task. Runtime-only — the cold-boot capture has no
        # link-tuner writes (kernel monitor vif), so verify_pcap doesn't model it, stays green.
        self._rssi_ewma: Optional[float] = None          # EWMA of RX RSSI (steers the tuner)
        self._vgc_level: Optional[int] = None            # last BBP66 the tuner wrote
        self._agc_task: Optional[asyncio.Task] = None
        self._bulk_in_ep: Optional[int] = None
        self._bulk_out_ep: Optional[int] = None
        self._tx_seq: int = 0            # running 802.11 seq stamped into injected frames
        self._rx_cb: Optional[Callable[[dict], None]] = None
        self._reader: Optional[RxReaderThread] = None
        self._io_lock = asyncio.Lock()   # serialize the COROUTINES (set_channel vs inject)
        # The REAL hardware serializer. asyncio.Lock guards the coroutine, but a coroutine
        # cancelled mid-`run_in_executor` (UI view switch → hop task cancel) releases it
        # while its executor THREAD keeps running config_channel — and a new set_channel's
        # thread then collides on the USB control endpoint and wedges the chip's RX-DMA.
        # A threading.Lock held by the executor work blocks that second thread until the
        # first (even a cancelled one) finishes.
        self._hw_lock = threading.Lock()

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RT5370Driver":
        return cls(RT5370Transport(dev))

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_cb = cb

    # ---- bring-up (blocking; run in an executor) --------------------------
    def _bringup(self) -> None:
        """Cold bring-up in wire order — the cold path stays in lockstep with
        ``scripts/verify_pcap.py rt5370`` ``_walk_init`` (the gate replays it). The leading
        warm check is **runtime-only** (the cold capture has no such read, so the gate does
        not model it): on a re-run without a replug the chip is still inited + in monitor with
        RX live (``close()`` disposes the USB handle but never resets the radio), so we skip FW
        upload + the whole MAC/BBP/RFCSR init + ``enable_monitor`` and let ``connect()`` resume
        — faster, and it avoids re-initialising a running radio (which reads worse than cold)."""
        t = self.transport
        try:
            if t.dev.is_kernel_driver_active(0):
                t.dev.detach_kernel_driver(0)
                logger.info("rt5370: detached kernel driver from interface 0")
        except (NotImplementedError, usb.core.USBError) as e:
            logger.debug("rt5370: kernel-driver detach skipped: %s", e)
        self._chip = mac.probe_rt(t)                              # MAC_CSR0 id/rev
        buf = eeprom.read_eeprom_efuse(t)                        # autorun + EFUSE (word offset)
        buf = eeprom.validate_eeprom(buf)                        # blank-field fix-up (no-op here)
        self._eeprom = eeprom.parse_eeprom(buf)
        self.mac_address = ":".join(f"{b:02x}" for b in self._eeprom.mac)

        if mac.is_chip_warm(t):
            self.is_warm = True                                 # resume — see docstring
            return
        self.is_warm = False

        mac.probe_hw_gpio(t)                                     # rfkill GPIO dir

        firmware.upload(t, firmware.load_firmware_blob())        # FW load + MCU boot

        ev, chip = self._eeprom, self._chip
        mac.set_radio_led(t, ev)                                 # radio LED on
        mac.wakeup(t)                                            # STATE_AWAKE
        mac.usb_enable_radio_dma(t)                              # USB DMA aggregation
        t.wait_wpdma_ready()                                     # enable_radio prologue
        mac.init_registers(t, chip, ev)                         # MAC config block
        mac.enable_radio_boot(t)                                # BBP/RF ready + boot signal
        bbp.init_bbp(t, chip, ev)                              # BBP init (53xx + EEPROM)
        self._drv = rfcsr.init_rfcsr_5390(t, chip, ev)        # RFCSR init (no RX-filter cal)
        mac.enable_radio_finish(t, chip, ev)                   # RX/LED (no MCU_CURRENT)
        mac.set_radio_led(t, ev)                                # leds-radio on
        mac.start_queue_rx(t)                                   # enable RX queue

        monitor.enable_monitor(t, chip, ev, self._drv)         # airmon monitor entry

    def _tune(self, channel: int) -> None:
        # Runs on an executor thread; _hw_lock guarantees only one hardware op touches
        # the device at a time even when a cancelled tune's thread is still draining.
        with self._hw_lock:
            chan.set_channel(self.transport, self._chip, self._eeprom, self._drv, channel)
            self._lna_gain = chan.config_lna_gain(self._eeprom, channel)

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        loop = asyncio.get_running_loop()

        if progress_cb:
            progress_cb(0.1, "RT5370: probe + EFUSE + firmware + radio bring-up")
        try:
            await loop.run_in_executor(None, self._bringup)
        except Exception as e:  # noqa: BLE001
            raise BringUpError("bring-up", str(e)) from e

        eps = probe_endpoints(self.transport.dev)
        self._bulk_in_ep = eps.primary_bulk_in
        # MGMT/inject frames go on the first TX queue's endpoint = the lowest-numbered
        # bulk-OUT (rt2x00usb assigns endpoints to queues in descriptor order).
        self._bulk_out_ep = min(eps.bulk_out) if eps.bulk_out else None
        mode = "WARM reattach (skipped FW + init)" if self.is_warm else "cold bring-up"
        logger.info("RT5370 %s: mac=%s rf=0x%04x %dT%dR ext_lna_2g=%s freq_off=%d",
                    mode, self.mac_address, self._eeprom.rf_type, self._eeprom.tx_chain_num,
                    self._eeprom.rx_chain_num, self._eeprom.external_lna_bg,
                    self._eeprom.freq_offset)

        if progress_cb:
            progress_cb(0.5, f"RT5370: {mode}")
            progress_cb(0.9, f"Tuning to channel {_SCAN_START_CHANNEL}")
        await self.set_channel(_SCAN_START_CHANNEL)

        # bulk-IN RX reader on a dedicated thread (off the event loop so the TUI can't
        # starve RX); each aggregated buffer → 802.11 frames + RSSI → rx callback.
        self._reader = RxReaderThread(loop, self._read_once, self._dispatch, name="rt5370-rx")
        self._reader.start()
        self._agc_task = loop.create_task(self._agc_loop())   # monitor-mode link tuner

        if progress_cb:
            progress_cb(1.0, f"RT5370 tuned to channel {_SCAN_START_CHANNEL} @ 20 MHz")
        return True

    def _read_once(self) -> Optional[bytes]:
        return read_rx_burst(self.transport.dev, self._bulk_in_ep)

    def _dispatch(self, buf: bytes) -> None:
        # Runs on the event loop (RxReaderThread posts via call_soon_threadsafe), so the AGC
        # EWMA update here shares a thread with the AGC task — no lock needed on the float.
        cb = self._rx_cb
        for frame, rssi in iter_frames(buf, self._eeprom, self._lna_gain):
            self._rssi_ewma = (rssi if self._rssi_ewma is None
                               else (self._rssi_ewma * (_RSSI_EWMA_N - 1) + rssi) / _RSSI_EWMA_N)
            if cb is None:
                continue
            parsed = WlanFrameParser.parse_80211_frame(frame, rssi)
            if parsed is not None:
                cb(parsed)

    # ---- monitor-mode AGC (link tuner) ------------------------------------
    def _apply_agc(self, rssi: int) -> None:
        # Executor thread; _hw_lock serializes BBP66 vs set_channel's reset_tuner and inject.
        with self._hw_lock:
            self._vgc_level = link_tuner.link_tuner(
                self.transport, self._chip, self._lna_gain, rssi,
                self._vgc_level if self._vgc_level is not None else -1)

    async def _agc_loop(self) -> None:
        """~1 Hz monitor-mode link tuner: back BBP66 off (+0x10) once the RX RSSI EWMA beats
        -80 dBm, the kernel's rt2800_link_tuner heuristic run in a context the kernel skips."""
        loop = asyncio.get_running_loop()
        try:
            while True:
                await asyncio.sleep(_AGC_INTERVAL_S)
                # Share _io_lock with set_channel/inject so an AGC write can't land mid-tune.
                async with self._io_lock:
                    rssi = self._rssi_ewma
                    if rssi is not None:
                        await loop.run_in_executor(None, self._apply_agc, int(rssi))
        except asyncio.CancelledError:
            pass

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, self._tune, channel)
            # The tune just wrote BBP66 = default (reset_tuner); re-seed the AGC from there so
            # it only writes when it backs the gain off, and drop the previous channel's EWMA.
            self._rssi_ewma = None
            self._vgc_level = link_tuner.get_default_vgc(self._chip, self._lna_gain)
        self._channel = channel
        return True

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        """Transmit one 802.11 frame (e.g. deauth / WEP replay) on the MGMT bulk-OUT
        pipe. Explicit-action only — nothing on the scan/connect path calls this
        [[passive_by_default]]. Serialized via ``_io_lock`` so it never races a retune."""
        if not frame_bytes:
            return False
        if self._bulk_out_ep is None:
            logger.error("RT5370 inject: no bulk-OUT endpoint")
            return False
        frame = self._stamp_seq(frame_bytes)
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, self._do_inject, frame, use_no_ack)
        return True

    def _do_inject(self, frame: bytes, use_no_ack: bool) -> None:
        # Executor thread; share _hw_lock with _tune so an inject can never collide with
        # an in-flight (or cancelled-but-draining) channel tune on the USB device.
        with self._hw_lock:
            tx.send_frame(self.transport.dev, self._bulk_out_ep, frame, use_no_ack=use_no_ack)

    def _stamp_seq(self, frame: bytes) -> bytes:
        """Stamp the next running sequence number into the frame's seqctl (bytes 22-23,
        ``seqnum << 4`` little-endian) — TXWI NSEQ=0 means the chip transmits the frame's
        own seqctl, so without this every inject shares seq=0 and a receiver's duplicate
        filter drops all but the first. Returns a copy; caller's bytes untouched."""
        if len(frame) < 24:
            return frame
        seq = self._tx_seq & 0xFFF
        self._tx_seq = (self._tx_seq + 1) & 0xFFF
        buf = bytearray(frame)
        buf[22:24] = ((seq << 4) & 0xFFFF).to_bytes(2, "little")
        return bytes(buf)

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Program ``mac`` as the self-MAC with UNICAST_TO_ME_MASK=0xff so the
        autoresponder HW-ACKs frames to it (rt5370's cold path never set a self-MAC).
        Reversed by exit_active_monitor."""
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, self._set_self_mac, bytes(mac), 0xFF)
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the monitor baseline: real EFUSE MAC + UNICAST_TO_ME_MASK=0."""
        if self._eeprom is None:
            return
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, self._set_self_mac, self._eeprom.mac, 0x00)

    def _set_self_mac(self, mac_bytes: bytes, u2me_mask: int) -> None:
        with self._hw_lock:
            mac.write_mac_address(self.transport, mac_bytes, u2me_mask=u2me_mask)

    async def close(self) -> None:
        if self._agc_task is not None:
            self._agc_task.cancel()
            try:
                await self._agc_task
            except asyncio.CancelledError:
                pass
            self._agc_task = None
        if self._reader is not None:
            await self._reader.stop()
            self._reader = None
        try:
            usb.util.dispose_resources(self.transport.dev)
        except Exception:  # noqa: BLE001
            pass
