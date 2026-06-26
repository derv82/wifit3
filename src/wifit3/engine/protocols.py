"""Wifit3 driver Protocol + device-id registry.

The :class:`WlanDriver` Protocol is wifit3's structural contract for a
chipset driver. Every driver under ``wifit3.chips.<name>.driver``
satisfies this Protocol and:

* declares its supported USB VID:PIDs via :attr:`SUPPORTED_IDS`
* provides a :py:meth:`WlanDriver.from_usb_device` factory so the
  device manager can instantiate it without knowing chip-specific
  construction details (transport wrapping, chip-id discriminators,
  etc.)
* implements the runtime methods consumed by :class:`WlanInterface`
  (``connect``, ``set_channel``, ``inject_frame``, ``close``,
  ``register_rx_callback``)
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, List, Optional, Protocol

import usb.core


class ProgressCallback(Protocol):
    def __call__(self, percentage: float, message: str) -> None: ...


class FakeMacSupport(enum.Enum):
    """Whether/how a driver can make the radio auto-ACK a chosen MAC.

    Auto-ACK for a programmed MAC is the prerequisite for any *ACKed conversation*
    where the AP addresses us and expects a link-layer ACK — WPS, EAP, and a
    software FakeAP/EvilTwin. Without it the AP retransmits each frame to its retry
    limit and abandons the session. A driver that omits FAKE_MAC is UNIMPLEMENTED.

    UNIMPLEMENTED — this driver hasn't ported active-monitor; the silicon may be capable,
                so steer the user to the card's default/recommended driver, NOT to a
                hardware limit. (The default for any driver that omits FAKE_MAC.)
    NONE      — the silicon genuinely cannot ACK a chosen MAC (hard/un-spoofable MAC,
                e.g. rtl8187, rt2500usb).
    FIXED_MAC — it ACKs, but only the card's own MAC; ``enter_active_monitor`` ignores the
                requested MAC and returns the card's own.
    SPOOFABLE — it ACKs an arbitrary forged MAC programmed at runtime.
    """
    UNIMPLEMENTED = "unimplemented"
    NONE = "none"
    FIXED_MAC = "fixed_mac"
    SPOOFABLE = "spoofable"


@dataclass(frozen=True)
class DeviceID:
    """One entry in a driver's supported-hardware list.

    `extras` carries driver-specific construction hints — RT2800USB uses
    it for `chip_id` (rt5572 / rt3572 / rt5372). Drivers that don't need
    extras just leave it empty.
    """
    vid: int
    pid: int
    description: str
    extras: dict[str, Any] = field(default_factory=dict)


class WlanDriver(Protocol):
    """Structural contract for a wifit3 hardware driver."""

    # ---- Discovery ----------------------------------------------------
    SUPPORTED_IDS: ClassVar[List[DeviceID]]
    """All USB VID:PID combinations this driver claims."""

    # ---- Capabilities -------------------------------------------------
    SUPPORTED_CHANNELS: ClassVar[List[int]]
    """All channel numbers this driver can tune to. Consumed by the UI
    (channel hopping default, range validation, etc.). 2.4 GHz channels
    are 1..14; 5 GHz channels are 36..165. Drivers that only support
    2.4 GHz should list 1..13 (or whatever their PHY actually supports).
    """

    # ---- Optional: Linux take-control hint ----------------------------
    KERNEL_MODULES: ClassVar[List[str]] = []
    """Optional fallback hint: the Linux kernel module name(s) that bind this
    chipset (e.g. ``["ath9k_htc"]``). The Linux "hand wifit3 this card"
    setup blacklists these so the kernel can't grab + taint the cold-boot
    state. Authoritative discovery is *live* (sysfs bound-driver + ``modprobe
    -R`` against the plugged-in card); this list is only consulted when the
    device isn't present to probe, so drivers may leave it empty. List only
    the leaf USB-binding module, never the shared stack below it
    (``mac80211``/``cfg80211``/…)."""

    @classmethod
    def from_usb_device(
        cls, dev: usb.core.Device, id_entry: DeviceID
    ) -> "WlanDriver":
        """Construct a driver instance for `dev`.

        The driver implements its own transport wrapping / chip-id
        derivation here, keeping the device manager generic.
        """
        ...

    # ---- Runtime state ------------------------------------------------
    mac_address: Optional[str]
    is_warm: bool

    # ---- Hooks --------------------------------------------------------
    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        """Register a function that receives one parsed-frame dict per
        802.11 frame the driver decodes."""
        ...

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """Initialise the hardware. May upload firmware, run init sequences,
        start RX loops, etc. Should yield (0..1, message) progress."""
        ...

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to `channel` (1..14 on 2.4 GHz, 36..165 on 5 GHz).

        ``scan=True`` hints this is a transient scan/hop tune, not a settle —
        drivers MAY take a lighter path (e.g. skip the per-hop calibration the
        kernel also skips while scanning). Most drivers ignore it; mt76x2u uses
        it to avoid a ~2 s recalibration on every hop.
        """
        ...

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        """Inject a raw 802.11 frame. Driver wraps with HW descriptors."""
        ...

    async def close(self) -> None:
        """Stop RX loops, release the USB interface."""
        ...

    # ---- Optional capability: active monitor (HW-ACK a chosen MAC) -----
    # Soft contract for now: a driver that can ACK a chosen MAC declares
    # FAKE_MAC and implements enter/exit_active_monitor; the interface gates on
    # both via getattr/hasattr, so drivers predating this stay valid (treated as
    # FakeMacSupport.UNIMPLEMENTED). Promote to a hard member once every driver declares it.
    FAKE_MAC: ClassVar[FakeMacSupport]
    """This radio's ability to auto-ACK a programmed MAC (default UNIMPLEMENTED if absent)."""

    async def enter_active_monitor(
        self, mac: bytes, bssid: Optional[bytes] = None
    ) -> bytes:
        """Arm hardware auto-ACK for ``mac`` while staying in monitor mode.

        ``bssid`` is the AP we're conversing with — required only by firmware-offload
        radios that gate ACK on an active BSS link (connac2); register-MAC radios
        ignore it (ACK is a pure RA==own-MAC match). Returns the MAC actually armed:
        == ``mac`` on a SPOOFABLE radio, the card's own MAC on a FIXED_MAC one.
        """
        ...

    async def exit_active_monitor(self) -> None:
        """Restore the plain-monitor baseline (stop ACKing the chosen MAC)."""
        ...
