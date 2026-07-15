"""RTL8187L driver — glues the bring-up chain onto the Driver Protocol.

Composition only: every step delegates to the layered modules in this
package (mac.py, rtl8225.py, chan.py, rx.py, tx.py, transport.py).

Bring-up flow (mirrors `rtl8187_probe` + `rtl8187_init_hw` + `rtl8187_start`
from data_dumps/rtl818x-source-v6.18/rtl8187/dev.c):

    connect()
      -> claim USB interface (cfg + claim)
      -> detect_chip_variant            (mac.py)        TX_CONF[27:25] HWVER probe
      -> is_chip_warm                   (mac.py)        CMD has TX_ENABLE|RX_ENABLE
      -> [warm]  resume bulk-IN polling
      -> [cold]  init_hw + rf.init + start              [M2]
      -> set_channel(1)                                 [M4]
      -> start RX loop                                  [M3]

Milestone status:
  * M1: control-transfer plumbing + chip-variant probe + warm probe.   [DONE]
  * M2a: init_hw + start (MAC side, rf.init stubbed).                  [DONE]
  * M2b: rtl8225 BCD RF init.                                          [DONE]
  * M2c: rtl8225z2 RF init (auto-dispatched by build_rf_init).         [DONE]
  * M3: rx descriptor decode + real RSSI + RX loop.                   [DONE]
  * M4: set_channel via rtl8225 set_chan + cached RfSetup.            [DONE]
  * M5: inject_frame + tx_hdr + bulk-OUT 0x02.                        [DONE]
  * M6 (current): handshake capture phase + ground-truth doc at
    chips/rtl8187/RTL8187L.md. Driver protocol surface complete.
"""
from __future__ import annotations

import asyncio
import errno
import logging
import time
from typing import Callable, Optional

import usb.core
import usb.util

from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError, BringUpPermissionsError

from wifit3.wlan.packet import WlanFrameParser

from .chan import config_channel as _config_channel
from .constants import REG_CMD, CMD_RX_ENABLE, CMD_TX_ENABLE, USB_PID_RTL8187, USB_VID_REALTEK
from .mac import (
    ChipVariant,
    cold_bring_up,
    is_chip_warm,
)
from .probe import probe
from .rtl8225 import RfSetup, TxPower, build_rf_init
from .rx import parse_rx_urb, probe_endpoints, read_rx_burst
from ..rx_reader import RxReaderThread
from .transport import RTL8187Transport
from .tx import inject_frame as _inject_frame

logger = logging.getLogger(__name__)


