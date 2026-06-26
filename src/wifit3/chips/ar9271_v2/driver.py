"""AR9271 (ath9k_htc) driver — clean-room v2 re-port from the v6.18.12 kernel source.

A fresh bring-up against the mainline ``htc_9271-1.4.0.fw`` protocol, verified op-by-op against
the cold-boot pcap (``scripts/ar9271_v2/verify_pcap.py``). The cold init + channel-hop sequencing
lives in ``bringup.py``; ``connect`` / ``set_channel`` drive it, and the same methods are what the
verify gate replays — so the bytes the gate checks are exactly the product's.

Live vs. gate: ``connect`` / ``set_channel`` detect a running asyncio loop. With one (the app),
they offload the blocking USB work to an executor and run the bulk-IN ``RxReaderThread``; with none
(the synchronous pcap gate over a ReplayDevice), they take the inline path. ``inject_frame`` stays
synchronous (the gate + unit tests drive it directly); the live-TX async wrapper lands with the
UI's TX wiring — live firing is the user's gate, not the agent's.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, ClassVar, List, Optional

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rx_reader import RxReaderThread
from wifit3.engine.protocols import DeviceID, FakeMacSupport, ProgressCallback
from wifit3.wlan.packet import WlanFrameParser

from . import bringup, chan as chanmod, constants as C, firmware, rx_decode, tx
from .transport import AR9271Transport

logger = logging.getLogger(__name__)

_RX_BUF_SIZE = 16384          # [SRC] hif_usb.h:60 MAX_RX_BUF_SIZE — one bulk-IN read


class AR9271V2Driver:
    """Atheros AR9271 / ALFA AWUS036NHA — 2.4 GHz, soft-MAC, ath9k_htc firmware."""

    SUPPORTED_IDS: ClassVar[List[DeviceID]] = [
        DeviceID(C.AR9271_VID, C.AR9271_PID, "Atheros AR9271 / ALFA AWUS036NHA (v2)"),
    ]
    SUPPORTED_CHANNELS: ClassVar[List[int]] = list(range(1, 14))   # 2.4 GHz only, no 5 GHz radio
    FAKE_MAC: ClassVar[FakeMacSupport] = FakeMacSupport.UNIMPLEMENTED

    def __init__(self, dev: usb.core.Device):
        self.transport = AR9271Transport(dev)
        self.is_warm = False
        self.mac_address: Optional[str] = None
        self.wmi = None                                  # WMI channel, set by cold_bringup
        self.hw = None                                   # AthHw, set by cold_bringup
        self.endpoints: dict = {}                        # HTC service -> endpoint id
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._reader: Optional[RxReaderThread] = None

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "AR9271V2Driver":
        return cls(dev)

    @classmethod
    def for_replay(cls, wmi, hw, endpoints: dict) -> "AR9271V2Driver":
        """Build a driver around an already-brought-up ``(wmi, hw)`` for the TX unit tests
        (``tests/chips/ar9271_v2/test_tx.py``), which exercise ``inject_frame`` in isolation
        without a full ``connect``. The production + gate path is ``connect`` -> ``cold_bringup``,
        which adopts the same state via ``_adopt``."""
        self = cls.__new__(cls)
        self.wmi = wmi
        self.hw = hw
        self.transport = wmi.t
        self.endpoints = endpoints
        self._rx_callback = None
        self._reader = None
        self._init_tx(endpoints)
        return self

    def _adopt(self, res: "bringup.BringupResult") -> None:
        """Take ownership of the state cold_bringup produced, and arm the TX path."""
        self.wmi = res.wmi
        self.hw = res.hw
        self.transport = res.wmi.t
        self.endpoints = res.endpoints
        self.mac_address = ":".join(f"{b:02x}" for b in res.hw.macaddr)
        self._init_tx(res.endpoints)

    def _init_tx(self, endpoints: dict) -> None:
        """Resolve the TX service endpoints from the HTC handshake map and arm the slot bitmap.
        The endpoint ids are assigned by connect_service (don't hardcode 5/6) — mgmt frames ride
        WMI_MGMT_SVC, data frames the BE service (get_htc_epid's default AC) [SRC] htc_drv_txrx.c
        :102; injected monitor frames carry no QoS, so they all map to BE."""
        self.mgmt_epid = endpoints[C.WMI_MGMT_SVC]
        self.data_be_epid = endpoints[C.WMI_DATA_BE_SVC]
        self.tx_slots = tx.TxSlots()

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_callback = cb

    # ---- bring-up ---------------------------------------------------------
    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """Download firmware, then run the cold bring-up to a monitor receiver on ch1.

        With no running loop (the synchronous pcap gate), download + bring-up run inline over the
        ReplayDevice transport. Under the app's loop, the blocking USB work is offloaded to an
        executor and the bulk-IN ``RxReaderThread`` is started BEFORE the bring-up's RX-enable —
        the cold pipe wedges if the reader starts after RX is turned on [[rx_reader_thread]]."""
        def _p(pct: float, msg: str) -> None:
            if progress_cb:
                progress_cb(pct, msg)
            logger.info("ar9271_v2 %d%%: %s", int(pct * 100), msg)

        fw = firmware.load_firmware_blob()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Gate path: one transport throughout, no re-enumeration, no RX reader.
            firmware.download(self.transport, fw)
            self._adopt(bringup.cold_bringup(self.transport))
            return True

        _p(0.05, "Downloading AR9271 firmware...")
        await loop.run_in_executor(None, firmware.download, self.transport, fw)

        _p(0.30, "Waiting for AR9271 to re-enumerate...")
        warm = await self._await_reenumeration()
        if warm is None:
            logger.error("ar9271_v2: card did not re-enumerate after firmware download")
            return False
        self.transport = AR9271Transport(warm)

        _p(0.45, "Starting RX reader + HTC/WMI init...")
        self._reader = RxReaderThread(loop, self._read_once, self._dispatch, name="ar9271v2-rx")
        self._reader.start()
        res = await loop.run_in_executor(None, bringup.cold_bringup, self.transport)
        self._adopt(res)
        _p(1.0, f"AR9271 monitor up (ch 1, {self.mac_address})")
        return True

    async def _await_reenumeration(self) -> Optional[usb.core.Device]:
        backend = libusb_package.get_libusb1_backend()
        for _ in range(12):                 # ~3 s; the chip boots its text image and re-attaches
            await asyncio.sleep(0.25)
            dev = usb.core.find(idVendor=C.AR9271_VID, idProduct=C.AR9271_PID, backend=backend)
            if dev is not None:
                return dev
        return None

    # ---- channel ----------------------------------------------------------
    async def set_channel(self, channel: int, scan: bool = False, *, _fastcc: bool = False) -> bool:
        """Tune to ``channel`` via a full ath9k_hw_reset (the always-correct retune). ``_fastcc``
        selects the kernel's within-band fast channel change — used only by the verify gate, which
        reads the full-vs-fast decision off the wire; the live hopper always takes the full reset
        (simple + safe). No running loop -> inline (the gate); otherwise offloaded."""
        ch = chanmod.channel_2ghz(channel)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._do_channel_change(ch, _fastcc)
            return True
        await loop.run_in_executor(None, self._do_channel_change, ch, _fastcc)
        return True

    def _do_channel_change(self, ch: chanmod.Channel, fastcc: bool) -> None:
        if fastcc:
            bringup.fast_channel_change(self.wmi, self.hw, ch)
        else:
            bringup.full_channel_change(self.wmi, self.hw, ch)

    # ---- RX (live only; the pcap gate does not model device->host frames) -
    def _read_once(self) -> Optional[bytes]:
        """Reader-thread side: one blocking bulk-IN read on WLAN_RX (None on a benign timeout)."""
        try:
            return self.transport.wlan_in(_RX_BUF_SIZE)
        except usb.core.USBError as e:
            if e.errno == 110 or "timeout" in str(e).lower():   # ETIMEDOUT — no traffic
                return None
            raise

    def _dispatch(self, buf: bytes) -> None:
        """Loop side: split the bulk-IN transfer into (mpdu, rssi) pairs (FCS stripped) and fan
        each parsed dict to the rx callback."""
        cb = self._rx_callback
        if cb is None:
            return
        for frame, rssi in rx_decode.iter_frames(buf):
            parsed = WlanFrameParser.parse_80211_frame(frame, rssi)
            if parsed is not None:
                cb(parsed)

    # ---- TX (wired, not fired — live injection is the user's gate) ---------
    def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> int:
        """Transmit one 802.11 frame (aireplay-ng ``--test`` / deauth): allocate a TX slot, build
        the HTC wrapper (tx_frame_hdr for data, tx_mgmt_hdr otherwise; see ``tx.py``), and bulk-OUT
        it on the WLAN_TX pipe. Mirrors ath9k_htc_tx -> ath9k_htc_tx_start [SRC] htc_drv_main.c:862
        / htc_drv_txrx.c:340. The 802.11 frame (incl. its sequence number) is the caller's; only
        the wrapper and the cookie are ours. Returns the allocated cookie (TX slot).

        Synchronous: the verify gate + unit tests drive it directly. The async ``WlanDriver``
        wrapper that the UI's deauth path awaits lands with the live-TX wiring (the human gate)."""
        dot11 = bytes(frame_bytes)
        cookie = self.tx_slots.get()
        if tx.is_data_frame(dot11):
            frame = tx.build_data_tx(self.data_be_epid, dot11, cookie)
        else:
            frame = tx.build_mgmt_tx(self.mgmt_epid, dot11, cookie)
        self.wmi.t.wlan_out(frame)
        return cookie

    def tx_status_event(self, event_body: bytes) -> None:
        """ath9k_htc_txstatus [SRC] htc_drv_txrx.c:647 — a WMI_TXSTATUS event reports completed
        TX cookies; free each one's slot. In production this is dispatched from the WMI-event RX
        path; the verify gate feeds the recorded events, interleaved by capture order."""
        for cookie in tx.txstatus_cookies(event_body):
            self.tx_slots.clear(cookie)

    async def close(self) -> None:
        if self._reader is not None:
            await self._reader.stop()        # join the reader BEFORE releasing the USB handle
            self._reader = None
        try:
            usb.util.dispose_resources(self.transport.dev)
        except Exception:
            pass
