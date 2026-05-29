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
from typing import Callable, Optional

import usb.core
import usb.util

from wifit3.engine.protocols import DeviceID, ProgressCallback
from wifit3.wlan.packet import WlanFrameParser

from .bbp import init_bbp
from .chan import antenna_defaults, config_channel, config_ant, set_channel
from .constants import (
    DEFAULT_RSSI_OFFSET,
    EEPROM_ANTENNA,
    EEPROM_ANTENNA_RF_TYPE,
    EEPROM_CALIBRATE_OFFSET,
    EEPROM_CALIBRATE_OFFSET_RSSI,
    EEPROM_MAC_ADDR_0,
    RT2500USB_DEVICE_TABLE,
)
from .mac import (
    apply_monitor_filter,
    init_registers,
    is_chip_warm,
    read_revision,
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
    # RF2525/RF2525E are 2.4 GHz only (channels 1-14).
    SUPPORTED_CHANNELS = list(range(1, 15))

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RT2500USBDriver":
        return cls(dev)

    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self.transport = RT2500USBTransport(dev)
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._rx_reader: Optional[RxReaderThread] = None
        self._bulk_in_ep: Optional[int] = None
        self._bulk_out_ep: Optional[int] = None
        self._claimed = False

        # EEPROM-derived, set at connect() time.
        self.rf_type: int = 0
        self._ant_tx: int = 0
        self._ant_rx: int = 0
        self._rssi_offset: int = DEFAULT_RSSI_OFFSET

        # WlanDriver Protocol surface area.
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self.current_channel: int = 1

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_callback = cb

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
        mac_off = EEPROM_MAC_ADDR_0 * 2
        mac = eeprom[mac_off:mac_off + 6]
        self.mac_address = ":".join(f"{b:02x}" for b in mac)

        ant_off = EEPROM_ANTENNA * 2
        antenna = eeprom[ant_off] | (eeprom[ant_off + 1] << 8)
        self.rf_type = get_field16(antenna, EEPROM_ANTENNA_RF_TYPE)
        self._ant_tx, self._ant_rx = antenna_defaults(antenna)

        cal_off = EEPROM_CALIBRATE_OFFSET * 2
        cal = eeprom[cal_off] | (eeprom[cal_off + 1] << 8)
        self._rssi_offset = (
            DEFAULT_RSSI_OFFSET if cal == 0xFFFF
            else get_field16(cal, EEPROM_CALIBRATE_OFFSET_RSSI)
        )

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
                prog(0.55, "chip WARM — skipping cold init")
            elif warm:
                # HOST_READY set but bulk-IN wedged. On Windows+WinUSB this
                # pipe often can't be recovered in userland — ask for a replug
                # rather than thrash. [[feedback_warm_reattach]]
                prog(0.5, "chip warm but bulk-IN wedged")
                logger.error(
                    "rt2500usb: bulk-IN pipe is wedged. Please unplug the "
                    "device, wait ~5s, replug, and reconnect."
                )
                return False
            else:
                prog(0.35, "cold bring-up: init_registers + set_state(AWAKE)")
                await loop.run_in_executor(
                    None, init_registers, self.transport, revision
                )
                prog(0.5, "init_bbp")
                await loop.run_in_executor(None, init_bbp, self.transport, eeprom)

            # Always (re)apply the monitor posture + antenna + channel, so a
            # warm reattach lands in a known always-monitor state.
            prog(0.7, "config_ant + monitor filter")
            await loop.run_in_executor(
                None, config_ant, self.transport, self.rf_type,
                self._ant_tx, self._ant_rx,
            )
            await loop.run_in_executor(None, apply_monitor_filter, self.transport)
            await loop.run_in_executor(
                None, set_channel, self.transport, self.rf_type, self.current_channel
            )
            prog(0.9, f"tuned to channel {self.current_channel}")

            self._rx_reader = RxReaderThread(
                loop, self._rx_read_once, self._rx_dispatch, name="rt2500usb-rx"
            )
            self._rx_reader.start()
            self.is_warm = True
            prog(1.0, "connected")
            return True
        except Exception as e:
            logger.exception("rt2500usb connect failed: %s", e)
            return False

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
        parsed = WlanFrameParser.parse_80211_frame(rx.mpdu, rx.rssi_dbm)
        if parsed is not None and self._rx_callback is not None:
            try:
                self._rx_callback(parsed)
            except Exception as e:
                logger.exception("rx_callback raised: %s", e)

    # ---- channel tune ---------------------------------------------------
    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        if channel not in self.SUPPORTED_CHANNELS:
            logger.warning("rt2500usb: channel %d not supported", channel)
            return False
        try:
            ok = await asyncio.get_event_loop().run_in_executor(
                None, config_channel, self.transport, self.rf_type, channel
            )
        except Exception as e:
            logger.error("rt2500usb set_channel(%d) failed: %s", channel, e)
            return False
        if ok:
            self.current_channel = channel
        return ok

    # ---- TX -------------------------------------------------------------
    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        """Inject a raw 802.11 frame (no FCS) at 1 Mbps CCK. Only ever
        called behind an explicit user action [[passive_by_default]]."""
        if self._bulk_out_ep is None:
            logger.error("rt2500usb: no bulk-OUT endpoint; cannot inject")
            return False
        try:
            sent = await asyncio.get_event_loop().run_in_executor(
                None, _tx_inject, self.dev, self._bulk_out_ep,
                frame_bytes, not use_no_ack,
            )
        except Exception as e:
            logger.error("rt2500usb inject_frame failed: %s", e)
            return False
        return sent > 0

    # ---- teardown -------------------------------------------------------
    async def close(self) -> None:
        if self._rx_reader is not None:
            await self._rx_reader.stop()
            self._rx_reader = None
        self._release()
        logger.info("rt2500usb driver closed")
