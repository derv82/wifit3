"""USB-bus scan + driver dispatch.

The manager has zero chipset-specific knowledge. Each driver declares
its own VID:PIDs via :attr:`SUPPORTED_IDS` and constructs itself via
:py:meth:`from_usb_device`; the manager just iterates the bus and asks
each driver "is this yours?".

Driver imports are deferred to the first :func:`_all_drivers` call to
sidestep the cycle through ``wifit3.wlan.__init__`` that would otherwise
form on first import.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Type

import libusb_package
import usb.core
import usb.util

from wifit3.engine.protocols import DeviceID, WlanDriver

from .interface import WlanInterface

logger = logging.getLogger(__name__)


_ALL_DRIVERS: List[Type[WlanDriver]] | None = None


def _all_drivers() -> List[Type[WlanDriver]]:
    """The driver registry, built once on first call.

    Order is the priority order for VID:PID disambiguation (only matters
    if two drivers ever claim the same pair).
    """
    global _ALL_DRIVERS
    if _ALL_DRIVERS is None:
        from wifit3.chips.ar9271.driver import AR9271Driver
        from wifit3.chips.mt76x0u.driver import MT76x0UDriver
        from wifit3.chips.mt76x2u.driver import MT76x2UDriver
        from wifit3.chips.mt7921au.driver import MT7921AUDriver
        from wifit3.chips.rt2500usb.driver import RT2500USBDriver
        from wifit3.chips.rt2800usb.driver import RT2800USBDriver
        from wifit3.chips.rtl8187.driver import RTL8187Driver
        from wifit3.chips.rtl8188eus.driver import RTL8188EUSDriver
        from wifit3.chips.rtl8812au.driver import RTL8812AUDriver
        from wifit3.chips.rtl8821au.driver import RTL8821AUDriver
        from wifit3.chips.rtl8822bu.driver import RTL8822BUDriver

        _ALL_DRIVERS = [
            AR9271Driver,
            RTL8187Driver,
            RT2500USBDriver,
            RT2800USBDriver,
            RTL8188EUSDriver,
            RTL8812AUDriver,
            RTL8821AUDriver,
            RTL8822BUDriver,
            MT76x0UDriver,
            MT76x2UDriver,
            MT7921AUDriver,
        ]
    return _ALL_DRIVERS


def _match_driver(
    dev: usb.core.Device,
) -> Optional[tuple[Type[WlanDriver], DeviceID]]:
    """Find the first registered driver that claims `dev`."""
    for driver_cls in _all_drivers():
        for entry in driver_cls.SUPPORTED_IDS:
            if entry.vid == dev.idVendor and entry.pid == dev.idProduct:
                return driver_cls, entry
    return None


class WlanDeviceManager:
    """Scans PyUSB, dispatches to driver factories, returns WlanInterfaces."""

    def __init__(self) -> None:
        self.interfaces: List[WlanInterface] = []

    async def refresh(self) -> List[WlanInterface]:
        backend = libusb_package.get_libusb1_backend()

        # Clean state.
        for iface in self.interfaces:
            await iface.close()
        self.interfaces = []

        for dev in usb.core.find(find_all=True, backend=backend):
            match = _match_driver(dev)
            if match is None:
                continue
            driver_cls, id_entry = match
            logger.info(
                "Found supported hardware: %s (vid=%04x pid=%04x)",
                id_entry.description, id_entry.vid, id_entry.pid,
            )
            try:
                driver = driver_cls.from_usb_device(dev, id_entry)
            except Exception as e:
                logger.exception(
                    "Failed to construct %s for %04x:%04x: %s",
                    driver_cls.__name__, id_entry.vid, id_entry.pid, e,
                )
                continue
            iface = WlanInterface(
                driver,
                f"wlan{len(self.interfaces)}",
                id_entry.description,
            )
            self.interfaces.append(iface)

        logger.info("Discovered %d native WlanInterfaces.", len(self.interfaces))
        return self.interfaces

    def get_interface(self, name: str) -> Optional[WlanInterface]:
        for iface in self.interfaces:
            if iface.name == name:
                return iface
        return None

    async def close_all(self) -> None:
        for iface in self.interfaces:
            await iface.close()
        self.interfaces = []
