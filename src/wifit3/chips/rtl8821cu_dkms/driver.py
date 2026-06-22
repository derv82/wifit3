"""RTL8821CU / 1T1R 802.11ac — vendor (HALMAC/PHYDM) cleanroom DKMS port.

WORK IN PROGRESS — milestone 1. The byte-for-byte gate
(``scripts/rtl8821cu_dkms/verify_pcap.py``) drives ``bringup.cold_bringup``; so far the
USB transport (with the 8821c 0x4E0 ON-section mirror) and the HALMAC card-enable power
tables are ported and the gate confirms the mirror reproduces the wire. The gate's current
frontier is the chip-id / pre-init read block the vendor runs *before* power-on
(SYS_CFG1 0xF0 chip-ver/cut, the 0x0030 indirect read loop) — that is the next milestone.

Deliberately NOT registered in ``wlan/manager.py`` yet: a stub that claims 0bda:c820 and
then fails ``connect()`` is worse than leaving the ID unclaimed. Registration lands when the
port is functionally complete (init + monitor + RX). Until then it is exercised only through
verify_pcap (and later ``scripts/rtl8821cu_dkms/test_hw.py``). Shares no code with the other
Realtek drivers by design (anti-DRY).
"""
from __future__ import annotations

import logging
from typing import Callable, ClassVar, List, Optional

import usb.core

from wifit3.engine.protocols import DeviceID, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError

from . import bringup
from .constants import USB_PID_8821CU, USB_VID_REALTEK
from .transport import Rtl8821cuTransport

logger = logging.getLogger(__name__)

CHANNELS_2G = list(range(1, 14))
# Non-DFS 5 GHz only for now; the capture also tunes DFS 52..144 but set_channel
# (and the DFS tune path) is a later milestone — see RTL8821CU_DKMS.md.
CHANNELS_5G = [36, 40, 44, 48, 149, 153, 157, 161, 165]


class Rtl8821cuDkmsDriver:
    SUPPORTED_IDS: ClassVar[List[DeviceID]] = [
        DeviceID(USB_VID_REALTEK, USB_PID_8821CU, "Realtek RTL8821CU 802.11ac (8821cu_dkms)"),
    ]
    SUPPORTED_CHANNELS: ClassVar[List[int]] = CHANNELS_2G + CHANNELS_5G
    FAKE_MAC: ClassVar[FakeMacSupport] = FakeMacSupport.UNIMPLEMENTED

    def __init__(self, dev: usb.core.Device):
        self.transport = Rtl8821cuTransport(dev)
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self._rx_cb: Optional[Callable[[dict], None]] = None

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "Rtl8821cuDkmsDriver":
        return cls(dev)

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_cb = cb

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """Run the cold bring-up. Milestone 1 only — power tables are ported but the
        chip-id prologue the wire runs first is not, so this is not yet a usable connect."""
        bringup.cold_bringup(self.transport)
        raise BringUpError(
            "RTL8821CU (8821cu_dkms) is a milestone-1 WIP port: USB transport + HALMAC "
            "power tables only. Chip-id/EFUSE, firmware download, MAC/BB/RF init and monitor "
            "entry are not yet ported — verify with scripts/rtl8821cu_dkms/verify_pcap.py."
        )

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        raise NotImplementedError("RTL8821CU set_channel: later milestone")

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        raise NotImplementedError("RTL8821CU inject_frame: later milestone")

    async def close(self) -> None:
        self.transport.close()
