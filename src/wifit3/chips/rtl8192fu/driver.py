from __future__ import annotations

import asyncio
from typing import Callable

import usb.core

from wifit3.chips.driver import Driver, FakeMacSupport, ProgressCallback
from wifit3.chips.rx_reader import RxReaderThread
from wifit3.dot11.parser import WlanFrameParser
from wifit3.errors import BringUpError
from wifit3.models.device_id import DeviceID

from . import constants as C
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
from .power import power_off
from .runtime import configure_monitor_filter, disable_rf, enable_hardware_led, enable_rf, open_rx_filters
from .rx import iter_frames, read_burst
from .transport import RTL8192FUTransport
from .tx import build_tx_packet, bulk_out_index, stamp_sequence


class RTL8192FUDriver(Driver):
    SUPPORTED_CHANNELS = list(range(1, 15))
    CONFLICTING_LINUX_MODULES = ["rtl8xxxu"]
    FAKE_MAC = FakeMacSupport.SPOOFABLE

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
        self._tx_lock = asyncio.Lock()
        self._active_monitor_lock = asyncio.Lock()
        self._tx_sequence = 0
        self.current_channel = 1
        self._cck_new_agc = False

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
        progress(0.02, "Claiming USB interface")
        try:
            await loop.run_in_executor(None, self.transport.claim)
            progress(0.08, "Probing chip state")
            await loop.run_in_executor(None, self._identify_chip)
            progress(0.12, "Reading EFUSE")
            await loop.run_in_executor(None, self._read_efuse)
            progress(0.18, "Power-on + firmware download")
            await loop.run_in_executor(None, self._firmware_bringup)
            progress(0.40, "PHY init (mac/bb/agc/rf tables)")
            await loop.run_in_executor(None, self._table_bringup)
            progress(0.58, "FIFO + RX/MAC init")
            await loop.run_in_executor(None, self._configure_fifo_and_mac)
            progress(0.68, "Applying TX power")
            await loop.run_in_executor(None, self._apply_tx_power)
            progress(0.76, "Radio calibration")
            await loop.run_in_executor(None, self._calibrate_radio)
            progress(0.88, "Finishing hardware init")
            await loop.run_in_executor(None, self._finish_cold_init)
            progress(0.94, "Enabling monitor mode")
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
            await loop.run_in_executor(None, configure_monitor_filter, self.transport)
            await loop.run_in_executor(None, self._set_channel_sync, 1)
            self.current_channel = 1
        except Exception as error:
            if self._rx_reader is not None:
                await self._rx_reader.stop()
                self._rx_reader = None
            self.transport.release()
            if isinstance(error, BringUpError):
                raise
            raise BringUpError("RTL8192FU bring-up", str(error)) from error
        self.is_warm = True
        progress(1.0, "RTL8192FU online")
        return True

    def _cold_bringup(self) -> None:
        self._identify_chip()
        self._read_efuse()
        self._firmware_bringup()
        self._table_bringup()
        self._configure_fifo_and_mac()
        self._apply_tx_power()
        self._calibrate_radio()
        self._finish_cold_init()

    def _identify_chip(self) -> None:
        self.identity = identify_chip(self.transport)

    def _read_efuse(self) -> None:
        self._efuse = parse_efuse(read_efuse(self.transport))
        self.mac_address = self._efuse.mac_address.hex(":")

    def _firmware_bringup(self) -> None:
        firmware_bringup(self.transport, parse_firmware(load_firmware_blob()))

    def _table_bringup(self) -> None:
        if self._efuse is None:
            raise RuntimeError("RTL8192FU EFUSE is unavailable")
        table_bringup(self.transport, self._efuse)

    def _configure_fifo_and_mac(self) -> None:
        fifo_and_rx_policy(self.transport)
        mac_policy(self.transport)

    def _apply_tx_power(self) -> None:
        if self._efuse is None:
            raise RuntimeError("RTL8192FU EFUSE is unavailable")
        set_tx_power(self.transport, self._efuse)

    def _calibrate_radio(self) -> None:
        if self._efuse is None:
            raise RuntimeError("RTL8192FU EFUSE is unavailable")
        calibrate_lc(self.transport)
        phy_iq_calibrate(self.transport, self._efuse.rfe_type)

    def _finish_cold_init(self) -> None:
        self._cck_new_agc = finish_cold_init(self.transport)
        enable_hardware_led(self.transport)

    def _read_once(self) -> bytes | None:
        endpoints = self.transport.endpoints
        if endpoints is None:
            return None
        return read_burst(self.dev, endpoints.bulk_in)

    def _dispatch(self, buffer: bytes) -> None:
        if self._rx_callback is None:
            return
        for frame, rssi in iter_frames(buffer, self._cck_new_agc):
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
        endpoints = self.transport.endpoints
        if endpoints is None:
            raise BringUpError("RTL8192FU TX", "driver is not connected")
        packet = build_tx_packet(frame_bytes)
        endpoint = endpoints.bulk_out[bulk_out_index(frame_bytes, len(endpoints.bulk_out))]
        loop = asyncio.get_running_loop()
        async with self._tx_lock:
            await loop.run_in_executor(None, self.transport.bulk_write, endpoint, packet)
        return True

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        stamped = stamp_sequence(frame_bytes, self._tx_sequence)
        self._tx_sequence = (self._tx_sequence + 1) & 0xFFF
        return stamped

    async def _enable_rx_acks(self) -> None:
        """No-op: monitor setup mirrors core.c:6932-6935 and keeps RCR_ACCEPT_CTRL_FRAME set."""
        return

    async def _disable_rx_acks(self) -> None:
        """No-op, matching `_enable_rx_acks`; the source-backed control filter stays active."""
        return

    def _program_active_monitor(self, mac: bytes, bssid: bytes, link_type: int) -> None:
        value = self.transport.read8(C.REG_MSR) & 0x0C
        self.transport.write8(C.REG_MSR, value | link_type)
        for offset, value in enumerate(mac):
            self.transport.write8(C.REG_MACID + offset, value)
        for offset, value in enumerate(bssid):
            self.transport.write8(C.REG_BSSID + offset, value)

    async def enter_active_monitor(self, mac: bytes, bssid: bytes | None = None) -> bytes:
        if len(mac) != 6 or bssid is not None and len(bssid) != 6:
            raise ValueError("RTL8192FU active-monitor addresses must be six bytes")
        requested_bssid = bytes(bssid or mac)
        link_type = C.MSR_LINKTYPE_AP if bytes(mac) == requested_bssid else C.MSR_LINKTYPE_STATION
        loop = asyncio.get_running_loop()
        async with self._active_monitor_lock:
            await loop.run_in_executor(
                None, self._program_active_monitor, bytes(mac), requested_bssid, link_type
            )
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        if self._efuse is None:
            return
        loop = asyncio.get_running_loop()
        async with self._active_monitor_lock:
            await loop.run_in_executor(
                None,
                self._program_active_monitor,
                self._efuse.mac_address,
                bytes(6),
                C.MSR_LINKTYPE_NONE,
            )

    async def close(self) -> None:
        if self._rx_reader is not None:
            await self._rx_reader.stop()
            self._rx_reader = None
        try:
            if self.transport.interface_number is not None:
                self.transport.write8(0x0522, 0xFF)
                self.transport.write16(0x06A0, 0)
                self.transport.write16(0x06A4, 0)
                self.transport.write8(0x0522, 0xFF)
                disable_rf(self.transport)
                power_off(self.transport)
        finally:
            try:
                self.transport.release()
            finally:
                self._efuse = None
                self.is_warm = False
