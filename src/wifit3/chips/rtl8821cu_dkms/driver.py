"""RTL8821CU / 1T1R 802.11ac — vendor (HALMAC/PHYDM) cleanroom DKMS port.

The byte-for-byte gate (``scripts/rtl8821cu_dkms/verify_pcap.py``) drives this driver's public
interface — ``connect`` (cold init + airmon monitor entry), ``set_channel`` (the airodump hops)
and ``inject_frame`` (the aireplay-ng test + deauth TX) — and reproduces the **entire** cold-boot
capture (all 21409 ctrl + bulk-OUT ops) byte-for-byte, so what the gate verifies is exactly the
product code path. The chip→host interrupt-IN (C2H) and bulk-IN (RX) streams are a separate blind
spot the host-side replay does not model — see RTL8821CU_DKMS.md.

Deliberately NOT registered in ``wlan/manager.py`` yet: warm reattach and the RX-reader thread are
not wired, and the ZeroCD / mode-switch discovery blocker (see RTL8821CU_DKMS.md) means a fresh
plug enumerates as a CD-ROM, not the Wi-Fi function. Registration lands once those are resolved;
until then it is exercised through verify_pcap (and ``scripts/rtl8821cu_dkms/test_hw.py``). Shares
no code with the other Realtek drivers by design (anti-DRY).
"""
from __future__ import annotations

import logging
from typing import Callable, ClassVar, List, Optional

import usb.core

from wifit3.engine.protocols import DeviceID, FakeMacSupport, ProgressCallback

from . import bringup, chan, tx
from .constants import USB_PID_8821CU, USB_VID_REALTEK
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
        self.info = None                # EfuseInfo from cold_bringup; set_channel/inject need it
        self._rx_cb: Optional[Callable[[dict], None]] = None

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "Rtl8821cuDkmsDriver":
        return cls(dev)

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_cb = cb

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """Cold bring-up + airmon monitor entry (``bringup.cold_bringup``), caching the decoded
        EFUSE/board info that ``set_channel`` and the watchdog/coex producers key on. Reproduced
        byte-for-byte by ``scripts/rtl8821cu_dkms/verify_pcap.py``. Warm reattach + the RX-reader
        thread are not wired yet, so this is not yet registered in ``wlan/manager.py``."""
        self.info = bringup.cold_bringup(self.transport)
        return True

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to ``channel`` via the phydm band/channel/bandwidth set (``chan.set_channel``,
        20 MHz). Requires a prior ``connect`` (needs the cached ``info``)."""
        chan.set_channel(self.transport, self.info, channel)
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
        self.transport.close()
