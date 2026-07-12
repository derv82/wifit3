"""rt2500usb driver — Ralink RT2570 (Buffalo "Nintendo Wi-Fi USB Connector"
and the rest of the rt2500usb device table).

Userland PyUSB port of the Linux ``rt2500usb`` kernel module. The RT2570
is the older Ralink USB generation: 16-bit CSRs, **no firmware**, BBP/RF
reached indirectly through PHY_CSR busy-poll registers. See RT2500USB.md
for the per-chip ground-truth doc.

Bring-up flow (mirrors rt2500usb_enable_radio, with the rt2x00 + rt2x00usb
framework layers flattened into wifit3's per-chip module shape):

    connect()
      ├─ claim USB interface + probe bulk endpoints (EP 0x81 IN / 0x01 OUT)
      ├─ read EEPROM           → MAC, RF type, antenna, RSSI offset      [M1]
      ├─ read_revision         MAC_CSR0 → version branch                 [M2a]
      ├─ is_chip_warm          MAC_CSR1.HOST_READY + bulk-IN smoke test   [M4]
      ├─ cold bring-up (if not warm)
      │   ├─ init_registers    + set_state(AWAKE)                        [M2a]
      │   └─ init_bbp          (PHY_CSR7/8 indirect)                     [M2b]
      ├─ config_ant            RF2525E TX I/Q flip + antenna             [M3]
      ├─ apply_monitor_filter  TXRX_CSR2 accept-real / drop-noise        [M3]
      ├─ set_channel(default)  RF2525E config_channel                    [M2c]
      └─ _rx_loop              EP 0x81 → RXD-at-end decode → parser      [M3]

Milestone status: M1-M3 hw-verified; M4 (this driver) wires them into the
WlanDriver Protocol + manager registration. M5 = inject_frame (TX).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable, Optional

import usb.core
import usb.util

from wifit3.engine.protocols import DeviceID, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.wlan.packet import WlanFrameParser

from . import monitor
from .bbp import init_bbp
from .chan import VERIFIED_RF, antenna_defaults, is_rf_ported
from .constants import (
    DEFAULT_RSSI_OFFSET,
    EEPROM_ANTENNA,
    EEPROM_ANTENNA_RF_TYPE,
    EEPROM_CALIBRATE_OFFSET,
    EEPROM_CALIBRATE_OFFSET_RSSI,
    EEPROM_MAC_ADDR_0,
    RF_NAMES,
    RT2500USB_DEVICE_TABLE,
)
from .mac import (
    config_filter,
    init_registers,
    is_chip_warm,
    read_revision,
    start_queue_rx,
)
from .rx import parse_rx_urb, probe_endpoints, read_rx_burst
from ..rx_reader import RxReaderThread
from .transport import RT2500USBTransport, get_field16
from .tx import inject as _tx_inject

logger = logging.getLogger(__name__)


class RT2500USBDriver:
    """Driver for the Ralink RT2570 (rt2500usb family)."""

    SUPPORTED_IDS = [
        DeviceID(vid, pid, desc) for (vid, pid, desc) in RT2500USB_DEVICE_TABLE
    ]
    # 2.4 GHz channels 1-14 — the band every RT2500 RF chip tunes (RF5222 also
    # has 5 GHz rows, but this driver is 2.4 GHz-only; see chan.RF_VALS_5222).
    SUPPORTED_CHANNELS = list(range(1, 15))
    # NONE: rt2500usb has no hardware autoresponder — it can't ACK any MAC.
    FAKE_MAC = FakeMacSupport.NONE

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RT2500USBDriver":
        return cls(dev)

    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self.transport = RT2500USBTransport(dev)
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._rx_reader: Optional[RxReaderThread] = None
        self._bulk_in_ep: Optional[int] = None
        self._bulk_out_ep: Optional[int] = None
        self._claimed = False

        # EEPROM-derived, set at connect() time.
        self._eeprom: bytes = b""        # raw one-shot read; reset_tuner re-reads it per hop
        self.rf_type: int = 0
        self._ant_tx: int = 0
        self._ant_rx: int = 0
        self._rssi_offset: int = DEFAULT_RSSI_OFFSET

        # WlanDriver Protocol surface area.
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self.current_channel: int = 1

        # Device-op serialization. _io_lock serializes the COROUTINES (a hop vs
        # an inject). _hw_lock is the REAL hardware serializer held by the
        # executor work: a coroutine cancelled mid-run_in_executor (UI view
        # switch → hop-task cancel) releases _io_lock while its executor THREAD
        # keeps issuing control transfers, and a new tune's thread would then
        # collide on the control endpoint and wedge this full-speed chip. The
        # threading.Lock blocks that second thread until the first (even a
        # cancelled one) finishes. [[project_rt3070 RX-DMA wedge pattern]]
        self._io_lock = asyncio.Lock()
        self._hw_lock = threading.Lock()

        # Observe the AP's ACK to our injects (did our TX land). Off by default.
        self._ack_detect_on: bool = False
        self._our_tx_macs: set[bytes] = set()      # source MACs we inject as
        self._ack_sightings: dict[str, int] = {}   # our-MAC -> ACK count
        self._all_acks_seen: int = 0
        self._ack_last_ts: dict[bytes, float] = {}  # our-MAC -> ts of last ACK
        self._tx_frames: int = 0
        self._tx_unacked: int = 0

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

    # ---- EEPROM ---------------------------------------------------------
    def _parse_eeprom(self, eeprom: bytes) -> None:
        """Pull MAC, RF type, antenna defaults, and the RSSI offset from a
        one-shot EEPROM read (rt2500usb_init_eeprom / validate_eeprom)."""
        self._eeprom = eeprom
        mac_off = EEPROM_MAC_ADDR_0 * 2
        mac = eeprom[mac_off:mac_off + 6]
        self.mac_address = ":".join(f"{b:02x}" for b in mac)

        ant_off = EEPROM_ANTENNA * 2
        antenna = eeprom[ant_off] | (eeprom[ant_off + 1] << 8)
        self.rf_type = get_field16(antenna, EEPROM_ANTENNA_RF_TYPE)
        self._ant_tx, self._ant_rx = antenna_defaults(antenna)
        self._log_rf_variant()

        cal_off = EEPROM_CALIBRATE_OFFSET * 2
        cal = eeprom[cal_off] | (eeprom[cal_off + 1] << 8)
        self._rssi_offset = (
            DEFAULT_RSSI_OFFSET if cal == 0xFFFF
            else get_field16(cal, EEPROM_CALIBRATE_OFFSET_RSSI)
        )

    def _log_rf_variant(self) -> None:
        """Classify the EEPROM RF chip once at connect. RF2525E is the
        hardware-verified reference; the other four kernel-known chips tune
        from ported-but-unverified rf_vals tables; anything else falls back to
        RF2525 (chan.config_channel). No hard failure on any of them."""
        name = RF_NAMES.get(self.rf_type, f"0x{self.rf_type:x}")
        if self.rf_type == VERIFIED_RF:
            return
        if is_rf_ported(self.rf_type):
            logger.info("RF chip %s: kernel rf_vals table ported, not yet "
                        "hardware-verified on this port (reference is RF2525E)", name)
        else:
            logger.warning("RF chip %s is not one of the six RT2500 RF chips — "
                           "untested; channel tuning will use the RF2525 fallback", name)

    # ---- warm probe -----------------------------------------------------
    async def _smoke_test_rx(self, loop) -> bool:
        """A warm chip should already be streaming RX. Probe the bulk-IN
        pipe: a timeout (no data this instant) or bytes both mean "pipe
        alive"; only a pipe-stall USBError means wedged."""
        try:
            for _ in range(3):
                await loop.run_in_executor(
                    None, read_rx_burst, self.dev, self._bulk_in_ep
                )
            return True
        except usb.core.USBError as e:
            logger.warning("warm bulk-IN smoke test failed: %s", e)
            return False

    # ---- connect --------------------------------------------------------
    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        loop = asyncio.get_event_loop()

        def prog(p: float, msg: str) -> None:
            logger.info("connect[%3d%%] %s", int(p * 100), msg)
            if progress_cb:
                progress_cb(p, msg)

        try:
            await loop.run_in_executor(None, self._claim)
            eps = await loop.run_in_executor(None, probe_endpoints, self.dev)
            self._bulk_in_ep = eps.primary_bulk_in
            self._bulk_out_ep = eps.bulk_out[0] if eps.bulk_out else None
            prog(0.1, "interface claimed")

            eeprom = await loop.run_in_executor(None, self.transport.read_eeprom)
            self._parse_eeprom(eeprom)
            prog(0.25, f"EEPROM: MAC {self.mac_address}, RF 0x{self.rf_type:x}, "
                       f"rssi_offset {self._rssi_offset}")

            revision = await loop.run_in_executor(None, read_revision, self.transport)
            warm = await loop.run_in_executor(None, is_chip_warm, self.transport)

            if warm and await self._smoke_test_rx(loop):
                # Already inited + monitoring from a prior session. Re-arm the
                # monitor filter + RX queue and re-seed the AGC via a tune; skip
                # FW-less cold init. [[feedback_warm_reattach]]
                prog(0.55, "chip WARM — re-arming monitor + tune")
                await loop.run_in_executor(
                    None, config_filter, self.transport, True
                )
                await loop.run_in_executor(None, start_queue_rx, self.transport)
            elif warm:
                # HOST_READY set but bulk-IN wedged. On Windows+WinUSB this
                # pipe often can't be recovered in userland — ask for a replug
                # rather than thrash. [[feedback_warm_reattach]]
                prog(0.5, "chip warm but bulk-IN wedged")
                raise BringUpError(
                    "warm reattach",
                    "bulk-IN pipe is wedged — please unplug the device, wait ~5s, replug, "
                    "and reconnect.",
                )
            else:
                prog(0.35, "cold bring-up: init_registers + set_state(AWAKE)")
                await loop.run_in_executor(
                    None, init_registers, self.transport, revision
                )
                prog(0.5, "init_bbp")
                await loop.run_in_executor(None, init_bbp, self.transport, eeprom)
                prog(0.7, "enable_monitor (LED, filter, antenna, AGC seed)")
                await loop.run_in_executor(
                    None, monitor.enable_monitor, self.transport, self.rf_type,
                    eeprom, self._ant_tx, self._ant_rx,
                )

            # Tune to the start channel (rt2x00mac_config CHANGE_CHANNEL): tunes
            # the synth and re-seeds the AGC (reset_tuner), warm or cold.
            await self.set_channel(self.current_channel)
            prog(0.9, f"tuned to channel {self.current_channel}")

            self._rx_reader = RxReaderThread(
                loop, self._rx_read_once, self._rx_dispatch, name="rt2500usb-rx",
                on_fatal=lambda e: self._on_lost and self._on_lost(e))
            self._rx_reader.start()
            self.is_warm = True
            prog(1.0, "connected")
            return True
        except BringUpError:
            raise
        except Exception as e:
            raise BringUpError("init", str(e)) from e

    # ---- RX loop --------------------------------------------------------
    # ---- RX callables for the shared RxReaderThread ---------------------
    # read_once runs on the reader thread; dispatch runs on the event loop.

    def _rx_read_once(self) -> Optional[bytes]:
        """One blocking bulk-IN read; None on a benign timeout."""
        return read_rx_burst(self.dev, self._bulk_in_ep)

    def _rx_dispatch(self, buf: bytes) -> None:
        """Decode one RX URB → parse → rx callback (on the loop)."""
        rx = parse_rx_urb(buf, rssi_offset=self._rssi_offset)
        if rx is None or rx.has_fcs_error:
            return
        mpdu = rx.mpdu
        # A 10-byte 0xD4 frame is an ACK (the parser drops control frames). mpdu[4:10]
        # is the RA the AP ACKed; keep only ACKs to a MAC we inject as.
        if self._ack_detect_on and len(mpdu) == 10 and mpdu[0] == 0xD4:
            self._all_acks_seen += 1
            ra = mpdu[4:10]
            if ra in self._our_tx_macs:
                self._ack_sightings[ra.hex()] = self._ack_sightings.get(ra.hex(), 0) + 1
                self._ack_last_ts[ra] = time.monotonic()   # for inject wait-for-ack
            return
        parsed = WlanFrameParser.parse_80211_frame(mpdu, rx.rssi_dbm)
        if parsed is not None and self._rx_callback is not None:
            try:
                self._rx_callback(parsed)
            except Exception as e:
                logger.exception("rx_callback raised: %s", e)

    # ---- TX-ACK detection -----------------------------------------------
    async def enable_ack_detect(self) -> None:
        """Arm the ACK tap. Pure software flag — the monitor RX filter already clears
        TXRX_CSR2_DROP_CONTROL (mac.config_filter, = FIF_CONTROL) and DROP_NOT_TO_ME, so
        the hardware forwards ACK control frames (incl. to a forged TA) to bulk-IN; no
        register write needed. The RT2570 has no auto-ACK engine (FAKE_MAC.NONE), so this
        is RX-side only — it never emits an ACK."""
        self._ack_sightings.clear()
        self._ack_last_ts.clear()
        self._all_acks_seen = 0
        self._tx_frames = 0
        self._tx_unacked = 0
        self._ack_detect_on = True
        logger.info("rt2500usb TX-ACK detection ON — observing our TX delivery")

    async def disable_ack_detect(self) -> None:
        """Disarm the ACK tap (software flag only; the monitor RX filter is unchanged)."""
        self._ack_detect_on = False

    def acks_seen(self, mac: bytes) -> int:
        """Count of ACKs observed addressed to ``mac`` (an injected source MAC) since enable."""
        return self._ack_sightings.get(bytes(mac).hex(), 0)

    async def _await_ack(self, ta: bytes, since: float, window: float) -> bool:
        """True if the tap observed an ACK to ``ta`` after ``since``, within ``window`` s.
        _rx_dispatch runs on this loop, so a sleep yield lets a just-arrived ACK's timestamp
        land between checks."""
        deadline = since + window
        while time.monotonic() < deadline:
            if self._ack_last_ts.get(ta, 0.0) > since:
                return True
            await asyncio.sleep(0.001)
        return False

    # ---- channel tune ---------------------------------------------------
    def _tune(self, channel: int) -> bool:
        # Executor thread; _hw_lock guarantees only one hardware op touches the
        # control endpoint at a time, even if a cancelled tune's thread is still
        # draining its tune_hop transfers.
        with self._hw_lock:
            return monitor.tune_hop(
                self.transport, self.rf_type, channel,
                self._eeprom, self._ant_tx, self._ant_rx,
            )

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        if channel not in self.SUPPORTED_CHANNELS:
            logger.warning("rt2500usb: channel %d not supported", channel)
            return False
        try:
            async with self._io_lock:
                ok = await asyncio.get_event_loop().run_in_executor(
                    None, self._tune, channel
                )
        except Exception as e:
            logger.error("rt2500usb set_channel(%d) failed: %s", channel, e)
            return False
        if ok:
            self.current_channel = channel
        return ok

    # ---- TX -------------------------------------------------------------
    def _do_inject(self, frame_bytes: bytes, ack: bool) -> int:
        # Shares _hw_lock with _tune so an inject can never collide with an
        # in-flight (or cancelled-but-draining) channel tune on the device.
        with self._hw_lock:
            return _tx_inject(self.dev, self._bulk_out_ep, frame_bytes, ack)

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True,
                           wait_for_ack: float = 0.0, max_resends: int = 0) -> bool:
        """Inject a raw 802.11 frame (no FCS) at 1 Mbps CCK. Only ever
        called behind an explicit user action [[passive_by_default]].

        ``wait_for_ack > 0`` (with TX-ACK detection armed) waits for the AP's ACK and
        resends the same frame up to ``max_resends`` times if none comes, returning whether
        it landed; ``0`` = fire-and-forget (byte-identical to the prior behaviour)."""
        if self._bulk_out_ep is None:
            logger.error("rt2500usb: no bulk-OUT endpoint; cannot inject")
            return False
        ta = bytes(frame_bytes[10:16]) if len(frame_bytes) >= 16 else None   # AP ACKs back to TA
        if self._ack_detect_on and ta is not None:
            self._our_tx_macs.add(ta)
        loop = asyncio.get_event_loop()
        ack_gated = wait_for_ack > 0 and self._ack_detect_on and ta is not None
        for _ in range(max_resends + 1):
            try:
                async with self._io_lock:
                    t0 = time.monotonic()
                    sent = await loop.run_in_executor(
                        None, self._do_inject, frame_bytes, not use_no_ack
                    )
            except Exception as e:
                logger.error("rt2500usb inject_frame failed: %s", e)
                return False
            self._tx_frames += 1
            if not ack_gated:
                return sent > 0             # fire-and-forget (deauth / current behaviour)
            if sent > 0 and await self._await_ack(ta, t0, wait_for_ack):
                return True                 # landed — the AP ACKed it
        self._tx_unacked += 1
        return False                        # never ACKed after every send

    # ---- teardown -------------------------------------------------------
    async def close(self) -> None:
        if self._rx_reader is not None:
            await self._rx_reader.stop()
            self._rx_reader = None
        self._release()
        logger.info("rt2500usb driver closed")
