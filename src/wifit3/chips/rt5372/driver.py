"""RT5372Driver — clean-room Ralink RT5372 (RT5392, Panda PAU05/PAU06, 148f:5372) driver.

``connect()`` runs the pcap-verified cold bring-up (probe → EFUSE → firmware → MAC/BBP/
RFCSR init → radio-on), then the monitor entry (``enable_monitor``: interface-up filter →
post-radio config → monitor filter) and the default channel tune, and starts the bulk-IN
RX reader. Every wire op through the channel hops is byte-diffed against the cold-boot
capture by ``scripts/verify_pcap.py rt5372`` (the operational gate).

TX (``inject_frame`` → ``tx.send_frame``) is wired but **never fired by
the driver** — injection/deauth is the user's explicit action [[passive_by_default]].

Standalone port (NOT a ``chips/rt2800usb`` DeviceID delta — that shared base is an
imitation port, highly inaccurate per the Pcap Replay, with a confirmed EFUSE addressing
bug); see ``chips/rt5372/RT5372.md``
for the ground-truth facts. RT5392 is 2T2R (txpath=rxpath=2 per the correct EFUSE read).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable, ClassVar, List, Optional

import usb.core
import usb.util

from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.wlan.packet import WlanFrameParser

from ..rx_reader import RxReaderThread
from . import bbp, chan, eeprom, firmware, mac, monitor, rfcsr, tx
from .rx import iter_frames, probe_endpoints, read_rx_burst
from .transport import RT5372Transport

logger = logging.getLogger(__name__)

_VID_RALINK = 0x148F
_PID_RT5372 = 0x5372
_SCAN_START_CHANNEL = 1            # first channel tuned at connect


class RT5372Driver(Driver):
    SUPPORTED_IDS: ClassVar[List[DeviceID]] = [
        DeviceID(_VID_RALINK, _PID_RT5372, "Ralink RT5372/RT5392 (Panda PAU05+06)"),
    ]
    SUPPORTED_CHANNELS: ClassVar[List[int]] = list(range(1, 15))   # 2.4 GHz, 20 MHz
    FAKE_MAC = FakeMacSupport.SPOOFABLE

    def __init__(self, transport: RT5372Transport):
        super().__init__()          # base owns the ACK tally (_ack_detect_on / _our_tx_macs / _ack_counts)
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
        self._on_lost: Optional[Callable[[Exception], None]] = None
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

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        """Sink for a terminal RX-reader failure (unplug). Forwarded to the RxReaderThread's
        on_fatal; resolved at call time so registration order vs connect() can't strand it."""
        self._on_lost = cb

    # ---- bring-up (blocking; run in an executor) --------------------------
    def _bringup(self) -> None:
        """Cold bring-up in wire order — the cold path stays in lockstep with
        ``scripts/verify_pcap.py rt5372`` ``_walk_init`` (the gate replays it). The leading
        warm check is **runtime-only** (the cold capture has no such read, so the gate does
        not model it): on a re-run without a replug the chip is still inited + in monitor with
        RX live (``close()`` disposes the USB handle but never resets the radio), so we skip FW
        upload + the whole MAC/BBP/RFCSR init + ``enable_monitor`` and let ``connect()`` resume
        — faster, and it avoids re-initialising a running radio (which reads worse than cold)."""
        t = self.transport
        try:
            if t.dev.is_kernel_driver_active(0):
                t.dev.detach_kernel_driver(0)
                logger.info("rt5372: detached kernel driver from interface 0")
        except (NotImplementedError, usb.core.USBError) as e:
            logger.debug("rt5372: kernel-driver detach skipped: %s", e)
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
        self._drv = rfcsr.init_rfcsr_5392(t, chip, ev)        # RFCSR init (no RX-filter cal)
        mac.enable_radio_finish(t, chip, ev)                   # RX/LED (no MCU_CURRENT)
        mac.set_radio_led(t, ev)                                # leds-radio on
        mac.start_queue_rx(t)                                   # enable RX queue

        monitor.enable_monitor(t, chip, ev, self._drv,         # airmon monitor entry
                               short_retry=self.DEFAULT_HW_ACK_RETRIES)

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
        except Exception as e:  # noqa: BLE001
            raise BringUpError("bring-up", str(e)) from e

        eps = probe_endpoints(self.transport.dev)
        self._bulk_in_ep = eps.primary_bulk_in
        # MGMT/inject frames go on the first TX queue's endpoint = the lowest-numbered
        # bulk-OUT (rt2x00usb assigns endpoints to queues in descriptor order).
        self._bulk_out_ep = min(eps.bulk_out) if eps.bulk_out else None
        mode = "WARM reattach (skipped FW + init)" if self.is_warm else "cold bring-up"
        # Resolve the RF companion chip from the runtime EEPROM so this driver runs on
        # ANY 148f:5372 card regardless of EEPROM contents (RF5372 2T2R reference /
        # RF5370 1T1R / RF5390 / RF5392 / erased). [SRC rt2800lib.c:11182-11235].
        rf = eeprom.resolve_rf_chip(self._chip.rt, self._eeprom)
        logger.info(
            "RT5372 %s: mac=%s silicon=0x%04x rf=%s %dT%dR ext_lna_2g=%s bt_coex=%s "
            "freq_off=%d eeprom=%s", mode, self.mac_address, self._chip.rt, rf.name,
            self._eeprom.tx_chain_num, self._eeprom.rx_chain_num,
            self._eeprom.external_lna_bg, self._eeprom.bt_coexist, self._eeprom.freq_offset,
            "unburned" if self._eeprom.looks_unburned else "burned")
        if not rf.ported and not self._eeprom.looks_unburned:
            logger.warning(
                "untested variant: EEPROM RF chip %s on RT5392 silicon has no ported "
                "config_channel path — running the silicon-default rf53xx tune "
                "(kernel would -ENODEV)", rf.name)

        if progress_cb:
            progress_cb(0.5, f"RT5372: {mode}")
            progress_cb(0.9, f"Tuning to channel {_SCAN_START_CHANNEL}")
        await self.set_channel(_SCAN_START_CHANNEL)

        # bulk-IN RX reader on a dedicated thread (off the event loop so the TUI can't
        # starve RX); each aggregated buffer → 802.11 frames + RSSI → rx callback.
        self._reader = RxReaderThread(
            loop, self._read_once, self._dispatch, name="rt5372-rx",
            on_fatal=lambda e: self._on_lost and self._on_lost(e))
        self._reader.start()

        if progress_cb:
            progress_cb(1.0, f"RT5372 tuned to channel {_SCAN_START_CHANNEL} @ 20 MHz")
        return True

    def _read_once(self) -> Optional[bytes]:
        return read_rx_burst(self.transport.dev, self._bulk_in_ep)

    def _dispatch(self, buf: bytes) -> None:
        cb = self._rx_cb
        if cb is None and not self._ack_detect_on:
            return
        for frame, rssi in iter_frames(buf, self._eeprom, self._lna_gain):
            # A 10-byte 0xD4 frame is an ACK (the parser drops control frames); the base tallies
            # it iff the ACK tap is armed and RA=frame[4:10] is a MAC we inject as.
            if len(frame) == 10 and frame[0] == 0xD4:
                self.record_ack(frame)
                continue
            if cb is not None:
                parsed = WlanFrameParser.parse_80211_frame(frame, rssi)
                if parsed is not None:
                    cb(parsed)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, self._tune, channel)
        self._channel = channel
        return True

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Transmit one 802.11 frame (e.g. deauth / WEP replay) on the MGMT bulk-OUT pipe.
        The TXWI ACK bit is set ON so the chip retries up to the global TX_RTY_CFG
        SHORT_RTY_LIMIT = ``self.DEFAULT_HW_ACK_RETRIES`` (set at monitor entry). The seq is
        already stamped by the base. Explicit-action only — nothing on the scan/connect path
        calls this [[passive_by_default]]. Serialized via ``_io_lock`` so it never races a retune."""
        if not frame_bytes:
            return False
        if self._bulk_out_ep is None:
            logger.error("RT5372 inject: no bulk-OUT endpoint")
            return False
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, self._do_inject, frame_bytes)
        return True

    async def _enable_rx_acks(self) -> None:
        """No-op: the Ralink monitor RX filter (RX_FILTER_CFG=0x93, DROP_ACK + DROP_NOT_TO_ME
        clear) already admits the AP's ACK control frames to any RA, so there is nothing to
        enable on the chip (the base arms the tally). Not enter_active_monitor, which makes
        the chip EMIT ACKs."""
        return

    async def _disable_rx_acks(self) -> None:
        """No-op, matching ``_enable_rx_acks``: the monitor RX filter is left untouched."""
        return

    def _do_inject(self, frame: bytes) -> None:
        # Executor thread; share _hw_lock with _tune so an inject can never collide with
        # an in-flight (or cancelled-but-draining) channel tune on the USB device. ACK bit ON
        # (use_no_ack=False) so the chip requests the AP's ACK + retries un-ACKed frames.
        with self._hw_lock:
            tx.send_frame(self.transport.dev, self._bulk_out_ep, frame, use_no_ack=False)

    def _stamp_tx_seq(self, frame: bytes) -> bytes:
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
        autoresponder HW-ACKs frames to it (rt5372's cold path never set a self-MAC).
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
        if self._reader is not None:
            await self._reader.stop()
            self._reader = None
        try:
            usb.util.dispose_resources(self.transport.dev)
        except Exception:  # noqa: BLE001
            pass
