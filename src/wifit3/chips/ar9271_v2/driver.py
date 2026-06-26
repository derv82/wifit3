"""AR9271 (ath9k_htc) driver — clean-room v2 re-port from the v6.18.12 kernel source.

A fresh bring-up against the mainline ``htc_9271-1.4.0.fw`` protocol, verified op-by-op
against the cold-boot pcap (``scripts/ar9271_v2/verify_pcap.py``).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, ClassVar, List, Optional

import libusb_package
import usb.core

from wifit3.engine.protocols import DeviceID, FakeMacSupport, ProgressCallback

from . import constants as C, firmware, htc, tx
from .transport import AR9271Transport

logger = logging.getLogger(__name__)


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
        self.htc: Optional[htc.HTCState] = None
        self._rx_callback: Optional[Callable[[dict], None]] = None

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "AR9271V2Driver":
        return cls(dev)

    @classmethod
    def for_replay(cls, wmi, hw, endpoints: dict) -> "AR9271V2Driver":
        """Build a driver around an already-brought-up ``(wmi, hw)`` so the verify gate can drive
        its public TX/channel methods against the replayed transport. (The production path is
        ``connect()``; this is the seam where the gate's per-op scaffold converges onto real
        driver methods — see ``scripts/ar9271_v2/verify_pcap.py``.) ``inject_frame`` sends on the
        WLAN_TX pipe via ``self.wmi.t`` (the transport the gate rebinds per op). ``endpoints`` is
        the HTC service->endpoint map from the handshake (``HTCState.endpoints``)."""
        self = cls.__new__(cls)
        self.wmi = wmi
        self.hw = hw
        self.transport = wmi.t
        self._rx_callback = None
        self._init_tx(endpoints)
        return self

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

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        def _p(pct: float, msg: str) -> None:
            if progress_cb:
                progress_cb(pct, msg)
            logger.info("ar9271_v2 %d%%: %s", int(pct * 100), msg)

        _p(0.05, "Downloading AR9271 firmware...")
        fw = firmware.load_firmware_blob()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, firmware.download, self.transport, fw)

        _p(0.30, "Waiting for AR9271 to re-enumerate...")
        warm = await self._await_reenumeration()
        if warm is None:
            logger.error("ar9271_v2: card did not re-enumerate after firmware download")
            return False
        self.transport = AR9271Transport(warm)

        _p(0.45, "HTC/WMI handshake...")
        self.htc = await loop.run_in_executor(None, htc.handshake, self.transport)

        # M2b+: WMI register init (ath9k_hw), calibration, monitor RX filter. Not yet ported
        # — fail loudly rather than report a half-initialised card as ready.
        raise NotImplementedError("ar9271_v2: M2b (WMI register init) not yet ported")

    async def _await_reenumeration(self) -> Optional[usb.core.Device]:
        backend = libusb_package.get_libusb1_backend()
        for _ in range(12):                 # ~3 s; the chip boots its text image and re-attaches
            await asyncio.sleep(0.25)
            dev = usb.core.find(idVendor=C.AR9271_VID, idProduct=C.AR9271_PID, backend=backend)
            if dev is not None:
                return dev
        return None

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        raise NotImplementedError("ar9271_v2: set_channel lands with M3")

    def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> int:
        """Transmit one 802.11 frame (aireplay-ng ``--test`` / deauth): allocate a TX slot, build
        the HTC wrapper (tx_frame_hdr for data, tx_mgmt_hdr otherwise; see ``tx.py``), and bulk-OUT
        it on the WLAN_TX pipe. Mirrors ath9k_htc_tx -> ath9k_htc_tx_start [SRC] htc_drv_main.c:862
        / htc_drv_txrx.c:340. The 802.11 frame (incl. its sequence number) is the caller's; only
        the wrapper and the cookie are ours. Returns the allocated cookie (TX slot).

        The verify gate drives this against the recorded frames; the agent wires TX, live firing
        stays the user's gate. (Sync for now so the gate drives it directly; the async WlanDriver
        wrapper lands with the UI wiring, alongside ``connect()``.)"""
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
        try:
            usb.util.dispose_resources(self.transport.dev)
        except Exception:
            pass
