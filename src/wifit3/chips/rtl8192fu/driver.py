from __future__ import annotations

import asyncio
from importlib.resources import files
from typing import Callable

import usb.core

from wifit3.chips.driver import Driver, FakeMacSupport, ProgressCallback
from wifit3.chips.rx_reader import RxReaderThread
from wifit3.dot11.parser import WlanFrameParser
from wifit3.errors import BringUpError, is_device_gone
from wifit3.models.device_id import DeviceID

from .core import (
    Nl80211Iftype,
    rtl8xxxu_config,
    Rtl8xxxuPriv,
    rtl8xxxu_deinit_led,
    rtl8xxxu_set_bssid,
    rtl8xxxu_set_linktype,
    rtl8xxxu_set_mac,
    rtl8xxxu_stop,
    rtl8xxxu_probe,
    rtl8xxxu_watchdog_callback,
    start_monitor_interface,
)
from .rtl8192f import rtl8192fu_fops
from .rx import read_burst, rtl8xxxu_c2hcmd_callback, rtl8xxxu_parse_rxdesc24
from .transport import RTL8192FUTransport
from .tx import rtl8xxxu_tx

RSSI_UNKNOWN = 0  # RX_FLAG_NO_SIGNAL_VAL: the descriptor carried no PHY status
WATCHDOG_SECONDS = 2.0  # core.c:7358 schedule_delayed_work(&priv->ra_watchdog, 2 * HZ)