class RTL8187Driver(Driver):
    """Driver for the Realtek RTL8187L (e.g. ALFA AWUS036H).

    2.4 GHz only, hard-MAC chipset (no firmware blob). Bring-up is a
    pure-control-transfer sequence mirrored from the in-tree Linux
    driver — see module docstring for the milestone breakdown.
    """

    SUPPORTED_IDS = [
        DeviceID(USB_VID_REALTEK, USB_PID_RTL8187, "Realtek RTL8187L (ALFA AWUS036H)"),
    ]
    # 2.4 GHz channels 1..13. Channel 14 is JP-only and the chip supports
    # it (rtl818x_channels[13].center_freq=2484) but we leave it off the
    # default hop list to match the other 2.4 GHz drivers.
    SUPPORTED_CHANNELS = list(range(1, 14))
    # NONE: rtl8187 monitor is passive RX — no hardware ACK engine, so nothing to spoof.
    FAKE_MAC = FakeMacSupport.NONE

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RTL8187Driver":
        return cls(dev)

    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self.transport = RTL8187Transport(dev)
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._rx_reader: Optional[RxReaderThread] = None
        self._bulk_in_ep: Optional[int] = None
        self._claimed = False
        self._rf_setup: Optional[RfSetup] = None
        self._power: Optional[TxPower] = None
        self._rx_conf: int = 0
        # 802.11 TX sequence counter (bits [4:15], so it steps by 0x10). The 8187L has no
        # hardware seq assignment on the L-path, so we stamp it ourselves per injected
        # frame — see tx.stamp_seq_ctrl. Updated on the event loop only (no lock needed).
        self._tx_seqno: int = 0

        # Driver Protocol surface area.
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self.current_channel: int = 1
        self.chip_variant: Optional[ChipVariant] = None

        # Observe the AP's ACK to our injects (did our TX land). Off by default.
        self._ack_detect_on: bool = False
        self._our_tx_macs: set[bytes] = set()      # source MACs we inject as
        self._ack_sightings: dict[str, int] = {}   # our-MAC -> ACK count
        self._ack_last_ts: dict[bytes, float] = {}  # our-MAC -> ts of last ACK

    # ---- discovery hook ---------------------------------------------------
    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_callback = cb

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        """Sink for a terminal RX-reader failure (unplug). Forwarded to the RxReaderThread's
        on_fatal; resolved at call time so registration order vs connect() can't strand it."""
        self._on_lost = cb

    # ---- USB claim helpers -----------------------------------------------
    def _claim(self) -> None:
        if self._claimed:
            return
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
                logger.info("detached kernel driver from interface 0")
        except usb.core.USBError as e:
            if e.errno == errno.EACCES:
                raise BringUpPermissionsError("detach", str(e)) from e
            logger.debug("kernel-driver detach skipped: %s", e)
        except NotImplementedError:
            pass  # Windows
        try:
            self.dev.get_active_configuration()  # already configured?
        except usb.core.USBError as e:
            if e.errno == errno.EACCES:
                raise BringUpPermissionsError("open", str(e)) from e
            self.dev.set_configuration()
        try:
            usb.util.claim_interface(self.dev, 0)
        except usb.core.USBError as e:
            if e.errno == errno.EACCES:
                raise BringUpPermissionsError("claim", str(e)) from e
            raise
        self._claimed = True
        logger.info("claimed USB interface 0")

    def _release(self) -> None:
        if not self._claimed:
            return
        try:
            usb.util.release_interface(self.dev, 0)
            usb.util.dispose_resources(self.dev)
        except usb.core.USBError as e:
            logger.warning("USB release warning: %s", e)
        self._claimed = False

    # ---- connect ----------------------------------------------------------
    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """Run identification then the cold bring-up.

        M2a path: claim → identify → cold_bring_up. We don't yet open an
        RX polling loop here — the rx descriptor decoder lands in M3 and
        the RF synth that makes the receiver useful lands in M2b.
        """
        loop = asyncio.get_event_loop()

        def _progress(pct: float, msg: str) -> None:
            if progress_cb:
                progress_cb(pct, msg)
            logger.info("[%3d%%] %s", int(pct * 100), msg)

        try:
            _progress(0.05, "Claiming USB interface")
            await loop.run_in_executor(None, self._claim)

            # Probe: 93cx6 EEPROM (MAC + per-channel TX power + base), asic_rev,
            # HWVER, RF variant, rfkill — the exact rtl8187_probe wire sequence. Runs on
            # both warm + cold paths (the EEPROM TX-power table is what set_channel needs,
            # and the reads are safe on a warm/RF-alive chip).
            _progress(0.20, "Probing (EEPROM MAC + TX power, asic_rev, HWVER, RF)")
            pr = await loop.run_in_executor(None, probe, self.transport)
            self.chip_variant = pr.chip
            self._rf_setup = pr.setup
            self._power = pr.power
            self.mac_address = ":".join(f"{b:02x}" for b in pr.mac)
            logger.info(
                "probe: mac=%s, chip=%s, asic_rev=%d, rf=%s",
                self.mac_address, pr.chip.name, pr.setup.asic_rev, pr.setup.variant.value,
            )
            if self.chip_variant.is_8187b_masquerade:
                logger.error(
                    "RTL8187B in 0x8187 disguise — this driver is 8187L only. "
                    "Bring-up aborted."
                )
                return False

            _progress(0.35, "Probing endpoints + warm/cold state")
            eps = probe_endpoints(self.dev)
            self._bulk_in_ep = eps.primary_bulk_in
            warm = await loop.run_in_executor(None, is_chip_warm, self.transport)

            # ALWAYS run the full cold bring-up — no warm shortcut on this chip. The
            # 8187L's RF/PHY/AGC state does NOT survive a USB handle close+reopen: the
            # next set_configuration soft-resets the radio, yet the CMD TX/RX-enable
            # bits persist, so is_chip_warm() reports "warm" while the AGC is dead
            # (agc=0 in every RX descriptor → RSSI stuck at -4, ~3x fewer frames, weak
            # handshake/EAPOL capture). Re-arming with start() alone (no RF init) leaves
            # it broken. Measured 2026-06-12: cold 215 frames/s (RSSI -71..-14) vs warm
            # 31-67 frames/s (RSSI all -4). The ~2 s cold init is the price of correct
            # RX — supersedes the earlier warm-reattach optimisation, whose beacons/s
            # check was ceiling-capped and missed the degradation.
            logger.info("is_warm (CMD bits)=%s — re-initialising RF anyway "
                        "(warm RF state is unreliable across reopen on the 8187L)", warm)
            _progress(0.50, "Building RF init callback")
            rf_init = build_rf_init(self.transport, self._rf_setup, self._power)
            _progress(0.55, "Cold bring-up (init_hw + RF + start + monitor entry)")
            self._rx_conf = await loop.run_in_executor(
                None, cold_bring_up, self.transport, rf_init
            )

            # Verify CMD latched TX_ENABLE | RX_ENABLE (warm re-arm or cold start).
            cmd = await loop.run_in_executor(None, self.transport.read8, REG_CMD)
            if not (cmd & CMD_TX_ENABLE and cmd & CMD_RX_ENABLE):
                logger.error(
                    "bring-up finished but CMD=0x%02x missing TX/RX enable bits", cmd
                )
                return False
            logger.info("CMD=0x%02x — TX_ENABLE + RX_ENABLE latched", cmd)

            _progress(0.85, "Starting RX loop")
            self._rx_reader = RxReaderThread(
                loop, self._rx_read_once, self._rx_dispatch, name="rtl8187-rx",
                on_fatal=lambda e: self._on_lost and self._on_lost(e)
            )
            self._rx_reader.start()

            self.is_warm = True  # subsequent connect()s will see us as warm
            _progress(1.00, "RTL8187L online — RX loop polling bulk-IN")
            return True

        except (IOError, usb.core.USBError, NotImplementedError) as e:
            raise BringUpError("bring-up", str(e)) from e

    # ---- RX loop ----------------------------------------------------------
    # ---- RX callables for the shared RxReaderThread ---------------------
    # read_once runs on the reader thread; dispatch runs on the event loop.
    # One URB = one frame on 8187L (no coalescing), so dispatch is one-shot.

    def _rx_read_once(self) -> Optional[bytes]:
        """One blocking bulk-IN read; None on a benign timeout."""
        return read_rx_burst(self.dev, self._bulk_in_ep)

    def _rx_dispatch(self, buf: bytes) -> None:
        """Decode one RX URB → parse → rx callback (on the loop)."""
        rx = parse_rx_urb(buf)
        if rx is None or rx.has_fcs_error:
            return
        mpdu = rx.mpdu
        # A 10-byte 0xD4 frame is an ACK (the parser drops control frames). mpdu[4:10]
        # is the RA the AP ACKed; keep only ACKs to a MAC we inject as.
        if self._ack_detect_on and len(mpdu) == 10 and mpdu[0] == 0xD4:
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
        """Arm the ACK tap. Pure software flag — the monitor RX_CONF already sets
        RX_CONF_CTRL (mac.configure_filter, = FIF_CONTROL), so the hardware forwards ACK
        control frames to bulk-IN; no register write needed. The 8187L has no auto-ACK
        engine (FAKE_MAC.NONE), so this is RX-side only — it never emits an ACK."""
        self._ack_sightings.clear()
        self._ack_last_ts.clear()
        self._ack_detect_on = True
        logger.info("RTL8187 TX-ACK detection ON — observing our TX delivery")

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

    # ---- channel tune (M4) -----------------------------------------------
    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        if self._rf_setup is None or self._power is None:
            logger.error("RTL8187 set_channel(%d): connect() must run first", channel)
            return False
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, _config_channel,
                self.transport, self._rf_setup.asic_rev,
                self._rf_setup.variant, channel, self._power,
            )
        except ValueError as e:
            logger.warning("RTL8187 set_channel: %s", e)
            return False
        except (IOError, usb.core.USBError) as e:
            logger.error("RTL8187 set_channel(%d) USB error: %s", channel, e)
            return False
        self.current_channel = channel
        return True

    # ---- TX inject (M5) --------------------------------------------------
    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True,
                           wait_for_ack: float = 0.0, max_resends: int = 0) -> bool:
        """Build tx_hdr + bulk-OUT inject.

        ``use_no_ack=True`` is the "fire-and-forget" mode used for
        spoofed frames (deauths, EAPOL inject) — the chip is told to
        send the frame *once* (``retry_count=1``) instead of retrying
        7× waiting for an ACK from a sender we're impersonating. Real
        retries would let the TX FIFO back up past the bulk-OUT
        timeout. ``use_no_ack=False`` uses ``RETRY_COUNT=7`` for
        normal unicast TX (where we actually want delivery).

        ``wait_for_ack > 0`` (with TX-ACK detection armed) waits for the AP's ACK and
        resends the same frame up to ``max_resends`` times if none comes, returning whether
        it landed; ``0`` = fire-and-forget (byte-identical to the prior behaviour).
        """
        from .tx import RETRY_COUNT, stamp_seq_ctrl
        retry_count = 1 if use_no_ack else RETRY_COUNT
        ta = bytes(frame_bytes[10:16]) if len(frame_bytes) >= 16 else None   # AP ACKs back to TA
        if self._ack_detect_on and ta is not None:
            self._our_tx_macs.add(ta)
        # Stamp an incrementing 802.11 sequence number ONCE (the 8187L L-path has no hardware
        # seq assignment; the AP dedups our association/EAPOL frames otherwise). Resends reuse
        # the same seq so the AP treats them as retransmissions. Done on the loop before the
        # blocking write, so _tx_seqno needs no lock.
        buf = bytearray(frame_bytes)
        self._tx_seqno = stamp_seq_ctrl(buf, self._tx_seqno)
        frame_to_send = bytes(buf)
        loop = asyncio.get_event_loop()
        ack_gated = wait_for_ack > 0 and self._ack_detect_on and ta is not None
        for _ in range(max_resends + 1):
            try:
                t0 = time.monotonic()
                await loop.run_in_executor(
                    None,
                    lambda: _inject_frame(self.dev, frame_to_send, retry_count=retry_count),
                )
            except usb.core.USBError as e:
                logger.error("RTL8187 inject_frame USBError: %s", e)
                return False
            except ValueError as e:
                logger.warning("RTL8187 inject_frame bad frame: %s", e)
                return False
            if not ack_gated:
                return True                 # fire-and-forget (deauth / EAPOL / current behaviour)
            if await self._await_ack(ta, t0, wait_for_ack):
                return True                 # landed — the AP ACKed it
        return False                        # never ACKed after every send

    async def close(self) -> None:
        if self._rx_reader is not None:
            await self._rx_reader.stop()
            self._rx_reader = None
        self._release()
        logger.info("RTL8187 driver closed")
