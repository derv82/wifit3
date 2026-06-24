"""RTL8821CU / 1T1R 802.11ac — vendor (HALMAC/PHYDM) cleanroom DKMS port.

The byte-for-byte gate (``scripts/rtl8821cu_dkms/verify_pcap.py``) drives this driver's public
interface — ``connect`` (cold init + airmon monitor entry), ``set_channel`` (the airodump hops)
and ``inject_frame`` (the aireplay-ng test + deauth TX) — and reproduces the **entire** cold-boot
capture (all 21409 ctrl + bulk-OUT ops) byte-for-byte, so what the gate verifies is exactly the
product code path. The chip→host interrupt-IN (C2H) and bulk-IN (RX) streams are a separate blind
spot the host-side replay does not model — see RTL8821CU_DKMS.md.

Registered in ``wlan/manager.py``. ``connect`` claims the combo card's WiFi (vendor-class)
interface, starts the bulk-IN ``RxReaderThread``, runs ``bringup.cold_bringup`` (FW download +
MAC/BB/RF + BT-coex + the ch1 monitor tune over the ep-0x05 FW/TX pipe — which leaves the chip in
the vendor's exact receiving config, byte-for-byte), then runs the phydm watchdog on a background
task. ``set_channel`` and ``inject_frame`` drive the phydm tune and the TX-descriptor path. The
whole cold-boot pcap is reproduced byte-for-byte by ``scripts/rtl8821cu_dkms/verify_pcap.py``, and
cold init is HW-validated (FW boots). Hardware status — including the open monitor-RX demod fault
(frames received, ~99% fail CRC) — is tracked in RTL8821CU_DKMS.md. Warm reattach and the ZeroCD
mode-switch discovery blocker are open. Shares no code with the other Realtek drivers (anti-DRY).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, ClassVar, List, Optional

import usb.core
import usb.util

from wifit3.chips.rx_reader import RxReaderThread
from wifit3.engine.protocols import DeviceID, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.wlan.packet import WlanFrameParser

from . import bringup, chan, efuse, tx, watchdog
from .constants import USB_PID_8821CU, USB_VID_REALTEK
from .rx import iter_frames
from .transport import Rtl8821cuTransport

logger = logging.getLogger(__name__)

CHANNELS_2G = list(range(1, 14))
# Non-DFS 5 GHz only for now; the capture also tunes DFS 52..144 but set_channel
# (and the DFS tune path) is a later milestone — see RTL8821CU_DKMS.md.
CHANNELS_5G = [36, 40, 44, 48, 149, 153, 157, 161, 165]

# Monitor-mode management-inject TX-descriptor attributes. [WIRE] every aireplay-ng frame in the
# capture (probe-req / RTS / auth / deauth) shares macid 1, QSEL_MGNT, raid 1, 1M CCK, retry off;
# only TXPKTSIZE + BMC (from addr1) + the XOR checksum vary, all derived from the 802.11 frame.
_QSEL_MGNT = 0x12              # [SRC] halmac_type.h HALMAC_TXDESC_QSEL_MGNT
_RAID_INJECT = 1              # [WIRE] aireplay tx-desc dw1[20:16]

_WIFI_INTF_CLASS = 0xFF        # combo card: the WiFi function is the vendor-specific interface
                               # (class 0xFF, #2); the Bluetooth interfaces 0/1 are class 0xE0

_WATCHDOG_PERIOD_S = 2.0        # phydm dynamic-check cadence [SRC] rtw_cmd.c rtw_dynamic_chk_wk


class Rtl8821cuDkmsDriver:
    SUPPORTED_IDS: ClassVar[List[DeviceID]] = [
        DeviceID(USB_VID_REALTEK, USB_PID_8821CU, "Realtek RTL8821CU 802.11ac (8821cu_dkms)"),
    ]
    SUPPORTED_CHANNELS: ClassVar[List[int]] = CHANNELS_2G + CHANNELS_5G
    FAKE_MAC: ClassVar[FakeMacSupport] = FakeMacSupport.UNIMPLEMENTED

    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        # FW download + TX bulk-OUT is on ep 0x05, not the transport's 0x04 default [WIRE]
        # coverage audit. (The offline gate's ReplayDevice ignores the endpoint, so this only
        # matters on real silicon — without it the FW download writes to the wrong pipe.)
        self.transport = Rtl8821cuTransport(dev, bulk_out_ep=0x05)
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self.info = None                # EfuseInfo from cold_bringup; set_channel/inject need it
        self._rx_cb: Optional[Callable[[dict], None]] = None
        self._reader: Optional[RxReaderThread] = None
        self._wifi_intf: Optional[int] = None       # claimed vendor (WiFi) interface number
        self._io_lock = asyncio.Lock()              # serialize watchdog tick vs set_channel
        self._wd_state = None
        self._watchdog_task = None

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "Rtl8821cuDkmsDriver":
        return cls(dev)

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_cb = cb

    def _claim(self) -> None:
        """Combo card: set the configuration and claim the vendor-specific (class 0xFF) WiFi
        interface — NOT the Bluetooth interfaces (class 0xE0). The manager is chipset-agnostic and
        does not claim, so the driver must (mirrors test_hw's manual claim). No-op once claimed."""
        if self._wifi_intf is not None:
            return
        try:
            self.dev.set_configuration()
        except usb.core.USBError as e:
            logger.debug("set_configuration: %s", e)
        intf_num = next((i.bInterfaceNumber for i in self.dev.get_active_configuration()
                         if i.bInterfaceClass == _WIFI_INTF_CLASS), None)
        if intf_num is None:
            raise BringUpError("RTL8821CU: no vendor-specific WiFi interface — the combo card is "
                               "likely still in ZeroCD (CD-ROM) mode; mode-switch it first.")
        try:
            if self.dev.is_kernel_driver_active(intf_num):
                self.dev.detach_kernel_driver(intf_num)
        except (NotImplementedError, usb.core.USBError) as e:
            logger.debug("kernel-driver detach skipped: %s", e)
        usb.util.claim_interface(self.dev, intf_num)
        self._wifi_intf = intf_num

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """Cold bring-up + airmon monitor entry (``bringup.cold_bringup``), caching the decoded
        EFUSE/board info that ``set_channel`` and the watchdog/coex producers key on. The cold path
        is reproduced byte-for-byte by ``scripts/rtl8821cu_dkms/verify_pcap.py`` — which drives this
        method synchronously with NO running loop, so that path skips the RX reader (host->chip
        only). Under a real event loop the bulk-IN RX reader starts FIRST: the monitor RX-enable
        lives inside ``cold_bringup``, and the kernel posts RX URBs before that gate — a reader
        started after it leaves the RX-DMA stalled (the chip's RX-starvation history)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.info = bringup.cold_bringup(self.transport)
            return True
        self._claim()
        self._reader = RxReaderThread(loop, self._read_once, self._dispatch, name="8821cu-dkms-rx")
        self._reader.start()
        self.info = await loop.run_in_executor(None, bringup.cold_bringup, self.transport)
        # phydm dynamic-check watchdog (kernel-parity — its ~2 s ticks are in the pcap). DIG runs
        # here; without it the RX AGC sits at full gain and the OFDM false-alarm count floods.
        self._wd_state = watchdog.WatchdogState(
            eeprom_thermal=self.info.eeprom_thermal, thermal_offset=efuse.thermal_offset(self.info))
        self._watchdog_task = loop.create_task(self._watchdog_loop())
        if progress_cb:
            progress_cb(1.0, "RTL8821CU monitor up (ch 1 @ 20 MHz)")
        return True

    async def _watchdog_loop(self) -> None:
        """Run the phydm dynamic-check tick at the kernel ~2 s cadence (DIG / CCK-PD / RX-agg /
        thermal / env-monitor) — the runtime maintenance the kernel does and the cold path does not.
        Serialized with set_channel via _io_lock so two control-transfer sequences never interleave;
        the blocking tick is offloaded so it never stalls the RX dispatch on the event loop."""
        loop = asyncio.get_running_loop()
        try:
            while True:
                await asyncio.sleep(_WATCHDOG_PERIOD_S)
                async with self._io_lock:
                    await loop.run_in_executor(None, watchdog.tick, self.transport, self._wd_state)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — a watchdog fault must not kill RX
            logger.exception("RTL8821CU watchdog stopped on error")

    def _read_once(self) -> Optional[bytes]:
        """Reader-thread side: one blocking bulk-IN read (None on no traffic)."""
        return self.transport.bulk_in()

    def _dispatch(self, buf: bytes) -> None:
        """Loop side: split the aggregated bulk-IN buffer into (frame, rssi) pairs (FCS already
        stripped) and fan each parsed dict to the rx callback."""
        cb = self._rx_cb
        if cb is None:
            return
        for frame, rssi in iter_frames(buf):
            parsed = WlanFrameParser.parse_80211_frame(frame, rssi)
            if parsed is not None:
                cb(parsed)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to ``channel`` via the phydm band/channel/bandwidth set (``chan.set_channel``,
        20 MHz). Requires a prior ``connect`` (needs the cached ``info``). Under a real loop the
        tune is serialized with the watchdog tick (``_io_lock``) and offloaded; the offline gate
        drives it synchronously (no running loop)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            chan.set_channel(self.transport, self.info, channel)
            return True
        async with self._io_lock:
            await loop.run_in_executor(None, chan.set_channel, self.transport, self.info, channel)
        return True

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        """Build the management TX descriptor for ``frame_bytes`` and bulk-OUT [desc][frame].
        BMC is derived from the frame's addr1; ``use_no_ack`` (single-shot, no retry) is the
        injection default the aireplay-ng capture uses. The descriptor builder is byte-verified
        against that capture by the gate's inject branch."""
        pkt = tx.build_mgnt_txdesc(frame_bytes, qsel=_QSEL_MGNT, raid=_RAID_INJECT,
                                   retry_ctrl=not use_no_ack)
        self.transport.bulk_out(pkt)
        return True

    async def close(self) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        if self._reader is not None:
            await self._reader.stop()       # join the reader BEFORE releasing the USB handle
            self._reader = None
        self.transport.close()
