"""RT5372Driver — clean-room Ralink RT5372 (RT5392, Panda PAU05/PAU06, 148f:5372) driver.

``connect()`` runs the pcap-verified cold bring-up (probe → EFUSE → firmware → MAC/BBP/
RFCSR init → radio-on), then the monitor entry (``enable_monitor``: interface-up filter →
post-radio config → monitor filter) and the default channel tune, and starts the bulk-IN
RX reader. Every wire op through the channel hops is byte-diffed against the cold-boot
capture by ``scripts/verify_pcap.py rt5372`` (the operational gate).

TX (``inject_frame`` → ``tx.send_frame``) is wired and pcap-faithful but **never fired by
the driver** — injection/deauth is the user's explicit action [[passive_by_default]].

Standalone port (NOT a ``chips/rt2800usb`` DeviceID delta — that shared base is an
unfaithful imitation with a confirmed EFUSE addressing bug); see ``chips/rt5372/RT5372.md``
for the ground-truth facts. RT5392 is 2T2R (txpath=rxpath=2 per the correct EFUSE read).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable, ClassVar, List, Optional

import usb.core
import usb.util

from wifit3.engine.protocols import DeviceID, ProgressCallback
from wifit3.wlan.packet import WlanFrameParser

from ..rx_reader import RxReaderThread
from . import bbp, chan, eeprom, firmware, mac, monitor, rfcsr, tx
from .rx import iter_frames, probe_endpoints, read_rx_burst
from .transport import RT5372Transport

logger = logging.getLogger(__name__)

_VID_RALINK = 0x148F
_PID_RT5372 = 0x5372
_SCAN_START_CHANNEL = 1            # first channel tuned at connect


class RT5372Driver:
    SUPPORTED_IDS: ClassVar[List[DeviceID]] = [
        DeviceID(_VID_RALINK, _PID_RT5372, "Ralink RT5372 (RT5392) 2T2R / Panda PAU05+PAU06"),
    ]
    SUPPORTED_CHANNELS: ClassVar[List[int]] = list(range(1, 15))   # 2.4 GHz, 20 MHz

    def __init__(self, transport: RT5372Transport):
        self.transport = transport
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self._chip = None
        self._eeprom: Optional[eeprom.EepromValues] = None
        self._drv = None                 # RT5392 threads no init-derived calibration (None)
        self._channel: Optional[int] = None
        self._lna_gain: int = 0          # current-channel LNA gain (RSSI conversion)
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
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RT5372Driver":
        return cls(RT5372Transport(dev))

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_cb = cb

    # ---- bring-up (blocking; run in an executor) --------------------------
    def _bringup(self) -> None:
        """Deterministic cold bring-up, in wire order — keep in lockstep with
        ``scripts/verify_pcap.py rt5372`` ``_walk_init`` (the gate replays this sequence)."""
        t = self.transport
        self._chip = mac.probe_rt(t)                              # MAC_CSR0 id/rev
        buf = eeprom.read_eeprom_efuse(t)                        # autorun + EFUSE (word offset)
        buf = eeprom.validate_eeprom(buf)                        # blank-field fix-up (no-op here)
        self._eeprom = eeprom.parse_eeprom(buf)
        self.mac_address = ":".join(f"{b:02x}" for b in self._eeprom.mac)
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
        self._drv = rfcsr.init_rfcsr_5392(t, chip, ev)        # RFCSR init (no RX-filter cal)
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
            progress_cb(0.1, "RT5372: probe + EFUSE + firmware + radio bring-up")
        try:
            await loop.run_in_executor(None, self._bringup)
        except Exception:  # noqa: BLE001
            logger.exception("RT5372 bring-up failed")
            if progress_cb:
                progress_cb(1.0, "RT5372 bring-up failed")
            return False

        eps = probe_endpoints(self.transport.dev)
        self._bulk_in_ep = eps.primary_bulk_in
        # MGMT/inject frames go on the first TX queue's endpoint = the lowest-numbered
        # bulk-OUT (rt2x00usb assigns endpoints to queues in descriptor order).
        self._bulk_out_ep = min(eps.bulk_out) if eps.bulk_out else None
        logger.info("RT5372 EFUSE: mac=%s rf=0x%04x %dT%dR ext_lna_2g=%s freq_off=%d",
                    self.mac_address, self._eeprom.rf_type, self._eeprom.tx_chain_num,
                    self._eeprom.rx_chain_num, self._eeprom.external_lna_bg,
                    self._eeprom.freq_offset)

        if progress_cb:
            progress_cb(0.9, f"Tuning to channel {_SCAN_START_CHANNEL}")
        await self.set_channel(_SCAN_START_CHANNEL)

        # bulk-IN RX reader on a dedicated thread (off the event loop so the TUI can't
        # starve RX); each aggregated buffer → 802.11 frames + RSSI → rx callback.
        self._reader = RxReaderThread(loop, self._read_once, self._dispatch, name="rt5372-rx")
        self._reader.start()

        if progress_cb:
            progress_cb(1.0, f"RT5372 tuned to channel {_SCAN_START_CHANNEL} @ 20 MHz")
        return True

    def _read_once(self) -> Optional[bytes]:
        return read_rx_burst(self.transport.dev, self._bulk_in_ep)

    def _dispatch(self, buf: bytes) -> None:
        cb = self._rx_cb
        if cb is None:
            return
        for frame, rssi in iter_frames(buf, self._eeprom, self._lna_gain):
            parsed = WlanFrameParser.parse_80211_frame(frame, rssi)
            if parsed is not None:
                cb(parsed)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, self._tune, channel)
        self._channel = channel
        return True

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        """Transmit one 802.11 frame (e.g. deauth / WEP replay) on the MGMT bulk-OUT
        pipe. Explicit-action only — nothing on the scan/connect path calls this
        [[passive_by_default]]. Serialized via ``_io_lock`` so it never races a retune."""
        if not frame_bytes:
            return False
        if self._bulk_out_ep is None:
            logger.error("RT5372 inject: no bulk-OUT endpoint")
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

    async def close(self) -> None:
        if self._reader is not None:
            await self._reader.stop()
            self._reader = None
        try:
            usb.util.dispose_resources(self.transport.dev)
        except Exception:  # noqa: BLE001
            pass