class RTL8192FUDriver(Driver):
    SUPPORTED_CHANNELS = list(range(1, 15))
    CONFLICTING_LINUX_MODULES = ["rtl8xxxu"]
    FAKE_MAC = FakeMacSupport.SPOOFABLE

    def __init__(self, dev: usb.core.Device, id_entry: DeviceID) -> None:
        super().__init__()
        self.dev = dev
        self.id_entry = id_entry
        self.transport = RTL8192FUTransport(dev)
        self.priv = Rtl8xxxuPriv(self.transport, rtl8192fu_fops)
        self.mac_address: str | None = None
        self.is_warm = False
        self.current_channel = 1
        self._rx_callback: Callable | None = None
        self._disconnect_callback: Callable[[Exception], None] | None = None
        self._device_gone = False
        self._rx_reader: RxReaderThread | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._c2hcmd_task: asyncio.Task[None] | None = None
        self._io_lock = asyncio.Lock()
        self._tx_sequence = 0
        self._started = False

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RTL8192FUDriver":
        return cls(dev, id_entry)

    def register_rx_callback(self, cb: Callable) -> None:
        self._rx_callback = cb

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        self._disconnect_callback = cb

    def _on_rx_fatal(self, error: Exception) -> None:
        self._device_gone = is_device_gone(error)
        if self._disconnect_callback is not None:
            self._disconnect_callback(error)

    def _probe(self, progress: Callable[[float, str], None]) -> None:
        self.transport.rtl8xxxu_parse_usb(self.priv)
        rtl8xxxu_probe(self.priv, files(__package__).joinpath("assets/rtl8192fufw.bin").read_bytes(), progress)
        self.mac_address = self.priv.mac_addr.hex(":")

    async def connect(self, progress_cb: ProgressCallback | None = None) -> bool:
        loop = asyncio.get_running_loop()
        progress = progress_cb or (lambda _value, _message: None)
        try:
            progress(0.00, "Claiming USB interface")
            await loop.run_in_executor(None, self.transport.claim)
            def from_worker(value: float, message: str) -> None:
                loop.call_soon_threadsafe(progress, value, message)

            await loop.run_in_executor(None, self._probe, from_worker)
            progress(0.90, "Starting monitor interface")
            self._rx_reader = RxReaderThread(
                loop, self._read_once, self._dispatch, name="rtl8192fu-rx", on_fatal=self._on_rx_fatal,
            )
            await loop.run_in_executor(None, start_monitor_interface, self.priv)
            self._started = True
            self._rx_reader.start()
            self._watchdog_task = loop.create_task(self._watchdog())
            self.current_channel = 1
        except Exception as error:
            await self._stop_tasks()
            self.transport.release()
            if isinstance(error, BringUpError):
                raise
            raise BringUpError("RTL8192FU bring-up", str(error)) from error
        self.is_warm = True
        progress(1.0, "RTL8192FU online")
        return True

    async def _watchdog(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(WATCHDOG_SECONDS)
            async with self._io_lock:
                await loop.run_in_executor(None, rtl8xxxu_watchdog_callback, self.priv)

    async def _stop_tasks(self) -> None:
        for task in (self._watchdog_task, self._c2hcmd_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._watchdog_task = None
        self._c2hcmd_task = None
        if self._rx_reader is not None:
            await self._rx_reader.stop()
            self._rx_reader = None

    def _read_once(self) -> bytes | None:
        if self.priv.pipe_in == 0:
            return None
        return read_burst(self.dev, self.priv.pipe_in)

    def _dispatch(self, buffer: bytes) -> None:
        for frame, signal in rtl8xxxu_parse_rxdesc24(self.priv, buffer):
            if len(frame) >= 10 and frame[0] == 0xD4:
                self.record_ack(frame)
            if self._rx_callback is None:
                continue
            packet = WlanFrameParser.parse_80211_frame(frame, RSSI_UNKNOWN if signal is None else signal)
            if packet is not None:
                self._rx_callback(packet)
        if self.priv.c2hcmd_queue and (self._c2hcmd_task is None or self._c2hcmd_task.done()):
            self._c2hcmd_task = asyncio.get_running_loop().create_task(self._c2hcmd_work())

    async def _c2hcmd_work(self) -> None:
        """priv->c2hcmd_work (core.c:6162 schedule_work after queuing a C2H)."""
        async with self._io_lock:
            await asyncio.get_running_loop().run_in_executor(None, rtl8xxxu_c2hcmd_callback, self.priv)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        if channel not in self.SUPPORTED_CHANNELS:
            raise ValueError(f"unsupported RTL8192FU channel: {channel}")
        if not self._started:
            raise BringUpError("RTL8192FU channel", "driver is not connected")
        if channel == self.current_channel:
            return True
        async with self._io_lock:
            await asyncio.get_running_loop().run_in_executor(None, rtl8xxxu_config, self.priv, channel)
            self.current_channel = channel
        return True

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        if not self._started:
            raise BringUpError("RTL8192FU TX", "driver is not connected")
        endpoint, urb = rtl8xxxu_tx(self.priv, frame_bytes)
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.transport.bulk_write, endpoint, urb)
        except Exception as error:
            if is_device_gone(error):
                self._on_rx_fatal(error)
            raise
        return True

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        if len(frame_bytes) < 24:
            return frame_bytes
        stamped = bytearray(frame_bytes)
        stamped[22:24] = ((self._tx_sequence << 4) | (stamped[22] & 0x0F)).to_bytes(2, "little")
        self._tx_sequence = (self._tx_sequence + 1) & 0xFFF
        return bytes(stamped)

    async def _enable_rx_acks(self) -> None:
        """No-op: MONITOR_FILTER_FLAGS carries FIF_CONTROL, so RCR_ACCEPT_CTRL_FRAME is already set."""
        return

    async def _disable_rx_acks(self) -> None:
        """No-op, matching ``_enable_rx_acks``."""
        return

    def _program_port0(self, linktype: Nl80211Iftype, mac: bytes, bssid: bytes) -> None:
        self.priv.vif_type = linktype
        self.priv.vif_addr = mac
        rtl8xxxu_set_linktype(self.priv, linktype, 0)
        rtl8xxxu_set_mac(self.priv, 0)
        rtl8xxxu_set_bssid(self.priv, bssid, 0)

    async def enter_active_monitor(self, mac: bytes, bssid: bytes | None = None) -> bytes:
        """Port 0 as STATION (or AP when ``bssid`` is ``mac``): set_linktype + set_mac + set_bssid."""
        if len(mac) != 6 or (bssid is not None and len(bssid) != 6):
            raise ValueError("RTL8192FU active-monitor addresses must be six bytes")
        bssid = bytes(bssid or mac)
        linktype = Nl80211Iftype.NL80211_IFTYPE_AP if bytes(mac) == bssid else Nl80211Iftype.NL80211_IFTYPE_STATION
        async with self._io_lock:
            await asyncio.get_running_loop().run_in_executor(None, self._program_port0, linktype, bytes(mac), bssid)
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        if not self._started:
            return
        async with self._io_lock:
            await asyncio.get_running_loop().run_in_executor(
                None, self._program_port0, Nl80211Iftype.NL80211_IFTYPE_UNSPECIFIED, self.priv.mac_addr, bytes(6),
            )
        self.priv.vif_type = None

    def _disconnect(self) -> None:
        """rtl8xxxu_disconnect order (core.c:7918-7922): deinit_led, stop, power_off."""
        rtl8xxxu_deinit_led(self.priv)
        rtl8xxxu_stop(self.priv)
        self.priv.fops.power_off(self.priv)

    async def close(self) -> None:
        await self._stop_tasks()
        try:
            if self._started and self.transport.interface_number is not None and not self._device_gone:
                async with self._io_lock:
                    await asyncio.get_running_loop().run_in_executor(None, self._disconnect)
        finally:
            try:
                self.transport.release()
            finally:
                self._started = False
                self.is_warm = False
                self._device_gone = False
