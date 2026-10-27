from __future__ import annotations

import asyncio
from typing import Callable

import usb.core

from wifit3.chips.driver import Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.models.device_id import DeviceID

from . import constants as C
from .base import rtl_watchdog_wq_callback
from .core import (
    Cfg80211ChanDef,
    ieee80211_channel_to_frequency,
    rtl_op_add_interface,
    rtl_op_config,
    rtl_op_configure_filter,
    rtl_op_remove_interface,
)
from .priv import RtlPriv
from .transport import RTL8192CUTransport
from .usb import ProbeError, UsbEndpointDescriptor, rtl_usb_probe, rtl_usb_start

WATCHDOG_SECONDS = C.RTL_WATCH_DOG_TIME / 1000
MONITOR_FILTER_FLAGS = C.FIF_ALLMULTI | C.FIF_CONTROL | C.FIF_OTHER_BSS
"""What mac80211 hands configure_filter for a promiscuous monitor interface."""
FIRST_CHANNEL = 1
"""mac80211's default chandef at interface up: the first 2.4 GHz channel."""


class RTL8192CUDriver(Driver):
    SUPPORTED_CHANNELS = list(range(1, 15))
    CONFLICTING_LINUX_MODULES = ["rtl8192cu", "rtl8xxxu"]
    FAKE_MAC = FakeMacSupport.SPOOFABLE

    def __init__(self, dev: usb.core.Device, id_entry: DeviceID) -> None:
        super().__init__()
        self.dev = dev
        self.id_entry = id_entry
        self.transport = RTL8192CUTransport(dev)
        self.priv = RtlPriv(self.transport)
        self.mac_address: str | None = None
        self.is_warm = False
        self._endpoints: list[UsbEndpointDescriptor] = []
        self._is_high_speed = True
        self._rx_callback: Callable | None = None
        self._disconnect_callback: Callable[[], None] | None = None
        self._io_lock = asyncio.Lock()
        self._watchdog_task: asyncio.Task[None] | None = None
        self._started = False

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RTL8192CUDriver":
        return cls(dev, id_entry)

    def register_rx_callback(self, cb: Callable) -> None:
        self._rx_callback = cb

    def register_disconnect_callback(self, cb: Callable[[], None]) -> None:
        self._disconnect_callback = cb

    def _bringup(self) -> None:
        """rtl_usb_probe, then mac80211's monitor interface up: rtl_op_start's adapter_start,
        configure_filter, and the first config with the default channel."""
        try:
            rtl_usb_probe(self.priv, self._endpoints, self._is_high_speed)
        except ProbeError as error:
            raise BringUpError("RTL8192CU probe", str(error)) from error
        self.mac_address = self.priv.efuse.dev_addr.hex(":")
        err = rtl_usb_start(self.priv)
        if err:
            raise BringUpError("RTL8192CU hw_init", f"rtl92cu_hw_init failed ({err})")
        rtl_op_configure_filter(self.priv, MONITOR_FILTER_FLAGS, MONITOR_FILTER_FLAGS)
        self._tune(FIRST_CHANNEL)

    def _tune(self, channel: int) -> None:
        chandef = Cfg80211ChanDef(hw_value=channel, center_freq=ieee80211_channel_to_frequency(channel))
        rtl_op_config(self.priv, C.IEEE80211_CONF_CHANGE_CHANNEL, chandef)

    async def connect(self, progress_cb: ProgressCallback | None = None) -> bool:
        loop = asyncio.get_running_loop()
        progress = progress_cb or (lambda _value, _message: None)
        try:
            progress(0.00, "Claiming USB interface")
            await loop.run_in_executor(None, self.transport.claim)
            assert self.transport.interface is not None
            self._endpoints = [UsbEndpointDescriptor(ep.bEndpointAddress, ep.bmAttributes)
                               for ep in self.transport.interface]
            self._is_high_speed = self.transport.is_high_speed()
            progress(0.05, "Reading EFUSE, downloading firmware, initialising MAC/BB/RF")
            async with self._io_lock:
                await loop.run_in_executor(None, self._bringup)
        except Exception as error:
            self.transport.release()
            if isinstance(error, BringUpError):
                raise
            raise BringUpError("RTL8192CU bring-up", str(error)) from error
        self._started = True
        self._watchdog_task = loop.create_task(self._watchdog())
        progress(1.0, "RTL8192CU online")
        return True

    async def _watchdog(self) -> None:
        """rtl_op_start fires the watchdog timer once straight away (core.c:141), then every
        RTL_WATCH_DOG_TIME (base.c:2221)."""
        loop = asyncio.get_running_loop()
        while True:
            async with self._io_lock:
                await loop.run_in_executor(None, rtl_watchdog_wq_callback, self.priv)
            await asyncio.sleep(WATCHDOG_SECONDS)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        if channel not in self.SUPPORTED_CHANNELS:
            raise ValueError(f"unsupported RTL8192CU channel: {channel}")
        if not self._started:
            raise BringUpError("RTL8192CU channel", "driver is not connected")
        async with self._io_lock:
            await asyncio.get_running_loop().run_in_executor(None, self._tune, channel)
        return True

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        raise NotImplementedError("rtl92cu_tx_fill_desc is not ported")

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        return frame_bytes

    def _set_ack_filter(self, admit: bool) -> None:
        """REG_RXFLTMAP1 is a per-subtype accept map; hw_init clears it (hw.c:717)."""
        ops = self.priv.cfg.ops
        ack = C.BIT(C.IEEE80211_STYPE_ACK >> 4)
        ctrl_filter = ops.get_hw_reg(self.priv, C.HW_VAR_CTRL_FILTER)
        ops.set_hw_reg(self.priv, C.HW_VAR_CTRL_FILTER, ctrl_filter | ack if admit else ctrl_filter & ~ack)

    async def _enable_rx_acks(self) -> None:
        async with self._io_lock:
            await asyncio.get_running_loop().run_in_executor(None, self._set_ack_filter, True)

    async def _disable_rx_acks(self) -> None:
        async with self._io_lock:
            await asyncio.get_running_loop().run_in_executor(None, self._set_ack_filter, False)

    def _add_station_interface(self, mac: bytes) -> None:
        if self.priv.mac80211.vif is not None:
            rtl_op_remove_interface(self.priv)
        err = rtl_op_add_interface(self.priv, C.NL80211_IFTYPE_STATION, mac)
        if err:
            raise BringUpError("RTL8192CU active monitor", f"rtl_op_add_interface failed ({err})")

    async def enter_active_monitor(self, mac: bytes, bssid: bytes | None = None) -> bytes:
        """rtl_op_add_interface as STATION: MSR to infra and REG_MACID to ``mac``, so the MAC
        ACKs frames to it. Unlinked, set_network_type keeps the RCR BSSID checks off."""
        if len(mac) != 6:
            raise ValueError("RTL8192CU active-monitor MAC must be six bytes")
        if not self._started:
            raise BringUpError("RTL8192CU active monitor", "driver is not connected")
        async with self._io_lock:
            await asyncio.get_running_loop().run_in_executor(None, self._add_station_interface, bytes(mac))
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        if not self._started or self.priv.mac80211.vif is None:
            return
        async with self._io_lock:
            await asyncio.get_running_loop().run_in_executor(None, rtl_op_remove_interface, self.priv)

    async def close(self) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None
        self.transport.release()
        self._started = False
        self.is_warm = False
