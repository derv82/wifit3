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
import os
from typing import Dict, List, Optional, Type

import libusb_package
import usb.core
import usb.util

from wifit3.engine.protocols import DeviceID, WlanDriver

from .interface import WlanInterface

logger = logging.getLogger(__name__)

# RTL8814AU (0bda:8813) is claimed by BOTH the vendor/DKMS port (default) and the
# mainline-derived driver. Set this to "mainline" to fall back to the mainline driver.
ENV_RTL8814_DRIVER = "WIFIT3_RTL8814"

# RTL8821AU/RTL8811AU (0bda:0811) is claimed by BOTH the vendor/DKMS port (default) and
# the mainline driver. Set this to "mainline" to fall back to the mainline driver. Read
# fresh each call so it flips between runs.
ENV_RTL8821_DRIVER = "WIFIT3_RTL8821"

# RTL8812AU (0bda:8812) is claimed by BOTH the vendor/DKMS port (default) and the mainline
# driver. The DKMS port wins by default: mainline RX-wedges on the 2.4+5 GHz channel hop
# (RF-synth lock loss — its own driver logs it), which the DKMS port survives. Set this to
# "mainline" to fall back to the mainline driver (e.g. a fixed-channel, non-hopping use).
ENV_RTL8812_DRIVER = "WIFIT3_RTL8812"

_DRIVER_CLASSES: Dict[str, Type[WlanDriver]] | None = None


def _import_driver_classes() -> Dict[str, Type[WlanDriver]]:
    """Import every driver class once (deferred to sidestep the import cycle)."""
    global _DRIVER_CLASSES
    if _DRIVER_CLASSES is None:
        from wifit3.chips.ar9271.driver import AR9271Driver
        from wifit3.chips.mt76x0u.driver import MT76x0UDriver
        from wifit3.chips.mt76x2u.driver import MT76x2UDriver
        from wifit3.chips.mt7921au.driver import MT7921AUDriver
        from wifit3.chips.rt2500usb.driver import RT2500USBDriver
        from wifit3.chips.rt2800usb.driver import RT2800USBDriver
        from wifit3.chips.rtl8187.driver import RTL8187Driver
        from wifit3.chips.rtl8188eus.driver import RTL8188EUSDriver
        from wifit3.chips.rtl8812au.driver import RTL8812AUDriver
        from wifit3.chips.rtl8812au_dkms.driver import Rtl8812auDkmsDriver
        from wifit3.chips.rtl8814au_dkms.driver import Rtl8814auDkmsDriver
        from wifit3.chips.rtl8821au.driver import RTL8821AUDriver
        from wifit3.chips.rtl8821au_dkms.driver import Rtl8821auDkmsDriver
        from wifit3.chips.rtl8822bu.driver import RTL8822BUDriver
        from wifit3.chips.rtw88_8814au.driver import RTL8814AUDriver

        _DRIVER_CLASSES = {
            "ar9271": AR9271Driver,
            "rtl8187": RTL8187Driver,
            "rt2500usb": RT2500USBDriver,
            "rt2800usb": RT2800USBDriver,
            "rtl8188eus": RTL8188EUSDriver,
            "rtl8812au": RTL8812AUDriver,
            "rtl8812au_dkms": Rtl8812auDkmsDriver,
            "rtl8821au": RTL8821AUDriver,
            "rtl8821au_dkms": Rtl8821auDkmsDriver,
            "rtl8822bu": RTL8822BUDriver,
            "rtl8814au_dkms": Rtl8814auDkmsDriver,
            "rtl8814au_mainline": RTL8814AUDriver,
            "mt76x0u": MT76x0UDriver,
            "mt76x2u": MT76x2UDriver,
            "mt7921au": MT7921AUDriver,
        }
    return _DRIVER_CLASSES


def _all_drivers() -> List[Type[WlanDriver]]:
    """The driver registry, in priority order (first match wins in `_match_driver`).

    Both Realtek 11ac pairs are ordered by their env var (read fresh each call so they can
    be flipped between runs without restarting): the DKMS port wins by default, "mainline"
    falls back to the mainline-derived driver.
    """
    c = _import_driver_classes()
    if os.environ.get(ENV_RTL8814_DRIVER, "").strip().lower() == "mainline":
        rtl8814 = [c["rtl8814au_mainline"], c["rtl8814au_dkms"]]
    else:
        rtl8814 = [c["rtl8814au_dkms"], c["rtl8814au_mainline"]]
    if os.environ.get(ENV_RTL8821_DRIVER, "").strip().lower() == "mainline":
        rtl8821 = [c["rtl8821au"], c["rtl8821au_dkms"]]
    else:
        rtl8821 = [c["rtl8821au_dkms"], c["rtl8821au"]]
    if os.environ.get(ENV_RTL8812_DRIVER, "").strip().lower() == "mainline":
        rtl8812 = [c["rtl8812au"], c["rtl8812au_dkms"]]
    else:
        rtl8812 = [c["rtl8812au_dkms"], c["rtl8812au"]]
    return [
        c["ar9271"], c["rtl8187"], c["rt2500usb"], c["rt2800usb"], c["rtl8188eus"],
        *rtl8812, *rtl8821, c["rtl8822bu"], *rtl8814,
        c["mt76x0u"], c["mt76x2u"], c["mt7921au"],
    ]


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
