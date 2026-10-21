"""RTL8187L / RTL8187B driver — glues the bring-up chain onto the Driver Protocol.

Composition only: every step delegates to the layered modules in this
package (mac.py, rtl8225.py, chan.py, rx.py, tx.py, transport.py).

Bring-up flow (mirrors `rtl8187_probe` + `rtl8187_init_hw` + `rtl8187_start`
from driver_sources/rtl818x-source-v6.18/rtl8187/dev.c):

    connect()
      -> claim USB interface (cfg + claim)
      -> detect_chip_variant            (mac.py)        TX_CONF[27:25] HWVER probe
      -> init_hw + rf.init + start                      [M2]
      -> set_channel(1)                                 [M4]
      -> start RX loop                                  [M3]

Milestone status:
  * M1: control-transfer plumbing + chip-variant probe.   [DONE]
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
from typing import Callable, Optional

import usb.core
import usb.util

from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError, BringUpPermissionsError

from wifit3.dot11.parser import WlanFrameParser

from .chan import config_channel as _config_channel
from .constants import (
    DEVICE_RTL8187B,
    USB_EP_BULK_IN,
    USB_EP_BULK_IN_8187B,
    USB_EP_BULK_OUT,
    USB_EP_TX_STATUS_8187B,
)
from .mac import (
    ChipVariant,
    cold_bring_up,
    stop,
)
from .probe import probe
from .rtl8225 import RfSetup, TxPower, build_rf_init, rtl8225_rf_stop
from .rx import parse_rx_urb, read_rx_burst
from ..rx_reader import RxReaderThread
from .transport import RTL8187Transport
from .tx import (
    RTL8187B_EP_MGMT,
    bulk_out_max_packet_size,
    epmap,
    inject_frame as _tx_inject,
    rtl8187b_status_cb,
    stamp_seq_ctrl,
)

logger = logging.getLogger(__name__)

RTL8187B_BULK_OUT_EPS = (RTL8187B_EP_MGMT, *epmap)


class RTL8187Driver(Driver):
    """Driver for the Realtek RTL8187L (e.g. ALFA AWUS036H) and RTL8187B.

    2.4 GHz only, hard-MAC chipset (no firmware blob). Bring-up is a
    pure-control-transfer sequence mirrored from the in-tree Linux
    driver — see module docstring for the milestone breakdown.
    """

    # rtl818x_channels: 2.4 GHz 1..14 (ch14 = 2484 MHz, its own CCK power table).
    SUPPORTED_CHANNELS = list(range(1, 15))
    # FIXED_MAC: the 8187L auto-ACKs its own silicon MAC in monitor mode (bench 2026-07-16:
    # 111 ACKs / 100 injects to the silicon MAC), but has no active monitor to program a
    # forged MAC, so a spoofed source is never ACKed.
    FAKE_MAC = FakeMacSupport.FIXED_MAC

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RTL8187Driver":
        return cls(dev, is_rtl8187b_id=id_entry.chipset == DEVICE_RTL8187B)

    def __init__(self, dev: usb.core.Device, is_rtl8187b_id: bool = False):
        super().__init__()          # base owns the ACK tally (_ack_detect_on / _our_tx_macs / _ack_counts)
        self.dev = dev
        # id->driver_info == DEVICE_RTL8187B (dev.c:1452); probe can still find B silicon on an L ID.
        self.is_rtl8187b_id = is_rtl8187b_id
        self.transport = RTL8187Transport(dev)
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._rx_reader: Optional[RxReaderThread] = None
        self._tx_status_reader: Optional[RxReaderThread] = None
        self._bulk_in_ep: Optional[int] = None
        self._bulk_out_max_packet_sizes: Optional[dict[int, int]] = None
        self._claimed = False
        self._rf_setup: Optional[RfSetup] = None
        self._power: Optional[TxPower] = None
        self._rx_conf: int = 0
        self._started = False
        # 802.11 TX sequence counter (bits [4:15], so it steps by 0x10). The 8187L has no
        # hardware seq assignment on the L-path, so we stamp it ourselves per injected
        # frame — see tx.stamp_seq_ctrl. Updated on the event loop only (no lock needed).
        self._tx_seqno: int = 0

        # Driver Protocol surface area.
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self.current_channel: Optional[int] = None
        self.chip_variant: Optional[ChipVariant] = None

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
        logger.debug("claimed USB interface 0")

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
            # HWVER, RF variant, rfkill — the exact rtl8187_probe wire sequence.
            _progress(0.20, "Probing (EEPROM MAC + TX power, asic_rev, HWVER, RF)")
            pr = await loop.run_in_executor(
                None, probe, self.transport, self.dev.idProduct, self.is_rtl8187b_id)
            self.chip_variant = pr.chip
            self._rf_setup = pr.setup
            self._power = pr.power
            self.mac_address = ":".join(f"{b:02x}" for b in pr.mac)
            logger.debug(
                "probe: mac=%s, chip=%s, asic_rev=%d, rf=%s",
                self.mac_address, pr.chip.name, pr.setup.asic_rev, pr.setup.variant.value,
            )
            if not pr.rfkill_off:
                raise BringUpError("rfkill", "hardware radio switch reads OFF")
            if pr.chip.is_rtl8187b and pr.chip.hw_rev is not None:
                logger.warning("RTL8187B (%s): ported from the kernel source, untested on hardware",
                               pr.chip.hw_rev.name)

            _progress(0.35, "Reading USB endpoints")
            self._bulk_in_ep = (USB_EP_BULK_IN_8187B if self.chip_variant.is_rtl8187b
                                else USB_EP_BULK_IN)
            out_eps = (RTL8187B_BULK_OUT_EPS if self.chip_variant.is_rtl8187b
                       else (USB_EP_BULK_OUT,))
            self._bulk_out_max_packet_sizes = {
                ep: bulk_out_max_packet_size(self.dev, ep) for ep in out_eps}

            # Always cold: the RF/AGC does not survive a USB reopen (RTL8187L.md, "No warm reattach").
            rf_init = build_rf_init(self.transport, self._rf_setup, self._power)
            rf_name = self._rf_setup.variant.value

            def _emit(pct: float, msg: str) -> None:
                loop.call_soon_threadsafe(_progress, pct, msg)

            def _rf_init_with_progress(t: RTL8187Transport) -> None:
                _emit(0.55, f"RF init ({rf_name})")
                rf_init(t)
                _emit(0.75, "Starting MAC + entering monitor mode")

            _progress(0.40, "Initialising MAC (init_hw)")
            self._rx_conf = await loop.run_in_executor(
                None, cold_bring_up, self.transport, _rf_init_with_progress,
                self.chip_variant.is_rtl8187b,
            )
            self._started = True
            # mac80211's first rtl8187_config (dev.c:1155) tunes the band's first channel.
            _progress(0.80, "Tuning channel 1")
            await loop.run_in_executor(
                None, _config_channel, self.transport, self._rf_setup, 1, self._power,
            )
            self.current_channel = 1

            _progress(0.85, "Starting RX loop")
            self._rx_reader = RxReaderThread(
                loop, self._rx_read_once, self._rx_dispatch, name="rtl8187-rx",
                on_fatal=lambda e: self._on_lost and self._on_lost(e)
            )
            self._rx_reader.start()
            if self.chip_variant.is_rtl8187b:            # rtl8187b_init_status_urb (dev.c:967)
                self._tx_status_reader = RxReaderThread(
                    loop, self._tx_status_read_once, self._tx_status_dispatch,
                    name="rtl8187b-tx-status",
                    on_fatal=lambda e: self._on_lost and self._on_lost(e)
                )
                self._tx_status_reader.start()

            self.is_warm = True  # subsequent connect()s will see us as warm
            _progress(1.00, f"{pr.chip.name} online — RX loop polling bulk-IN")
            return True

        except (IOError, usb.core.USBError, NotImplementedError) as e:
            raise BringUpError("bring-up", str(e)) from e

    # ---- RX loop ----------------------------------------------------------
    # ---- RX callables for the shared RxReaderThread ---------------------
    # read_once runs on the reader thread; dispatch runs on the event loop.
    # One URB = one frame (no coalescing), so dispatch is one-shot.

    def _rx_read_once(self) -> Optional[bytes]:
        """One blocking bulk-IN read; None on a benign timeout."""
        return read_rx_burst(self.dev, self._bulk_in_ep)

    def _rx_dispatch(self, buf: bytes) -> None:
        """Decode one RX URB → parse → rx callback (on the loop)."""
        rx = parse_rx_urb(buf, self.chip_variant is not None and self.chip_variant.is_rtl8187b)
        if rx is None or rx.has_fcs_error:
            return
        mpdu = rx.mpdu
        # A 10-byte 0xD4 frame is an ACK (the parser drops control frames); the base tallies it
        # iff the ACK tap is armed and RA=mpdu[4:10] is a MAC we inject as.
        if len(mpdu) == 10 and mpdu[0] == 0xD4:
            self.record_ack(mpdu)
            return
        parsed = WlanFrameParser.parse_80211_frame(mpdu, rx.rssi_dbm)
        if parsed is not None and self._rx_callback is not None:
            try:
                self._rx_callback(parsed)
            except Exception as e:
                logger.exception("rx_callback raised: %s", e)

    def _tx_status_read_once(self) -> Optional[bytes]:
        return read_rx_burst(self.dev, USB_EP_TX_STATUS_8187B, max_size=8)

    def _tx_status_dispatch(self, buf: bytes) -> None:
        status = rtl8187b_status_cb(buf)
        if status is not None:
            logger.debug("RTL8187B tx close: seq_no=0x%03x tok=%s pkt_rc=%d",
                         status.seq_no, status.tok, status.pkt_rc)

    # ---- RX-ACK detection -----------------------------------------------
    async def _enable_rx_acks(self) -> None:
        """No-op: monitor entry sets RX_CONF_CTRL (mac.configure_filter, = FIF_CONTROL), so the
        hardware already forwards ACK control frames (FC=0xD4) to bulk-IN. Nothing to enable on the
        chip (the base arms the tally). This does not touch the card's own auto-ACK responder, which
        HW-ACKs the card's silicon MAC regardless (FAKE_MAC.FIXED_MAC)."""
        return

    async def _disable_rx_acks(self) -> None:
        """No-op, matching ``_enable_rx_acks``: the monitor RX filter is left untouched."""
        return

    # ---- Active monitor (FIXED_MAC: HW-ACKs only the card's own silicon MAC) ----
    async def enter_active_monitor(self, mac: bytes,
                                   bssid: Optional[bytes] = None) -> bytes:
        """FIXED_MAC: the RTL8187 auto-ACK responder answers only frames addressed to the card's
        own silicon MAC; there is no register to aim it at a forged one. So this is a no-op that
        reports the silicon MAC, and the caller associates/injects as it, so the chip's HW ACKs are
        honored (no retransmit storm). ``mac``/``bssid`` are ignored. Falls back to the requested
        MAC if the silicon MAC has not been read yet (connect() populates it)."""
        if not self.mac_address:
            return bytes(mac)
        return bytes(int(x, 16) for x in self.mac_address.split(":"))

    async def exit_active_monitor(self) -> None:
        """No-op: enter_active_monitor programmed nothing (the silicon-MAC auto-ACK is always on)."""
        return

    # ---- channel tune (M4) -----------------------------------------------
    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        if self._rf_setup is None or self._power is None:
            logger.error("RTL8187 set_channel(%d): RF setup / EEPROM TX power not probed yet "
                         "(read in connect()); connect() must run first", channel)
            return False
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, _config_channel, self.transport, self._rf_setup, channel, self._power,
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
    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Build tx_hdr + bulk-OUT ``frame_bytes`` once, over EP 0x02, with the chip's HW ACK-retry
        limit set to ``RETRY_COUNT`` (7). The 802.11 sequence number is already stamped
        by ``_stamp_tx_seq``."""
        max_packet_sizes = self._bulk_out_max_packet_sizes
        if max_packet_sizes is None or self.chip_variant is None:
            logger.error("RTL8187 inject_frame: bulk-OUT wMaxPacketSize / chip variant not set yet "
                         "(read in connect()); connect() must run first")
            return False
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: _tx_inject(self.dev, frame_bytes, max_packet_sizes,
                                   self.chip_variant.is_rtl8187b),
            )
        except usb.core.USBError as e:
            logger.error("RTL8187 inject_frame USBError: %s", e)
            return False
        except ValueError as e:
            logger.warning("RTL8187 inject_frame bad frame: %s", e)
            return False
        return True

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        """Stamp an incrementing 802.11 sequence number (step 0x10, the number lives in seq_ctrl
        bits [4:15]). The 8187L L-path has NO hardware seq assignment (HW_SEQNUM is 8187B-only), so
        without this every inject leaves seq=0 and an AP dedups our multi-frame association/EAPOL
        conversation as retransmissions (PMKID / WPS); see tx.stamp_seq_ctrl. Runs on the event loop
        before the blocking write, so ``_tx_seqno`` needs no lock."""
        if self.chip_variant is not None and self.chip_variant.is_rtl8187b:
            return frame_bytes                    # TX_CONF_HW_SEQNUM (dev.c:959)
        buf = bytearray(frame_bytes)
        self._tx_seqno = stamp_seq_ctrl(buf, self._tx_seqno)
        return bytes(buf)

    async def close(self) -> None:
        """rtl8187_stop: register writes first, then the RX URBs die (dev.c:1047)."""
        if self._started and self._rf_setup is not None and self.chip_variant is not None:
            asic_rev = self._rf_setup.asic_rev
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, stop, self.transport, lambda t: rtl8225_rf_stop(t, asic_rev),
                    self.chip_variant.is_rtl8187b)
            except (IOError, usb.core.USBError) as e:
                logger.debug("RTL8187 stop on close ignored: %s", e)
            self._started = False
        if self._rx_reader is not None:
            await self._rx_reader.stop()
            self._rx_reader = None
        if self._tx_status_reader is not None:
            await self._tx_status_reader.stop()
            self._tx_status_reader = None
        self._release()
        logger.debug("RTL8187 driver closed")
