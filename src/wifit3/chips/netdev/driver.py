"""Linux netdev 802.11 driver for PCIe / M.2 cards via AF_PACKET and nl80211/iw."""
from __future__ import annotations

import asyncio
import logging
import shutil
import socket
import struct
import subprocess
from pathlib import Path
from typing import Callable, ClassVar, List, Optional

from wifit3.chips.driver import Driver, FakeMacSupport, ProgressCallback
from wifit3.dot11.packet import Packet
from wifit3.dot11.parser import WlanFrameParser
from wifit3.errors import BringUpError, BringUpPermissionsError
from wifit3.models.device_id import DeviceID

logger = logging.getLogger(__name__)

ETH_P_ALL = 0x0003


def parse_radiotap(data: bytes) -> tuple[bytes, int]:
    """Parse a Linux radiotap frame: returns (mpdu_bytes, rssi_dbm)."""
    if len(data) < 8 or data[0] != 0:
        return data, -50
    it_len = struct.unpack_from("<H", data, 2)[0]
    if it_len > len(data):
        return b"", -50
    it_present = struct.unpack_from("<I", data, 4)[0]
    offset = 8
    while (it_present & (1 << 31)) and offset + 4 <= it_len:
        it_present = struct.unpack_from("<I", data, offset)[0]
        offset += 4

    rssi = -50
    flags = 0
    if it_present & (1 << 0):
        offset = (offset + 7) & ~7
        offset += 8
    if it_present & (1 << 1):
        if offset < it_len:
            flags = data[offset]
        offset += 1
    if it_present & (1 << 2):
        offset += 1
    if it_present & (1 << 3):
        offset = (offset + 1) & ~1
        offset += 4
    if it_present & (1 << 4):
        offset = (offset + 1) & ~1
        offset += 2
    if it_present & (1 << 5):
        if offset < it_len:
            rssi = struct.unpack_from("<b", data, offset)[0]
        offset += 1

    mpdu = data[it_len:]
    if (flags & 0x10) and len(mpdu) >= 4:
        mpdu = mpdu[:-4]
    return mpdu, rssi


class LinuxNetdevDriver(Driver):
    """Driver implementation for Linux PCIe/M.2 cards via standard netdev and nl80211."""

    SUPPORTED_CHANNELS: ClassVar[List[int]] = [
        1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14,
        36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
        149, 153, 157, 161, 165,
    ]
    FAKE_MAC: ClassVar[FakeMacSupport] = FakeMacSupport.SPOOFABLE
    CONFLICTING_LINUX_MODULES: ClassVar[List[str]] = []
    LINUX_REPLUG_AFTER_MODPROBE: ClassVar[bool] = False

    def __init__(self, iface: str, id_entry: DeviceID) -> None:
        super().__init__()
        self.iface = iface
        self.id_entry = id_entry
        self.mac_address = self._read_mac()
        self._sock: Optional[socket.socket] = None
        self._rx_callback: Optional[Callable[[Packet], None]] = None
        self._disconnect_callback: Optional[Callable[[], None]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._tx_seq = 0
        self.is_warm = False
        self._changed_mode = False

    @classmethod
    def from_netdev(cls, iface: str, id_entry: DeviceID) -> "LinuxNetdevDriver":
        return cls(iface, id_entry)

    def _read_mac(self) -> Optional[str]:
        path = Path(f"/sys/class/net/{self.iface}/address")
        try:
            return path.read_text().strip().upper()
        except OSError:
            return None

    def _is_monitor(self) -> bool:
        try:
            out = subprocess.run(["iw", "dev", self.iface, "info"], capture_output=True, text=True, timeout=5)
            return "type monitor" in out.stdout
        except (OSError, subprocess.SubprocessError):
            return False

    async def _ensure_monitor_mode(self) -> None:
        if self._is_monitor():
            return

        cmds = [
            f"ip link set {self.iface} down",
            f"iw dev {self.iface} set type monitor",
            f"ip link set {self.iface} up",
        ]
        if shutil.which("nmcli"):
            cmds.insert(0, f"nmcli dev set {self.iface} managed no")

        compound = " && ".join(cmds)
        # Try unprivileged first
        try:
            ret = subprocess.run(["sh", "-c", compound], capture_output=True, text=True, timeout=10)
            if ret.returncode == 0:
                self._changed_mode = True
                return
        except (OSError, subprocess.SubprocessError):
            pass

        # Try privilege escalation if unprivileged failed
        from wifit3.setup.linux import _choose_escalation, run_privileged
        esc = _choose_escalation()
        if esc is not None:
            code = await asyncio.to_thread(run_privileged, compound, esc)
            if code == 0:
                self._changed_mode = True
                return

        raise BringUpPermissionsError(
            "monitor",
            f"Failed to set {self.iface} into monitor mode. Root/CAP_NET_ADMIN privileges required.",
        )

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        if progress_cb:
            progress_cb(0.2, f"Setting monitor mode on {self.iface}…")
        await self._ensure_monitor_mode()

        if progress_cb:
            progress_cb(0.6, "Opening raw packet socket…")
        try:
            self._sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
            self._sock.bind((self.iface, 0))
            self._sock.setblocking(False)
        except PermissionError as exc:
            raise BringUpPermissionsError("open", f"Raw socket access denied on {self.iface}: {exc}") from exc
        except OSError as exc:
            raise BringUpError("open", f"Failed to bind {self.iface}: {exc}") from exc

        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(self._sock.fileno(), self._on_socket_read)

        if progress_cb:
            progress_cb(1.0, f"{self.iface} monitor active")
        return True

    def register_rx_callback(self, cb: Callable[[Packet], None]) -> None:
        self._rx_callback = cb

    def register_disconnect_callback(self, cb: Callable[[], None]) -> None:
        self._disconnect_callback = cb

    def _on_socket_read(self) -> None:
        if self._sock is None:
            return
        try:
            raw_data = self._sock.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except Exception:
            if self._disconnect_callback:
                self._disconnect_callback()
            return

        mpdu, rssi = parse_radiotap(raw_data)
        if not mpdu:
            return
        if self._ack_detect_on:
            self.record_ack(mpdu)
        pkt = WlanFrameParser.parse_80211_frame(mpdu, rssi=rssi)
        if pkt is not None and self._rx_callback:
            self._rx_callback(pkt)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        cmd = ["iw", "dev", self.iface, "set", "channel", str(channel)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return (await proc.wait()) == 0
        except (OSError, subprocess.SubprocessError):
            return False

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        if self._sock is None:
            return False
        # Minimal 8-byte radiotap header telling mac80211 to inject the raw 802.11 MPDU
        radiotap = b"\x00\x00\x08\x00\x00\x00\x00\x00"
        try:
            self._sock.send(radiotap + frame_bytes)
            return True
        except Exception:
            return False

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        self._tx_seq = (self._tx_seq + 1) & 0xFFF
        if len(frame_bytes) >= 24:
            seq = (self._tx_seq << 4) & 0xFFF0
            return frame_bytes[:22] + struct.pack("<H", seq) + frame_bytes[24:]
        return frame_bytes

    async def _enable_rx_acks(self) -> None:
        return

    async def _disable_rx_acks(self) -> None:
        return

    async def close(self) -> None:
        if self._sock is not None and self._loop is not None:
            try:
                self._loop.remove_reader(self._sock.fileno())
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

        if self._changed_mode:
            try:
                subprocess.run(
                    f"ip link set {self.iface} down && iw dev {self.iface} set type managed && ip link set {self.iface} up",
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
            except Exception:
                pass
