"""RTL8822CU USB monitor-mode receiver and management-frame injector."""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, ClassVar, Optional

import usb.core
import usb.util

from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.dot11.parser import WlanFrameParser
from wifit3.chips.rx_reader import RxReaderThread

from .chipid import ChipInfo, read_chip_info
from .constants import CHIP_ID_RTL8822CU
from .efuse import EfuseInfo, read_efuse
from .firmware import download_firmware, firmware_ready, load_firmware
from .mac import (
    cut_mask_from_sys_cfg1,
    enable_bb_rf,
    enter_monitor_mode,
    init_rx_mac,
    mac_power_on,
    set_mac_addr,
)
from .phy import initialize_phy, set_channel_20mhz
from .rx import iter_bulk_frames, read_rx_burst
from .transport import EndpointLayout, RTL8822CUTransport
from .tx import TX_DESC_QSEL_MGMT, build_tx_desc_mgmt, pick_bulk_out_ep, write_bulk

logger = logging.getLogger(__name__)


class RTL8822CUDriver(Driver):
    SUPPORTED_CHANNELS: ClassVar[list[int]] = list(range(1, 14)) + [36, 40, 44, 48, 149, 153, 157, 161, 165]
    FAKE_MAC = FakeMacSupport.SPOOFABLE

    def __init__(self, dev: usb.core.Device):
        super().__init__()
        self.dev = dev
        self.transport = RTL8822CUTransport(dev)
        self.mac_address: Optional[str] = None
        self.is_warm = False
        self.layout: Optional[EndpointLayout] = None
        self.chip_id: Optional[int] = None
        self.chip_info: Optional[ChipInfo] = None
        self.efuse: Optional[EfuseInfo] = None
        self._claimed = False
        self._rx_callback: Optional[Callable] = None
        self._on_lost: Optional[Callable] = None
        self._rx_reader: Optional[RxReaderThread] = None
        self._bulk_in_ep: Optional[int] = None
        self._bulk_out_eps: list[int] = []
        self.current_band_is_2g = True
        self._current_channel = 1

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RTL8822CUDriver":
        return cls(dev)

    def register_rx_callback(self, cb: Callable) -> None:
        self._rx_callback = cb

    def register_disconnect_callback(self, cb: Callable) -> None:
        self._on_lost = cb

    def _claim(self) -> None:
        if self._claimed:
            return
        try:
            self.dev.set_configuration()
        except usb.core.USBError:
            pass
        usb.util.claim_interface(self.dev, self.layout.interface if self.layout else 0)
        self._claimed = True

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        if progress_cb:
            progress_cb(0.1, "Inspecting RTL8822CU USB endpoints")
        self.layout = self.transport.endpoints()
        self._claim()
        self.chip_info = read_chip_info(self.transport)
        self.chip_id = self.chip_info.chip_id
        if self.chip_id != CHIP_ID_RTL8822CU:
            raise BringUpError("chip-id", f"expected 0x{CHIP_ID_RTL8822CU:02x}, got 0x{self.chip_id:02x}")
        if progress_cb:
            progress_cb(0.5, "Reading RTL8822CU EFUSE")
        self.efuse = read_efuse(self.transport)
        if self.efuse.map_valid:
            self.mac_address = ":".join(f"{octet:02x}" for octet in self.efuse.mac_address)
        if not self.layout.bulk_out:
            raise BringUpError("endpoints", "RTL8822CU has no bulk OUT endpoint for firmware upload")
        loop = asyncio.get_running_loop()
        try:
            if progress_cb:
                progress_cb(0.6, "Powering on RTL8822CU MAC")
            cut_mask = cut_mask_from_sys_cfg1(self.chip_info.raw_cfg1)
            await loop.run_in_executor(None, lambda: mac_power_on(self.transport, cut_mask=cut_mask))
            if progress_cb:
                progress_cb(0.7, "Uploading RTL8822C firmware")
            image = load_firmware()
            await loop.run_in_executor(
                None, lambda: download_firmware(self.dev, self.transport, self.layout.bulk_out[0], image)
            )
            if progress_cb:
                progress_cb(0.95, "Waiting for RTL8822C firmware")
            await loop.run_in_executor(None, firmware_ready, self.transport)
            if progress_cb:
                progress_cb(0.96, "Loading RTL8822C BB/RF tables")
            rfe_type = self.efuse.rfe_type
            await loop.run_in_executor(None, enable_bb_rf, self.transport)
            await loop.run_in_executor(
                None, lambda: initialize_phy(self.transport, cut=self.chip_info.cut, rfe_type=rfe_type)
            )
            if progress_cb:
                progress_cb(0.98, "Configuring RTL8822C monitor RX")
            await loop.run_in_executor(None, init_rx_mac, self.transport)
            await loop.run_in_executor(None, enter_monitor_mode, self.transport)
            await loop.run_in_executor(None, set_channel_20mhz, self.transport, 1)
        except (IOError, usb.core.USBError) as exc:
            raise BringUpError("firmware", str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise BringUpError("phy", str(exc)) from exc
        if not self.layout.bulk_in:
            raise BringUpError("endpoints", "RTL8822CU has no bulk IN endpoint for RX")
        self._bulk_in_ep = self.layout.bulk_in[0]
        self._bulk_out_eps = list(self.layout.bulk_out)
        self._rx_reader = RxReaderThread(
            loop, self._rx_read_once, self._rx_dispatch, name="rtl8822cu-rx",
            on_fatal=lambda exc: self._on_lost and self._on_lost(exc),
        )
        self._rx_reader.start()
        self.is_warm = True
        if progress_cb:
            progress_cb(1.0, "RTL8822CU monitor receiver online")
        return True

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        try:
            if scan:
                # Fast hop: the vendor's runtime ``switch_channel_bw`` sequence only. Replaying the
                # whole AGC/BB/RF init tables on every hop is what forced the 0.75 s dwell; the
                # hopper now gets the cheap path and can dwell at the normal cadence.
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: set_channel_20mhz(self.transport, channel))
            else:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._retune_channel, channel
                )
            self._current_channel = channel
            self.current_band_is_2g = channel <= 14
            return True
        except (IOError, ValueError, usb.core.USBError) as exc:
            logger.warning("RTL8822CU channel %d failed: %s", channel, exc)
            return False

    def _retune_channel(self, channel: int) -> None:
        """Full lock-time retune: restore the complete RTL8822C PHY state.

        ``set_channel(scan=False)`` (campaign focus/PBC lock) replays the band-specific
        BB/RF tables before the runtime switch. The vendor driver reapplies them when
        crossing channels, and a bare RF18 write leaves stale AGC/RXBB state on this
        USB adapter; experimentally that locks RX to the boot channel only. The table
        replay is deterministic and is the same sequence used at bring-up. The scanner's
        transient hops take ``set_channel_20mhz`` directly instead (see ``set_channel``).
        """
        if self.chip_info is None or self.efuse is None:
            raise RuntimeError("RTL8822CU PHY metadata is unavailable")
        initialize_phy(
            self.transport,
            cut=self.chip_info.cut,
            rfe_type=self.efuse.rfe_type,
        )
        set_channel_20mhz(self.transport, channel)

    async def close(self) -> None:
        if self._rx_reader:
            await self._rx_reader.stop()
            self._rx_reader = None
        if self._claimed:
            try:
                usb.util.release_interface(self.dev, self.layout.interface if self.layout else 0)
            except usb.core.USBError:
                pass
            usb.util.dispose_resources(self.dev)
            self._claimed = False

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Point REG_MACID at ``mac`` so the hardware HW-ACKs frames addressed to it while
        staying in monitor mode — the accept-all monitor RCR (AAP) still HW-ACKs
        RA == REG_MACID, so no RCR flip is needed. MAC-only, mirroring the proven Realtek
        siblings. Reversed by ``exit_active_monitor``. ``bssid`` is unused (register-MAC
        ACK is a pure RA-match)."""
        await self._write_mac(bytes(mac))
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the card's real EFUSE MAC in REG_MACID (stop ACKing the forged MAC)."""
        if self.mac_address:
            await self._write_mac(bytes(int(x, 16) for x in self.mac_address.split(":")))

    async def _write_mac(self, mac6: bytes) -> None:
        """Program ``mac6`` into REG_MACID, offloaded so the blocking control transfer
        never stalls the RX dispatch."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: set_mac_addr(self.transport, mac6))

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        if not self._bulk_out_eps:
            return False
        try:
            desc = build_tx_desc_mgmt(frame_bytes, band_is_2g=self.current_band_is_2g)
            ep = pick_bulk_out_ep(self._bulk_out_eps, queue=TX_DESC_QSEL_MGMT)
            loop = asyncio.get_running_loop()
            sent = await loop.run_in_executor(None, lambda: write_bulk(self.dev, ep, desc + frame_bytes))
            return sent == len(desc) + len(frame_bytes)
        except (ValueError, IOError, usb.core.USBError) as exc:
            logger.warning("RTL8822CU TX failed: %s", exc)
            return False

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        return frame_bytes

    async def _enable_rx_acks(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self.transport.write16(0x06A2, self.transport.read16(0x06A2) | (1 << 13)))

    async def _disable_rx_acks(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self.transport.write16(0x06A2, self.transport.read16(0x06A2) & ~(1 << 13)))

    def _rx_read_once(self) -> bytes | None:
        if self._bulk_in_ep is None:
            return None
        return read_rx_burst(self.dev, self._bulk_in_ep, max_size=16384, timeout_ms=100)

    def _rx_dispatch(self, buf: bytes) -> None:
        callback = self._rx_callback
        if callback is None and not self._ack_detect_on:
            return
        for _stat, mpdu, rssi in iter_bulk_frames(buf):
            if len(mpdu) == 10 and mpdu[0] == 0xD4:
                self.record_ack(mpdu)
                continue
            if callback is None:
                continue
            parsed = WlanFrameParser.parse_80211_frame(mpdu, rssi if rssi is not None else -100)
            if parsed:
                callback(parsed)
