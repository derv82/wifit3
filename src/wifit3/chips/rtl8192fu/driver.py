from __future__ import annotations

import asyncio
from typing import Callable

import usb.core

from wifit3.chips.driver import Driver, ProgressCallback
from wifit3.chips.rx_reader import RxReaderThread
from wifit3.dot11.parser import WlanFrameParser
from wifit3.errors import BringUpError
from wifit3.models.device_id import DeviceID

from .bringup import (
    calibrate_lc,
    fifo_and_rx_policy,
    finish_cold_init,
    firmware_bringup,
    mac_policy,
    set_tx_power,
    table_bringup,
)
from .chan import config_channel
from .chip import ChipIdentity, identify_chip
from .efuse import EfuseData, parse_efuse, read_efuse
from .firmware import load_firmware_blob, parse_firmware
from .iqk import phy_iq_calibrate
from .runtime import disable_rf, enable_hardware_led, enable_rf, open_rx_filters
from .rx import iter_frames, read_burst
from .transport import RTL8192FUTransport


class RTL8192FUDriver(Driver):
    SUPPORTED_CHANNELS = list(range(1, 15))
    CONFLICTING_LINUX_MODULES = ["rtl8xxxu"]

    def __init__(self, dev: usb.core.Device, id_entry: DeviceID) -> None:
        super().__init__()
        self.dev = dev
        self.id_entry = id_entry
        self.transport = RTL8192FUTransport(dev)
        self.identity: ChipIdentity | None = None
        self.mac_address: str | None = None
        self.is_warm = False
        self._rx_callback: Callable | None = None
        self._disconnect_callback: Callable[[], None] | None = None
        self._rx_reader: RxReaderThread | None = None
        self._efuse: EfuseData | None = None
        self._channel_lock = asyncio.Lock()
        self.current_channel = 1

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RTL8192FUDriver":
        return cls(dev, id_entry)

    def register_rx_callback(self, cb: Callable) -> None:
        self._rx_callback = cb

    def register_disconnect_callback(self, cb: Callable[[], None]) -> None:
        self._disconnect_callback = cb

    async def connect(self, progress_cb: ProgressCallback | None = None) -> bool:
        loop = asyncio.get_running_loop()
        progress = progress_cb or (lambda _value, _message: None)
        progress(0.02, "Claiming RTL8192FU USB interface")
        try:
            await loop.run_in_executor(None, self.transport.claim)
            progress(0.08, "Reading RTL8192FU EFUSE")
            await loop.run_in_executor(None, self._cold_bringup)
            progress(0.96, "Starting RTL8192FU receiver")
            enable_rf(self.transport)
            self._rx_reader = RxReaderThread(
                loop,
                self._read_once,
                self._dispatch,
                name="rtl8192fu-rx",
                on_fatal=lambda _error: self._disconnect_callback and self._disconnect_callback(),
            )
            self._rx_reader.start()
            await loop.run_in_executor(None, open_rx_filters, self.transport)
        except Exception as error:
            if self._rx_reader is not None:
                await self._rx_reader.stop()
                self._rx_reader = None
            self.transport.release()
            if isinstance(error, BringUpError):
                raise
            raise BringUpError("RTL8192FU bring-up", str(error)) from error
        self.is_warm = True
        progress(1.0, f"RTL8192FU online ({self.mac_address})")
        return True

    def _cold_bringup(self) -> None:
        self.identity = identify_chip(self.transport)
        self._efuse = parse_efuse(read_efuse(self.transport))
        self.mac_address = self._efuse.mac_address.hex(":")
        firmware_bringup(self.transport, parse_firmware(load_firmware_blob()))
        table_bringup(self.transport, self._efuse)
        fifo_and_rx_policy(self.transport)
        mac_policy(self.transport)
        set_tx_power(self.transport, self._efuse)
        calibrate_lc(self.transport)
        phy_iq_calibrate(self.transport, self._efuse.rfe_type)
        finish_cold_init(self.transport)
        enable_hardware_led(self.transport)

    def _read_once(self) -> bytes | None:
        endpoints = self.transport.endpoints
        if endpoints is None:
            return None
        return read_burst(self.dev, endpoints.bulk_in)

    def _dispatch(self, buffer: bytes) -> None:
        if self._rx_callback is None:
            return
        for frame, rssi in iter_frames(buffer):
            if len(frame) >= 10 and frame[0] == 0xD4:
                self.record_ack(frame)
            packet = WlanFrameParser.parse_80211_frame(frame, rssi)
            if packet is not None:
                self._rx_callback(packet)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        if channel not in self.SUPPORTED_CHANNELS:
            raise ValueError(f"unsupported RTL8192FU channel: {channel}")
        if self._efuse is None:
            raise BringUpError("RTL8192FU channel", "driver is not connected")
        if channel == self.current_channel:
            return True
        loop = asyncio.get_running_loop()
        async with self._channel_lock:
            await loop.run_in_executor(None, self._set_channel_sync, channel)
            self.current_channel = channel
        return True

    def _set_channel_sync(self, channel: int) -> None:
        if self._efuse is None:
            raise RuntimeError("RTL8192FU EFUSE is unavailable")
        set_tx_power(self.transport, self._efuse, channel)
        config_channel(self.transport, channel)

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        raise BringUpError("RTL8192FU TX", "TX descriptor construction is not ported yet")

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        return frame_bytes

    async def _enable_rx_acks(self) -> None:
        raise BringUpError("RTL8192FU RX ACK", "RX filtering is not ported yet")

    async def _disable_rx_acks(self) -> None:
        return

    async def close(self) -> None:
        if self._rx_reader is not None:
            await self._rx_reader.stop()
            self._rx_reader = None
        if self.transport.interface_number is not None:
            self.transport.write8(0x0522, 0xFF)
            self.transport.write16(0x06A0, 0)
            self.transport.write16(0x06A4, 0)
            self.transport.write8(0x0522, 0xFF)
            disable_rf(self.transport)
        self.transport.release()
