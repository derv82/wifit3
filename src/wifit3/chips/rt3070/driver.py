"""RT3070Driver — clean-room Ralink RT3070 (ALFA AWUS036NH, 148f:3070) driver.

``connect()`` runs the pcap-verified cold bring-up (probe → EFUSE → firmware → MAC/BBP/
RFCSR init + RX-filter calibration → radio-on), then the monitor entry (``enable_monitor``:
interface-up filter → post-radio config → monitor filter) and the default channel tune,
and starts the bulk-IN RX reader. Every wire op through the channel hops is byte-diffed
against the cold-boot capture by ``scripts/verify_pcap.py rt3070`` (the operational gate).

TX (``inject_frame`` → ``tx.send_frame``) is wired and pcap-faithful but **never fired by
the driver** — injection/deauth is the user's explicit action [[passive_by_default]].

Standalone port (NOT a ``chips/rt2800usb`` DeviceID delta — that shared base has a
confirmed EFUSE addressing bug); see ``chips/rt3070/RT3070.md`` for the ground-truth facts.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, ClassVar, List, Optional

import usb.core
import usb.util

from wifit3.engine.protocols import DeviceID, ProgressCallback
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
        DeviceID(_VID_RALINK, _PID_RT3070, "Ralink RT3070 1T1R / ALFA AWUS036NH"),
    ]
    SUPPORTED_CHANNELS: ClassVar[List[int]] = list(range(1, 15))   # 2.4 GHz, 20 MHz

    def __init__(self, transport: RT3070Transport):
        self.transport = transport
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self._chip = None
        self._eeprom: Optional[eeprom.EepromValues] = None
        self._drv = None                 # DrvData (RX-filter calibration) from init
        self._channel: Optional[int] = None
        self._lna_gain: int = 0          # current-channel LNA gain (RSSI conversion)
        self._bulk_in_ep: Optional[int] = None
        self._rx_cb: Optional[Callable[[dict], None]] = None
        self._reader: Optional[RxReaderThread] = None
        self._io_lock = asyncio.Lock()   # serialize EP0 batches (set_channel vs inject)

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RT3070Driver":
        return cls(RT3070Transport(dev))

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_cb = cb

    # ---- bring-up (blocking; run in an executor) --------------------------
    def _bringup(self) -> None:
        """Deterministic cold bring-up, in wire order — keep in lockstep with
        ``scripts/verify_pcap.py`` ``_walk_init`` (the gate replays this exact sequence)."""
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
        bbp.init_bbp(t, chip, ev)                              # BBP init (30xx + EEPROM)
        self._drv = rfcsr.init_rfcsr_30xx(t, chip, ev)         # RFCSR init + RX-filter cal
        mac.enable_radio_finish(t, chip, ev)                   # MCU current / RX / LED
        mac.set_radio_led(t, ev)                                # leds-radio on
        mac.start_queue_rx(t)                                   # enable RX queue

        monitor.enable_monitor(t, chip, ev, self._drv)         # airmon monitor entry

    def _tune(self, channel: int) -> None:
        chan.set_channel(self.transport, self._chip, self._eeprom, self._drv, channel)
        self._lna_gain = chan.config_lna_gain(self._eeprom, channel)

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        loop = asyncio.get_running_loop()

        if progress_cb:
            progress_cb(0.1, "RT3070: probe + EFUSE + firmware + radio bring-up")
        try:
            await loop.run_in_executor(None, self._bringup)
        except Exception:  # noqa: BLE001
            logger.exception("RT3070 bring-up failed")
            if progress_cb:
                progress_cb(1.0, "RT3070 bring-up failed")
            return False

        self._bulk_in_ep = probe_endpoints(self.transport.dev).primary_bulk_in
        logger.info("RT3070 EFUSE: mac=%s rf=0x%04x %dT%dR ext_lna_2g=%s freq_off=%d",
                    self.mac_address, self._eeprom.rf_type, self._eeprom.tx_chain_num,
                    self._eeprom.rx_chain_num, self._eeprom.external_lna_bg,
                    self._eeprom.freq_offset)

        if progress_cb:
            progress_cb(0.9, f"Tuning to channel {_SCAN_START_CHANNEL}")
        await self.set_channel(_SCAN_START_CHANNEL)

        # bulk-IN RX reader on a dedicated thread (off the event loop so the TUI can't
        # starve RX); each aggregated buffer → 802.11 frames + RSSI → rx callback.
        self._reader = RxReaderThread(loop, self._read_once, self._dispatch, name="rt3070-rx")
        self._reader.start()

        if progress_cb:
            progress_cb(1.0, f"RT3070 tuned to channel {_SCAN_START_CHANNEL} @ 20 MHz")
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
        out_eps = probe_endpoints(self.transport.dev).bulk_out
        if not out_eps:
            logger.error("RT3070 inject: no bulk-OUT endpoint")
            return False
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(
                None, tx.send_frame, self.transport.dev, out_eps[0], frame_bytes, use_no_ack)
        return True

    async def close(self) -> None:
        if self._reader is not None:
            await self._reader.stop()
            self._reader = None
        try:
            usb.util.dispose_resources(self.transport.dev)
        except Exception:  # noqa: BLE001
            pass
