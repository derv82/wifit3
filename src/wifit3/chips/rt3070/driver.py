"""RT3070Driver — clean-room Ralink RT3070 (ALFA AWUS036NH, 148f:3070) driver.

``connect()`` runs the pcap-verified cold bring-up (probe → EFUSE → firmware → MAC/BBP/
RFCSR init + RX-filter calibration → radio-on), then the monitor entry (``enable_monitor``:
interface-up filter → post-radio config → monitor filter) and the default channel tune,
and starts the bulk-IN RX reader. Every wire op through the channel hops is byte-diffed
against the cold-boot capture by ``scripts/verify_pcap.py rt3070`` (the operational gate).

TX (``inject_frame`` → ``tx.send_frame``) is wired but **never fired by
the driver** — injection/deauth is the user's explicit action [[passive_by_default]].

Standalone port (NOT a ``chips/rt2800usb`` DeviceID delta — that shared base has a
confirmed EFUSE addressing bug); see ``chips/rt3070/RT3070.md`` for the ground-truth facts.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable, ClassVar, List, Optional

import usb.core
import usb.util

from wifit3.engine.protocols import DeviceID, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.wlan.packet import WlanFrameParser

from ..rx_reader import RxReaderThread
from . import bbp, chan, eeprom, firmware, mac, monitor, rfcsr, tx
from .rx import iter_frames, probe_endpoints, read_rx_burst
from .transport import RT3070Transport

logger = logging.getLogger(__name__)

_VID_RALINK = 0x148F
_PID_RT3070 = 0x3070
_SCAN_START_CHANNEL = 1            # first channel tuned at connect


class RT3070Driver:
    SUPPORTED_IDS: ClassVar[List[DeviceID]] = [
        DeviceID(_VID_RALINK, _PID_RT3070, "Ralink RT3070 (ALFA AWUS036NH)"),
    ]
    SUPPORTED_CHANNELS: ClassVar[List[int]] = list(range(1, 15))   # 2.4 GHz, 20 MHz
    FAKE_MAC = FakeMacSupport.SPOOFABLE

    def __init__(self, transport: RT3070Transport):
        self.transport = transport
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self._chip = None
        self._eeprom: Optional[eeprom.EepromValues] = None
        self._rf: Optional[eeprom.RfChip] = None   # RF companion resolved from the EEPROM
        self._drv = None                 # DrvData (RX-filter calibration) from init
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
        # first (even a cancelled one) finishes. Verified against wifit3.log @ 20:34:22.
        self._hw_lock = threading.Lock()
        # Observe the AP's ACK to our injects (did our TX land). Off by default.
        # The monitor RX filter (RX_FILTER_CFG=0x93, DROP_ACK/DROP_NOT_TO_ME clear)
        # already admits the AP's ACK to any RA, so arming is a pure software flag —
        # no register write (unlike the Realtek/MediaTek ports whose monitor RCR drops
        # control frames).
        self._ack_detect_on: bool = False
        self._our_tx_macs: set[bytes] = set()      # source MACs we inject as
        self._ack_sightings: dict[str, int] = {}   # our-MAC -> ACK count
        self._all_acks_seen: int = 0
        self._ack_last_ts: dict[bytes, float] = {}  # our-MAC -> ts of last ACK
        self._tx_frames: int = 0
        self._tx_unacked: int = 0

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RT3070Driver":
        return cls(RT3070Transport(dev))

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_cb = cb

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        """Sink for a terminal RX-reader failure (unplug). Forwarded to the RxReaderThread's
        on_fatal; resolved at call time so registration order vs connect() can't strand it."""
        self._on_lost = cb

    # ---- bring-up (blocking; run in an executor) --------------------------
    def _bringup(self) -> None:
        """Cold bring-up in wire order — the cold path stays in lockstep with
        ``scripts/verify_pcap.py`` ``_walk_init`` (the gate replays it). The leading warm
        check is **runtime-only** (the cold capture has no such read, so the gate does not
        model it): on a re-run without a replug the chip is still inited + in monitor with RX
        live (``close()`` disposes the USB handle but never resets the radio), so we skip FW
        upload + the whole MAC/BBP/RFCSR init + ``enable_monitor`` and let ``connect()``
        resume — faster, and it avoids re-initialising a running radio."""
        t = self.transport
        try:
            if t.dev.is_kernel_driver_active(0):
                t.dev.detach_kernel_driver(0)
                logger.info("rt3070: detached kernel driver from interface 0")
        except (NotImplementedError, usb.core.USBError) as e:
            logger.debug("rt3070: kernel-driver detach skipped: %s", e)
        self._chip = mac.probe_rt(t)                              # MAC_CSR0 id/rev
        buf = eeprom.read_eeprom_efuse(t)                        # autorun + EFUSE (word offset)
        buf = eeprom.validate_eeprom(buf)                        # blank-field fix-up (no-op here)
        self._eeprom = eeprom.parse_eeprom(buf)
        self.mac_address = ":".join(f"{b:02x}" for b in self._eeprom.mac)

        # Runtime EEPROM config identification, ported from rt2800_init_eeprom so this driver
        # runs on ANY 148f:3070 regardless of EEPROM contents (not just the RF3020 reference).
        ev, chip = self._eeprom, self._chip
        self._rf = eeprom.resolve_rf_chip(ev)
        logger.info(
            "RT3070 config: silicon=0x%04x rev=0x%04x rf=%s antenna=%dT%dR freq_off=%d "
            "ext_lna_bg=%s ext_tx_alc=%s ant_div=%d power_limit=%s",
            chip.rt, chip.rev, self._rf.name, ev.tx_chain_num, ev.rx_chain_num,
            ev.freq_offset, ev.external_lna_bg, ev.external_tx_alc, ev.ant_diversity,
            ev.power_limit,
        )
        if not self._rf.ported and self._rf.rf_id != 0:
            logger.warning(
                "RT3070 untested variant: EEPROM RF chip %s is not in the ported rf3xxx set; "
                "running the RT30xx silicon-default channel tune (the kernel would -ENODEV). "
                "Reference card is RF3020.", self._rf.name,
            )

        if mac.is_chip_warm(t):
            self.is_warm = True
            # The RX-filter cal already ran on the first cold boot; recover its result from
            # the chip (RFCSR24) — config_channel needs it every tune (unlike rt5372/RF53xx).
            self._drv = rfcsr.recover_drv_data(t)
            return
        self.is_warm = False

        mac.probe_hw_gpio(t)                                     # rfkill GPIO dir

        firmware.upload(t, firmware.load_firmware_blob())        # FW load + MCU boot

        mac.set_radio_led(t, ev)                                 # radio LED on
        mac.wakeup(t)                                            # STATE_AWAKE
        mac.usb_enable_radio_dma(t)                              # USB DMA aggregation
        t.wait_wpdma_ready()                                     # enable_radio prologue
        mac.init_registers(t, chip, ev)                         # MAC config block
        mac.enable_radio_boot(t)                                # BBP/RF ready + boot signal
        bbp.init_bbp(t, chip, ev)                              # BBP init (30xx + EEPROM)
        self._drv = rfcsr.init_rfcsr_30xx(t, chip, ev)         # RFCSR init + RX-filter cal
        mac.enable_radio_finish(t, chip, ev)                   # MCU current / RX / LED
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
            progress_cb(0.1, "RT3070: probe + EFUSE + firmware + radio bring-up")
        try:
            await loop.run_in_executor(None, self._bringup)
        except Exception as e:  # noqa: BLE001
            raise BringUpError("bring-up", str(e)) from e

        eps = probe_endpoints(self.transport.dev)
        self._bulk_in_ep = eps.primary_bulk_in
        # MGMT/inject frames go on the first TX queue's endpoint = the lowest-numbered
        # bulk-OUT (rt2x00usb assigns endpoints to queues in descriptor order). Confirmed
        # on capture-1: every aireplay deauth (FC=0xc0) rode bulk-OUT EP 0x01.
        self._bulk_out_ep = min(eps.bulk_out) if eps.bulk_out else None
        mode = "WARM reattach (skipped FW + init)" if self.is_warm else "cold bring-up"
        logger.info("RT3070 %s: mac=%s rf=%s %dT%dR ext_lna_2g=%s freq_off=%d",
                    mode, self.mac_address, self._rf.name, self._eeprom.tx_chain_num,
                    self._eeprom.rx_chain_num, self._eeprom.external_lna_bg,
                    self._eeprom.freq_offset)

        if progress_cb:
            progress_cb(0.5, f"RT3070: {mode}")
            progress_cb(0.9, f"Tuning to channel {_SCAN_START_CHANNEL}")
        await self.set_channel(_SCAN_START_CHANNEL)

        # bulk-IN RX reader on a dedicated thread (off the event loop so the TUI can't
        # starve RX); each aggregated buffer → 802.11 frames + RSSI → rx callback.
        self._reader = RxReaderThread(loop, self._read_once, self._dispatch, name="rt3070-rx",
                                      on_fatal=lambda e: self._on_lost and self._on_lost(e))
        self._reader.start()

        if progress_cb:
            progress_cb(1.0, f"RT3070 tuned to channel {_SCAN_START_CHANNEL} @ 20 MHz")
        return True

    def _read_once(self) -> Optional[bytes]:
        return read_rx_burst(self.transport.dev, self._bulk_in_ep)

    def _dispatch(self, buf: bytes) -> None:
        cb = self._rx_cb
        if cb is None and not self._ack_detect_on:
            return
        for frame, rssi in iter_frames(buf, self._eeprom, self._lna_gain):
            # A 10-byte 0xD4 frame is an ACK (the parser drops control frames). RA=frame[4:10]
            # is the STA the AP ACKed; when armed, keep only ACKs to a MAC we inject as.
            if self._ack_detect_on and len(frame) == 10 and frame[0] == 0xD4:
                self._all_acks_seen += 1
                ra = bytes(frame[4:10])
                if ra in self._our_tx_macs:
                    self._ack_sightings[ra.hex()] = self._ack_sightings.get(ra.hex(), 0) + 1
                    self._ack_last_ts[ra] = time.monotonic()   # for inject wait-for-ack
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

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True,
                           wait_for_ack: float = 0.0, max_resends: int = 0) -> bool:
        """Transmit one 802.11 frame (e.g. deauth / WEP replay) on the MGMT bulk-OUT
        pipe. Explicit-action only — nothing on the scan/connect path calls this
        [[passive_by_default]]. Serialized via ``_io_lock`` so it never races a retune.

        ``wait_for_ack > 0`` (with TX-ACK detection armed) waits for the AP's ACK and
        resends the identical frame up to ``max_resends`` times if none comes, returning
        whether it landed; ``0`` = fire-and-forget (current behaviour)."""
        if not frame_bytes:
            return False
        if self._bulk_out_ep is None:
            logger.error("RT3070 inject: no bulk-OUT endpoint")
            return False
        frame = self._stamp_seq(frame_bytes)   # stamp once — a resend re-sends the identical frame
        ta = bytes(frame[10:16]) if len(frame) >= 16 else None   # AP ACKs back to this
        if self._ack_detect_on and ta is not None:
            self._our_tx_macs.add(ta)
        loop = asyncio.get_running_loop()
        ack_gated = wait_for_ack > 0 and self._ack_detect_on and ta is not None
        for _ in range(max_resends + 1):
            async with self._io_lock:
                t0 = time.monotonic()
                await loop.run_in_executor(None, self._do_inject, frame, use_no_ack)
            self._tx_frames += 1
            if not ack_gated:
                return True                 # fire-and-forget (deauth / WEP / current behaviour)
            if await self._await_ack(ta, t0, wait_for_ack):
                return True                 # landed — the AP ACKed it
        self._tx_unacked += 1
        return False                        # never ACKed after every send

    async def enable_ack_detect(self) -> None:
        """Arm the ACK tap. The Ralink monitor RX filter already admits the AP's ACK to
        any RA (RX_FILTER_CFG DROP_ACK + DROP_NOT_TO_ME clear), so this is a pure software
        flag — no register write. Not enter_active_monitor, which makes the chip emit ACKs."""
        self._ack_sightings.clear()
        self._ack_last_ts.clear()
        self._all_acks_seen = 0
        self._tx_frames = 0
        self._tx_unacked = 0
        self._ack_detect_on = True
        logger.info("RT3070 TX-ACK detection ON (monitor RX filter already admits ACKs) — "
                    "observing our TX delivery")

    async def disable_ack_detect(self) -> None:
        """Disarm the ACK tap (software flag; the monitor RX filter is left untouched)."""
        self._ack_detect_on = False

    def acks_seen(self, mac: bytes) -> int:
        """Count of ACKs observed addressed to ``mac`` (an injected source MAC) since enable."""
        return self._ack_sightings.get(bytes(mac).hex(), 0)

    async def _await_ack(self, ta: bytes, since: float, window: float) -> bool:
        """True if the tap observed an ACK to ``ta`` after ``since``, within ``window`` s.
        _dispatch runs on this loop, so a sleep yield lets a just-arrived ACK's timestamp
        land between checks."""
        deadline = since + window
        while time.monotonic() < deadline:
            if self._ack_last_ts.get(ta, 0.0) > since:
                return True
            await asyncio.sleep(0.001)
        return False

    def _do_inject(self, frame: bytes, use_no_ack: bool) -> None:
        # Executor thread; share _hw_lock with _tune so an inject can never collide with
        # an in-flight (or cancelled-but-draining) channel tune on the USB device.
        with self._hw_lock:
            tx.send_frame(self.transport.dev, self._bulk_out_ep, frame, use_no_ack=use_no_ack)

    def _stamp_seq(self, frame: bytes) -> bytes:
        """Stamp the next running sequence number into the frame's seqctl (bytes 22-23,
        ``seqnum << 4`` little-endian) — TXWI NSEQ=0 means the chip transmits the frame's
        own seqctl, so without this every inject shares seq=0 and a receiver's duplicate
        filter drops all but the first (the deauth then 'works' once and never again).
        aireplay-ng increments identically. Returns a copy; caller's bytes untouched."""
        if len(frame) < 24:
            return frame
        seq = self._tx_seq & 0xFFF
        self._tx_seq = (self._tx_seq + 1) & 0xFFF
        buf = bytearray(frame)
        buf[22:24] = ((seq << 4) & 0xFFFF).to_bytes(2, "little")
        return bytes(buf)

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Program ``mac`` as the self-MAC with UNICAST_TO_ME_MASK=0xff so the
        autoresponder HW-ACKs frames to it (rt3070's cold path never set a self-MAC).
        Reversed by exit_active_monitor."""
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, self._set_self_mac, bytes(mac), 0xFF)
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the monitor baseline: real EFUSE MAC + UNICAST_TO_ME_MASK=0
        (promiscuous capture, autoresponder matches nothing). The monitor RX filter is
        never touched, so capture survives the round trip."""
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
