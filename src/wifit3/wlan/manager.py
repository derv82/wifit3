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
from dataclasses import dataclass
from typing import Dict, List, Optional, Type

import libusb_package
import usb.core
import usb.util

from wifit3.engine.protocols import DeviceID, WlanDriver

from .interface import WlanInterface

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UnboundDevice:
    """A supported card that's plugged in but can't be opened — present on the bus
    (``find()`` sees it) yet has no libusb-class driver: on Windows it needs WinUSB
    (Zadig today), on Linux the kernel driver still holds it. The UX surfaces this as
    'present but needs setup' instead of failing silently. [DEVICE-SETUP.md Tier 0]"""
    vid: int
    pid: int
    description: str

    @property
    def vidpid(self) -> str:
        return f"{self.vid:04x}:{self.pid:04x}"

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

# RTL8188EUS (2357:010c) is claimed by BOTH the mainline-derived driver (default) and the
# vendor/DKMS port. Unlike the 11ac pairs, the mainline driver stays the default until the
# DKMS port is hardware-proven to tie/beat it on 2.4 GHz breadth; "dkms" opts in.
ENV_RTL8188_DRIVER = "WIFIT3_RTL8188"

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
        from wifit3.chips.rt3070.driver import RT3070PlaceholderDriver  # TEMP: Tier-0 UI only
        from wifit3.chips.rtl8187.driver import RTL8187Driver
        from wifit3.chips.rtl8188eus.driver import RTL8188EUSDriver
        from wifit3.chips.rtl8188eus_dkms.driver import Rtl8188eusDkmsDriver
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
            "rt3070": RT3070PlaceholderDriver,  # TEMP: Tier-0 device-setup UI placeholder

            "rtl8188eus": RTL8188EUSDriver,
            "rtl8188eus_dkms": Rtl8188eusDkmsDriver,
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
    # RTL8188EUS: mainline-derived port is the default; "dkms" opts into the vendor port.
    if os.environ.get(ENV_RTL8188_DRIVER, "").strip().lower() == "dkms":
        rtl8188 = [c["rtl8188eus_dkms"], c["rtl8188eus"]]
    else:
        rtl8188 = [c["rtl8188eus"], c["rtl8188eus_dkms"]]
    return [
        c["ar9271"], c["rtl8187"], c["rt2500usb"], c["rt2800usb"], c["rt3070"], *rtl8188,
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


def _is_openable(dev: usb.core.Device) -> bool:
    """Tier-0 probe: can libusb actually OPEN this device, or is it present-but-unbound?

    ``find()`` enumerates every device regardless of driver, but opening one needs a
    libusb-class driver. On Windows an un-bound card (native Wi-Fi driver, no WinUSB)
    raises ``NotImplementedError`` (libusb ``LIBUSB_ERROR_NOT_SUPPORTED``) on the first
    open — verified on a fresh RT3070/netr28ux [DEVICE-SETUP.md VERIFY-W1]. Reading the
    active configuration is the least-invasive op that forces that open.

    Linux note: a kernel-claimed card can still read descriptors here (the claim is what
    fails, later, in the driver), so this returns True for it and the existing
    ``from_usb_device`` guard handles that case — Linux 'needs-detach' classification is a
    separate refinement (DEVICE-SETUP.md Linux / VERIFY-L2)."""
    try:
        dev.get_active_configuration()
        return True
    except NotImplementedError:
        return False          # Windows: enumerable but no WinUSB driver to open it
    except usb.core.USBError:
        return False          # busy / access-denied — not ready to drive either way
    finally:
        # get_active_configuration() opens a libusb handle to read the descriptor. On
        # Windows WinUSB that handle is *exclusive*, so leaking it makes the NEXT probe
        # (1 s later, and other processes) fail with ACCESS_DENIED — a ready card then
        # flips to present-but-unbound on the next refresh. Always release what we opened
        # here; drivers re-open lazily in connect(). dispose on a never-opened dev is a
        # harmless no-op.
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass


class WlanDeviceManager:
    """Scans PyUSB, dispatches to driver factories, returns WlanInterfaces."""

    def __init__(self) -> None:
        self.interfaces: List[WlanInterface] = []
        # Supported cards that are plugged in but not openable (need WinUSB / kernel
        # detach). Populated by refresh(); the splash surfaces them. [DEVICE-SETUP.md Tier 0]
        self.unbound: List[UnboundDevice] = []

    async def refresh(self) -> List[WlanInterface]:
        backend = libusb_package.get_libusb1_backend()

        # Clean state.
        for iface in self.interfaces:
            await iface.close()
        self.interfaces = []
        self.unbound = []

        for dev in usb.core.find(find_all=True, backend=backend):
            match = _match_driver(dev)
            if match is None:
                continue
            driver_cls, id_entry = match
            # Tier-0 classify: a known card that find() lists but libusb can't open is
            # present-but-unbound (needs WinUSB / detach) — surface it, don't try to drive
            # it (that would just raise deep in from_usb_device/connect).
            if not _is_openable(dev):
                logger.info(
                    "Present-but-unbound (needs WinUSB/detach): %s (vid=%04x pid=%04x)",
                    id_entry.description, id_entry.vid, id_entry.pid,
                )
                self.unbound.append(
                    UnboundDevice(id_entry.vid, id_entry.pid, id_entry.description))
                continue
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
                vid=id_entry.vid,
                pid=id_entry.pid,
            )
            self.interfaces.append(iface)

        logger.info("Discovered %d native WlanInterfaces, %d present-but-unbound.",
                    len(self.interfaces), len(self.unbound))
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
