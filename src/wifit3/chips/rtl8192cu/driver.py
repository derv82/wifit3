from __future__ import annotations

import asyncio
from typing import Callable

import usb.core

from wifit3.chips.driver import Driver, FakeMacSupport, ProgressCallback
from wifit3.chips.rx_reader import RxReaderThread
from wifit3.dot11.parser import WlanFrameParser
from wifit3.errors import BringUpError, is_device_gone
from wifit3.models.device_id import DeviceID

from . import constants as C
from .base import rtl_watchdog_wq_callback
from .core import (
    Cfg80211ChanDef,
    ieee80211_channel_to_frequency,
    ieee80211_select_queue_80211,
    rtl_op_add_interface,
    rtl_op_config,
    rtl_op_configure_filter,
    rtl_op_remove_interface,
    rtl_op_stop,
    rtl_op_tx,
)
from .priv import Ieee80211TxInfo, RtlPriv
from .transport import RTL8192CUTransport
from .usb import (
    USB_DIR_IN,
    ProbeError,
    UsbEndpointDescriptor,
    _rtl_rx_completed,
    _rtl_rx_work,
    rtl_usb_probe,
    rtl_usb_start,
    rtl_usb_stop,
    rtl_usb_tx,
)

WATCHDOG_SECONDS = C.RTL_WATCH_DOG_TIME / 1000
MONITOR_FILTER_FLAGS = C.FIF_ALLMULTI | C.FIF_CONTROL | C.FIF_OTHER_BSS
"""What mac80211 hands configure_filter for a promiscuous monitor interface."""
FIRST_CHANNEL = 1
"""mac80211's default chandef at interface up: the first 2.4 GHz channel."""
INJECTED_FRAME_RATE_IDX = 0
"""mac80211's rate_control_send_low for a frame with no STA: rtl_ratetable_2g[0], 1 Mbps."""
RX_READ_TIMEOUT_MS = 100
TX_WRITE_TIMEOUT_MS = 1000
"""Kernel bulk URBs carry no timeout; PyUSB needs one."""


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
        self._disconnect_callback: Callable[[Exception], None] | None = None
        self._device_gone = False
        self._rx_reader: RxReaderThread | None = None
        self._io_lock = asyncio.Lock()
        self._watchdog_task: asyncio.Task[None] | None = None
        self._tx_sequence = 0
        self._started = False

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RTL8192CUDriver":
        return cls(dev, id_entry)

    def register_rx_callback(self, cb: Callable) -> None:
        self._rx_callback = cb

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        self._disconnect_callback = cb

    def _on_rx_fatal(self, error: Exception) -> None:
        self._device_gone = is_device_gone(error)
        if self._disconnect_callback is not None:
            self._disconnect_callback(error)

    def _rtl_usb_receive(self) -> int:
        """usb.c:641: submit the bulk-IN URBs, here the reader thread."""
        if self._rx_reader is not None:
            self._rx_reader.start()
        return 0

    def _usb_kill_anchored_urbs(self) -> None:
        if self._rx_reader is not None:
            self._rx_reader.pause()

    def _bringup(self) -> None:
        """rtl_usb_probe, then mac80211's monitor interface up: rtl_op_start's adapter_start,
        configure_filter, and the first config with the default channel."""
        try:
            rtl_usb_probe(self.priv, self._endpoints, self._is_high_speed)
        except ProbeError as error:
            raise BringUpError("RTL8192CU probe", str(error)) from error
        self.mac_address = self.priv.efuse.dev_addr.hex(":")
        err = rtl_usb_start(self.priv, self._rtl_usb_receive)
        if err:
            raise BringUpError("RTL8192CU hw_init", f"rtl92cu_hw_init failed ({err})")
        rtl_op_configure_filter(self.priv, MONITOR_FILTER_FLAGS, MONITOR_FILTER_FLAGS)
        self._tune(FIRST_CHANNEL)

    def _tune(self, channel: int) -> None:
        chandef = Cfg80211ChanDef(hw_value=channel, center_freq=ieee80211_channel_to_frequency(channel))
        self.priv.conf_center_freq = chandef.center_freq
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
            self._rx_reader = RxReaderThread(
                loop, self._read_once, self._dispatch, name="rtl8192cu-rx", on_fatal=self._on_rx_fatal,
            )
            progress(0.05, "Reading EFUSE, downloading firmware, initialising MAC/BB/RF")
            async with self._io_lock:
                await loop.run_in_executor(None, self._bringup)
        except Exception as error:
            if self._rx_reader is not None:
                await self._rx_reader.stop()
                self._rx_reader = None
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

    def _read_once(self) -> bytes | None:
        return self.transport.bulk_read(USB_DIR_IN | self.priv.usb.in_ep, self.priv.usb.rx_max_size,
                                        RX_READ_TIMEOUT_MS)

    def _dispatch(self, transfer: bytes) -> None:
        """_rtl_rx_completed, then the tasklet's _rtl_rx_work. mac80211 strips the FCS
        (RX_INCLUDES_FCS, base.c:375); so does this, before the frame leaves the driver."""
        skb = _rtl_rx_completed(self.priv, transfer)
        if skb is None:
            return
        received = _rtl_rx_work(self.priv, skb)
        if received is None:
            return
        mpdu, rx_status = received
        if len(mpdu) <= C.FCS_LEN:
            return
        frame = mpdu[:-C.FCS_LEN]
        if len(frame) >= 10 and frame[0] == 0xD4:
            self.record_ack(frame)
        if self._rx_callback is None:
            return
        packet = WlanFrameParser.parse_80211_frame(frame, rx_status.signal)
        if packet is not None:
            self._rx_callback(packet)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        if channel not in self.SUPPORTED_CHANNELS:
            raise ValueError(f"unsupported RTL8192CU channel: {channel}")
        if not self._started:
            raise BringUpError("RTL8192CU channel", "driver is not connected")
        async with self._io_lock:
            await asyncio.get_running_loop().run_in_executor(None, self._tune, channel)
        return True

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """rtl_op_tx for a monitor-injected frame: no STA, mac80211's queue pick, the lowest rate."""
        if not self._started:
            raise BringUpError("RTL8192CU TX", "driver is not connected")
        info = Ieee80211TxInfo(rate_idx=INJECTED_FRAME_RATE_IDX,
                               queue_mapping=ieee80211_select_queue_80211(frame_bytes))
        urb = rtl_op_tx(self.priv, frame_bytes, info, rtl_usb_tx)
        if urb is None:
            return False
        ep_num, payload = urb
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, self.transport.bulk_write, ep_num, payload, self.priv.usb.max_bulk_out_size,
                TX_WRITE_TIMEOUT_MS)
        except Exception as error:
            if is_device_gone(error):
                self._on_rx_fatal(error)
            raise
        return True

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        """HWSEQ_EN is set only under firmware-controlled LPS (trx.c:600), never on USB, and
        mac80211 assigns no sequence to monitor-injected frames: stamp it here."""
        if len(frame_bytes) < 24:
            return frame_bytes
        stamped = bytearray(frame_bytes)
        stamped[22:24] = ((self._tx_sequence << 4) | (stamped[22] & 0x0F)).to_bytes(2, "little")
        self._tx_sequence = (self._tx_sequence + 1) & 0xFFF
        return bytes(stamped)

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

    def _stop(self) -> None:
        rtl_op_stop(self.priv, lambda: rtl_usb_stop(self.priv, self._usb_kill_anchored_urbs))

    async def close(self) -> None:
        """rtl_op_stop: the watchdog is cancelled first (rtl_deinit_deferred_work), then
        rtl_usb_stop idles the reader and rtl92cu_card_disable powers the chip down."""
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None
        try:
            if self._started and not self._device_gone:
                async with self._io_lock:
                    await asyncio.get_running_loop().run_in_executor(None, self._stop)
        finally:
            if self._rx_reader is not None:
                await self._rx_reader.stop()
                self._rx_reader = None
            self.transport.release()
            self._started = False
            self.is_warm = False
            self._device_gone = False
