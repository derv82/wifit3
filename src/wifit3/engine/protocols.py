"""Typing contracts for wifit3 chipset drivers: the structural Protocol
(WlanDriver), its supported-hardware records (DeviceID), and the
capability/progress helper types (FakeMacSupport, ProgressCallback)."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, ClassVar, List, Optional, Protocol

if TYPE_CHECKING:
    from wifit3.wlan.packet import Packet

import usb.core


class ProgressCallback(Protocol):
    """Reports incremental driver bring-up status, so a multi-second
    connect() surfaces progress instead of appearing frozen. `percentage`
    is a 0..1 fraction (1.0 == done)."""
    def __call__(self, percentage: float, message: str) -> None: ...


class FakeMacSupport(enum.Enum):
    """Whether/how a driver can make the radio auto-ACK a chosen MAC.

    Auto-ACK for a programmed MAC is the prerequisite for any *ACKed conversation*
    where the AP addresses us and expects a link-layer ACK — e.g. WPS and PMKID
    association. Without it the AP retransmits each frame to its retry limit and
    abandons the session.

    UNIMPLEMENTED and NONE both mean "can't auto-ACK", for different reasons:
    UNIMPLEMENTED — this driver hasn't implemented auto-ACK; the silicon may well be
                capable. (The default when a driver omits FAKE_MAC.)
    NONE      — the silicon genuinely cannot ACK a chosen MAC (hard/un-spoofable MAC,
                e.g. rtl8187, rt2500usb).
    FIXED_MAC — it ACKs, but only the card's own MAC.
    SPOOFABLE — it ACKs an arbitrary forged MAC programmed at runtime.
    """
    UNIMPLEMENTED = "unimplemented"
    NONE = "none"
    FIXED_MAC = "fixed_mac"
    SPOOFABLE = "spoofable"


@dataclass(frozen=True)
class DeviceID:
    """One entry in a driver's supported-hardware list.

    `extras` carries driver-specific construction hints — e.g. RT2800USB
    stores a chip-variant discriminator. Drivers that don't need extras
    just leave it empty.
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
    """All channel numbers this driver can tune to. 2.4 GHz channels
    are 1..14; 5 GHz channels are 36..165. Drivers that only support
    2.4 GHz should list 1..13 (or whatever their PHY actually supports).
    """

    # ---- Optional: Linux device-setup metadata ------------------------
    # Not part of the runtime contract — consulted only by Linux device setup.
    CONFLICTING_LINUX_MODULES: ClassVar[List[str]] = []
    """Optional fallback hint: the Linux kernel module name(s) that bind this
    chipset (e.g. ``["ath9k_htc"]``). Device setup blacklists/unloads these so
    the kernel can't grab + taint the cold-boot state. Authoritative discovery
    is *live* (sysfs bound-driver + ``modprobe -R`` against the plugged-in
    card); this list is only consulted when the device isn't present to probe,
    so drivers may leave it empty. List only the leaf USB-binding module, never
    the shared stack below it (``mac80211``/``cfg80211``/…)."""

    LINUX_REPLUG_AFTER_MODPROBE: ClassVar[bool] = True
    """Whether a Linux install must ask for a physical replug before connecting.

    Device setup unloads the kernel driver (``modprobe -r``) but that leaves the
    card *warm*. Most silicon can't reach a clean cold state from a kernel-warmed
    chip in userland, so RX comes up silently degraded unless the user physically
    replugs (a real power-cycle → cold boot). The **safe default is therefore
    True** — ask for the replug. A chip that genuinely self-colds in userland
    opts out with ``False`` (AR9271 re-enumerates on its firmware download,
    mt76x0u on ``modprobe -r``, mt76x2u via ``force_power_cycle``). Read only via
    getattr in the Linux setup layer, which also defaults True, so a driver that
    omits it is treated as replug-required."""

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
    def register_rx_callback(self, cb: Callable[[Packet], None]) -> None:
        """Register a function that receives one parsed Packet per
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
    # FakeMacSupport.UNIMPLEMENTED).
    # TODO: promote to a hard member once every driver declares it.
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
