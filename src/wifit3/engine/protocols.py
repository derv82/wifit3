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

from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, List, Optional, Protocol

import usb.core


class ProgressCallback(Protocol):
    def __call__(self, percentage: float, message: str) -> None: ...


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

    async def set_channel(self, channel: int) -> bool:
        """Tune to `channel` (1..14 on 2.4 GHz, 36..165 on 5 GHz)."""
        ...

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        """Inject a raw 802.11 frame. Driver wraps with HW descriptors."""
        ...

    async def close(self) -> None:
        """Stop RX loops, release the USB interface."""
        ...
