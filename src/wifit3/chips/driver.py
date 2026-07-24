"""The driver contract every ``chips/*/driver.py`` inherits, plus its value types."""
from __future__ import annotations

import asyncio
import enum
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, ClassVar, List, Optional, Protocol

import usb.core

if TYPE_CHECKING:
    from wifit3.dot11.packet import Packet


class ProgressCallback(Protocol):
    """A ``(percentage, message)`` sink for bring-up progress; percentage is 0..1."""
    def __call__(self, percentage: float, message: str) -> None: ...


class FakeMacSupport(enum.Enum):
    """A radio's ability to hardware-ACK a chosen MAC."""
    UNIMPLEMENTED = "unimplemented"   # not ported; the silicon may be capable
    NONE = "none"                     # silicon genuinely cannot ACK a chosen MAC
    FIXED_MAC = "fixed_mac"           # ACKs only the card's own MAC
    SPOOFABLE = "spoofable"           # ACKs an arbitrary forged MAC


_SILICON = {"RTL": "Realtek", "MT": "MediaTek", "RT": "Ralink", "AR": "Atheros"}  # RTL before RT


@dataclass(frozen=True)
class DeviceID:
    """One VID:PID a driver claims. ``chipset`` is the bare silicon (e.g. ``"RTL8812AU"``);
    ``vendor`` is the retail brand when the VID:PID names one unambiguously (else None, pending an
    OUI read); ``product_name`` is the bare model, with the brand inlined when ``vendor`` is None.
    ``extras`` carries driver-specific construction hints."""
    vid: int
    pid: int
    chipset: str
    vendor: Optional[str] = None
    product_name: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def silicon_vendor(self) -> str:
        """The chip maker, derived from the chipset prefix (Realtek/MediaTek/Ralink/Atheros)."""
        return next(v for p, v in _SILICON.items() if self.chipset.startswith(p))

    @property
    def description(self) -> str:
        """Human label ``"CHIPSET (brand model)"`` (or just ``"CHIPSET"``). Back-compat shim for
        the call sites that render a device as one string."""
        brand = " ".join(x for x in (self.vendor, self.product_name) if x)
        return f"{self.chipset} ({brand})" if brand else self.chipset


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

    MAX_ACK_DELAY: ClassVar[float] = 0.02
    """Default wait window for ``inject_frame_slow_retry``. An 802.11 ACK returns within SIFS
    (~10 us); ACKs via RxReader arrive ~20 ms later (averaged ~15 ms on Windows)."""

    mac_address: Optional[str]
    """The card's own MAC address, or None before it is read."""

    product_name: Optional[str] = None
    """The card's exact retail model. Defaults to None; a driver that can tell same-VID:PID variants
    apart by OUI (mt7921au: AXML vs PAU0F) seeds it from its DeviceID and narrows it during bring-up.
    The UI reads this in preference to the static SUPPORTED_IDS name; None means 'use that name'."""

    is_warm: bool
    """True if the chip was found already initialised (cold bring-up skipped)."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for attr in ("SUPPORTED_IDS", "SUPPORTED_CHANNELS"):
            if not hasattr(cls, attr):
                raise TypeError(f"{cls.__name__} must define {attr}")

    def __init__(self) -> None:
        self._ack_detect_on: bool = False           # is the tally armed?
        self._our_tx_macs: set[bytes] = set()       # source MACs whose returning ACKs we count
        self._ack_counts: dict[bytes, int] = {}     # MAC -> ACKs seen since the last reset

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
    async def close(self) -> None:
        """Stop RX loops and release the USB interface."""
        ...

    # ---- TX ---------------------------------------------------------------

    async def inject_frame(self, frame_bytes: bytes) -> bool:
        """Transmit one raw 802.11 frame, fire-and-forget. The chip's own HW ACK-based retry
        (the driver's per-chip retry limit) is the only retransmission. When the ACK tally is
        armed, the frame's Addr2 is registered so ``record_ack`` counts its ACKs. For a
        software retry that blocks on the recipient's ACK, use ``inject_frame_slow_retry``."""
        if self._ack_detect_on:
            mac = self._extract_mac(frame_bytes)
            if mac is not None:
                self._our_tx_macs.add(mac)
        return await self._inject_frame(self._stamp_tx_seq(frame_bytes))

    async def inject_frame_slow_retry(self, frame_bytes: bytes,
                                      timeout: Optional[float] = None,
                                      max_resends: int = 0) -> bool:
        """Note: Requires ``enable_rx_acks()`` to be executed first. Software ACK-based retry.
        Watches RX stream for an ACK to our Addr2, resending up to ``max_resends`` times if none 
        arrives within ``timeout`` (default ``MAX_ACK_DELAY``). "slow" because the RX-tap round-trip
        is ~15 m versus ~10 us for the chip's own HW retry. Each attempt is a fresh sequence number."""
        timeout = self.MAX_ACK_DELAY if timeout is None else timeout
        mac = self._extract_mac(frame_bytes)
        for _ in range(max_resends + 1):
            base = self.acks_seen(mac)
            ok = await self.inject_frame(frame_bytes)
            if not self._ack_detect_on or mac is None:
                return ok                              # can't observe an ACK -> fire-and-forget
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.acks_seen(mac) > base:
                    return True                        # a new ACK to our Addr2 landed
                await asyncio.sleep(0.001)
        return False                                   # never ACKed after every send

    @abstractmethod
    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Build the chip's TX descriptor and send ``frame_bytes`` once over bulk-OUT."""
        ...

    @abstractmethod
    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        """Return ``frame_bytes`` carrying an incrementing 802.11 sequence number, or return it
        unchanged if the chip's hardware assigns the sequence itself."""
        ...

    def _extract_mac(self, frame_bytes: bytes) -> Optional[bytes]:
        """Addr2/TA of ``frame_bytes``: the source MAC the recipient's ACK comes back to."""
        return bytes(frame_bytes[10:16]) if len(frame_bytes) >= 16 else None

    # ---- RX-ACK detection (the tally) -------------------------------------

    async def enable_rx_acks(self) -> None:
        """Ensures ACKs flow through the RX stream. Only streams ACKs to "our" macs.
        See ``inject_frame_slow_retry`` for more info. Note: Some cards emit ACKs
        over the RX stream by-default (nothing to "enable" on the chip)."""
        self._our_tx_macs.clear()
        self._ack_counts.clear()
        self._ack_detect_on = True
        await self._enable_rx_acks()

    async def disable_rx_acks(self) -> None:
        """Prevent ACKs from flowing through the RX stream. Note: This is impossible for some cards."""
        self._ack_detect_on = False
        await self._disable_rx_acks()

    @abstractmethod
    async def _enable_rx_acks(self) -> None:
        """Make the chip admit the recipient's ACK control frames (FC=0xD4) to RX. A register
        write on the cards that filter them out; a documented ``return`` no-op on the cards whose
        monitor filter already admits ACKs. Abstract so every port states which it is."""
        ...

    @abstractmethod
    async def _disable_rx_acks(self) -> None:
        """Undo ``_enable_rx_acks`` (or a documented no-op, matching it)."""
        ...

    def record_ack(self, frame: bytes) -> None:
        """Tally one received ACK when the tally is armed (via ``enable_rx_acks``) and the RA
        is a source MAC we have injected as. Drivers call this from their RX tap after the length/FC check."""
        if not self._ack_detect_on:
            return
        ra = bytes(frame[4:10])
        if ra in self._our_tx_macs:
            self._ack_counts[ra] = self._ack_counts.get(ra, 0) + 1

    def acks_seen(self, mac: Optional[bytes]) -> int:
        """ACKs tallied to source MAC ``mac`` since the last ``enable_rx_acks``. Approximate and
        chipset-dependent (some cards ignore ACKs and continue retransmitting, dumbly)."""
        return self._ack_counts.get(bytes(mac), 0) if mac is not None else 0

    # ---- Active monitor (make the chip EMIT ACKs for a chosen MAC) --------

    async def enter_active_monitor(self, mac: bytes,
                                   bssid: Optional[bytes] = None) -> bytes:
        """Arm hardware auto-ACK for ``mac`` while in monitor mode; return the MAC armed.
        ``bssid`` is the conversing AP, needed only by firmware-offload radios. Unlike
        ``_enable_rx_acks`` (admit incoming ACKs), this makes the chip EMIT ACKs on RX.
        Not supported across all cards (see ``FakeMacSupport``)."""
        raise NotImplementedError(f"{type(self).__name__} cannot active-monitor")

    async def exit_active_monitor(self) -> None:
        """Undo ``enter_active_monitor``."""
        raise NotImplementedError(f"{type(self).__name__} cannot active-monitor")
