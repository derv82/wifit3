"""The driver contract every ``chips/*/driver.py`` inherits, plus its value types."""
from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, ClassVar, List, Optional, Protocol

import usb.core

if TYPE_CHECKING:
    from wifit3.wlan.packet import Packet


class ProgressCallback(Protocol):
    """A ``(percentage, message)`` sink for bring-up progress; percentage is 0..1."""
    def __call__(self, percentage: float, message: str) -> None: ...


class FakeMacSupport(enum.Enum):
    """A radio's ability to hardware-ACK a chosen MAC."""
    UNIMPLEMENTED = "unimplemented"   # not ported; the silicon may be capable
    NONE = "none"                     # silicon genuinely cannot ACK a chosen MAC
    FIXED_MAC = "fixed_mac"           # ACKs only the card's own MAC
    SPOOFABLE = "spoofable"           # ACKs an arbitrary forged MAC


@dataclass(frozen=True)
class DeviceID:
    """One VID:PID a driver claims. ``extras`` carries driver-specific construction hints."""
    vid: int
    pid: int
    description: str
    extras: dict[str, Any] = field(default_factory=dict)


class Driver(ABC):
    """The contract every ``chips/*/driver.py`` implements."""

    SUPPORTED_IDS: ClassVar[List[DeviceID]]
    """Every USB VID:PID this driver claims."""

    SUPPORTED_CHANNELS: ClassVar[List[int]]
    """Every channel this driver can tune to (2.4 GHz 1..14, 5 GHz 36..165)."""

    FAKE_MAC: ClassVar[FakeMacSupport] = FakeMacSupport.UNIMPLEMENTED
    """This radio's ability to auto-ACK a programmed MAC."""

    CONFLICTING_LINUX_MODULES: ClassVar[List[str]] = []
    """Leaf kernel module(s) that bind this chipset; Linux setup blacklists them."""

    LINUX_REPLUG_AFTER_MODPROBE: ClassVar[bool] = True
    """Whether Linux setup must request a physical replug before ``connect()`` (default
    True: a kernel-warmed chip usually can't reach a clean cold state in userland)."""

    mac_address: Optional[str]
    """The card's own MAC address, or None before it is read."""

    is_warm: bool
    """True if the chip was found already initialised (cold bring-up skipped)."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for attr in ("SUPPORTED_IDS", "SUPPORTED_CHANNELS"):
            if not hasattr(cls, attr):
                raise TypeError(f"{cls.__name__} must define {attr}")

    @classmethod
    @abstractmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "Driver":
        """Construct a driver instance for ``dev``."""
        ...

    @abstractmethod
    def register_rx_callback(self, cb: Callable[[Packet], None]) -> None:
        """Register a sink that receives one parsed Packet per decoded 802.11 frame."""
        ...

    @abstractmethod
    def register_disconnect_callback(self, cb: Callable[[], None]) -> None:
        """Register a sink called when the RX reader hits a terminal failure."""
        ...

    @abstractmethod
    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """Bring up the hardware, reporting progress. On failure raise
        ``BringUpError(stage, detail)`` rather than returning False; the True return
        exists for the warm no-op path."""
        ...

    @abstractmethod
    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to ``channel``. ``scan=True`` marks a transient hop, permitting a lighter path."""
        ...

    @abstractmethod
    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True,
                           wait_for_ack: float = 0.0, max_resends: int = 0) -> bool:
        """Transmit one raw 802.11 frame, wrapping it in the chip's TX descriptor."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Stop RX loops and release the USB interface."""
        ...

    @abstractmethod
    async def enable_ack_detect(self) -> None:
        """Begin counting the AP's link-ACKs to our injected frames, resetting the tally."""
        ...

    @abstractmethod
    async def disable_ack_detect(self) -> None:
        """Stop counting ACKs."""
        ...

    @abstractmethod
    def acks_seen(self, mac: bytes) -> int:
        """ACKs seen addressed to ``mac`` since the last ``enable_ack_detect()``."""
        ...

    async def enter_active_monitor(self, mac: bytes,
                                   bssid: Optional[bytes] = None) -> bytes:
        """Arm hardware auto-ACK for ``mac`` while in monitor mode; return the MAC armed.
        ``bssid`` is the conversing AP, needed only by firmware-offload radios. The base
        raises: a driver whose FAKE_MAC can ACK overrides it."""
        raise NotImplementedError(f"{type(self).__name__} cannot active-monitor")

    async def exit_active_monitor(self) -> None:
        """Undo ``enter_active_monitor``. The base raises; capable drivers override."""
        raise NotImplementedError(f"{type(self).__name__} cannot active-monitor")
