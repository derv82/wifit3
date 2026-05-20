"""RTL8187L driver — glues the bring-up chain onto the WlanDriver Protocol.

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
    chips/rtl8187/RTL8187L.md. WlanDriver protocol surface complete.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

import usb.core
import usb.util

from wifit3.engine.protocols import DeviceID, ProgressCallback

from wifit3.wlan.packet import WlanFrameParser

from .chan import set_channel as _set_channel
from .constants import REG_CMD, CMD_RX_ENABLE, CMD_TX_ENABLE, USB_PID_RTL8187, USB_VID_REALTEK
from .mac import (
    ChipVariant,
    cold_bring_up,
    detect_chip_variant,
    is_chip_warm,
    read_perm_mac,
)
from .rtl8225 import RfSetup, build_rf_init, probe_rf_setup
from .rx import parse_rx_urb, probe_endpoints, read_rx_burst
from .transport import RTL8187Transport
from .tx import inject_frame as _inject_frame

logger = logging.getLogger(__name__)


class RTL8187Driver:
    """Driver for the Realtek RTL8187L (e.g. ALFA AWUS036H).

    2.4 GHz only, hard-MAC chipset (no firmware blob). Bring-up is a
    pure-control-transfer sequence mirrored from the in-tree Linux
    driver — see module docstring for the milestone breakdown.
    """

    SUPPORTED_IDS = [
        DeviceID(USB_VID_REALTEK, USB_PID_RTL8187, "Realtek RTL8187L / ALFA AWUS036H"),
    ]
    # 2.4 GHz channels 1..13. Channel 14 is JP-only and the chip supports
    # it (rtl818x_channels[13].center_freq=2484) but we leave it off the
    # default hop list to match the other 2.4 GHz drivers.
    SUPPORTED_CHANNELS = list(range(1, 14))

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RTL8187Driver":
        return cls(dev)

    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self.transport = RTL8187Transport(dev)
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._rx_task: Optional[asyncio.Task] = None
        self._rx_running = False
        self._bulk_in_ep: Optional[int] = None
        self._claimed = False
        self._rf_setup: Optional[RfSetup] = None

        # WlanDriver Protocol surface area.
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self.current_channel: int = 1
        self.chip_variant: Optional[ChipVariant] = None

    # ---- discovery hook ---------------------------------------------------
    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_callback = cb

    # ---- USB claim helpers -----------------------------------------------
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

            _progress(0.15, "Probing chip variant (TX_CONF HWVER)")
            self.chip_variant = await loop.run_in_executor(
                None, detect_chip_variant, self.transport
            )
            logger.info(
                "chip_variant: %s (HWVER raw=0x%08x, is_8187b_masquerade=%s)",
                self.chip_variant.name,
                self.chip_variant.hwver_raw,
                self.chip_variant.is_8187b_masquerade,
            )
            if self.chip_variant.is_8187b_masquerade:
                logger.error(
                    "RTL8187B in 0x8187 disguise — this driver is 8187L only. "
                    "Bring-up aborted."
                )
                return False

            _progress(0.25, "Reading permanent MAC")
            mac_bytes = await loop.run_in_executor(None, read_perm_mac, self.transport)
            self.mac_address = ":".join(f"{b:02x}" for b in mac_bytes)
            logger.info("mac_address: %s", self.mac_address)

            _progress(0.35, "Probing warm/cold state")
            warm = await loop.run_in_executor(None, is_chip_warm, self.transport)
            logger.info("is_warm: %s", warm)
            if warm:
                # M2a doesn't yet implement the warm-reattach short-circuit —
                # for now we re-run the cold bring-up either way. M3 (which
                # owns the RX loop) is the right place to add the warm
                # reattach + bulk-IN smoke-test pattern.
                logger.info("warm chip — re-running cold bring-up anyway (M2a)")

            _progress(0.45, "Probing RF (asic_rev + variant)")
            self._rf_setup = await loop.run_in_executor(
                None, probe_rf_setup, self.transport
            )

            _progress(0.50, "Building RF init callback")
            rf_init = build_rf_init(self.transport, self._rf_setup)

            _progress(0.55, "Running cold bring-up (init_hw + RF init + start)")
            await loop.run_in_executor(None, cold_bring_up, self.transport, rf_init)

            # Verify CMD latched TX_ENABLE | RX_ENABLE.
            cmd = await loop.run_in_executor(None, self.transport.read8, REG_CMD)
            if not (cmd & CMD_TX_ENABLE and cmd & CMD_RX_ENABLE):
                logger.error(
                    "bring-up finished but CMD=0x%02x missing TX/RX enable bits", cmd
                )
                return False
            logger.info("CMD=0x%02x — TX_ENABLE + RX_ENABLE latched", cmd)

            _progress(0.85, "Probing endpoints + starting RX loop")
            eps = probe_endpoints(self.dev)
            self._bulk_in_ep = eps.primary_bulk_in
            self._rx_running = True
            self._rx_task = asyncio.create_task(self._rx_loop())

            self.is_warm = True  # subsequent connect()s will see us as warm
            _progress(1.00, "RTL8187L online — RX loop polling bulk-IN")
            return True

        except (IOError, usb.core.USBError, NotImplementedError) as e:
            logger.error("RTL8187 connect failed: %s", e)
            return False

    # ---- RX loop ----------------------------------------------------------
    async def _rx_loop(self) -> None:
        """Poll bulk-IN, decode each URB, dispatch parsed frames.

        Runs the synchronous PyUSB read in the default executor so the
        event loop stays responsive. One URB = one frame on 8187L (no
        coalescing), so parse_rx_urb is one-shot per read.
        """
        loop = asyncio.get_event_loop()
        ep = self._bulk_in_ep
        assert ep is not None, "_rx_loop called before _bulk_in_ep was set"
        logger.info("RTL8187L RX loop started on EP 0x%02x", ep)

        while self._rx_running:
            try:
                buf = await loop.run_in_executor(
                    None, read_rx_burst, self.dev, ep
                )
            except usb.core.USBError as e:
                # Non-timeout USBError (e.g. pipe stall) — log and back off.
                logger.error("RTL8187L bulk-IN error: %s", e)
                await asyncio.sleep(0.05)
                continue
            except Exception as e:
                logger.exception("RTL8187L RX loop unexpected error: %s", e)
                await asyncio.sleep(0.05)
                continue

            if buf is None:
                # Timeout — normal when the radio is on a quiet channel.
                continue

            rx = parse_rx_urb(buf)
            if rx is None or rx.has_fcs_error:
                continue

            parsed = WlanFrameParser.parse_80211_frame(rx.mpdu, rx.rssi_dbm)
            if parsed is not None and self._rx_callback is not None:
                try:
                    self._rx_callback(parsed)
                except Exception as e:
                    logger.exception("rx_callback raised: %s", e)

        logger.info("RTL8187L RX loop stopped")

    # ---- channel tune (M4) -----------------------------------------------
    async def set_channel(self, channel: int) -> bool:
        if self._rf_setup is None:
            logger.error("RTL8187 set_channel(%d): connect() must run first", channel)
            return False
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, _set_channel,
                self.transport, self._rf_setup.asic_rev,
                self._rf_setup.variant, channel,
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
    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        """Build tx_hdr + bulk-OUT inject.

        ``use_no_ack=True`` is the "fire-and-forget" mode used for
        spoofed frames (deauths, EAPOL inject) — the chip is told to
        send the frame *once* (``retry_count=1``) instead of retrying
        7× waiting for an ACK from a sender we're impersonating. Real
        retries would let the TX FIFO back up past the bulk-OUT
        timeout. ``use_no_ack=False`` uses ``RETRY_COUNT=7`` for
        normal unicast TX (where we actually want delivery).
        """
        from .tx import RETRY_COUNT
        retry_count = 1 if use_no_ack else RETRY_COUNT
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: _inject_frame(self.dev, frame_bytes, retry_count=retry_count),
            )
            return True
        except usb.core.USBError as e:
            logger.error("RTL8187 inject_frame USBError: %s", e)
            return False
        except ValueError as e:
            logger.warning("RTL8187 inject_frame bad frame: %s", e)
            return False

    async def close(self) -> None:
        self._rx_running = False
        if self._rx_task:
            self._rx_task.cancel()
            try:
                await self._rx_task
            except asyncio.CancelledError:
                pass
        self._release()
        logger.info("RTL8187 driver closed")
