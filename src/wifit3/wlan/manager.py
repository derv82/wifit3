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

import asyncio
import logging
import os
import time
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


def _scan_bus(backend) -> List[tuple]:
    """Blocking bus scan: ``(dev, driver_cls, id_entry)`` for every supported VID:PID match.

    ``usb.core.find`` reads each device's descriptors, which stalls ~1s+ when a non-WinUSB
    device is present on Windows (libusb is slow to read a card it can't cleanly open). It's
    pure CPU/IO with no event-loop interaction, so callers run it via ``asyncio.to_thread`` to
    keep the TUI responsive. Driver construction stays on the loop — it's instant and may
    touch asyncio objects."""
    t = time.perf_counter()
    devs = list(usb.core.find(find_all=True, backend=backend))
    logger.debug("scan_bus: usb.core.find enumerated %d device(s) in %.0f ms",
                 len(devs), (time.perf_counter() - t) * 1000)
    out: List[tuple] = []
    for dev in devs:
        match = _match_driver(dev)
        if match is not None:
            out.append((dev, match[0], match[1]))
    return out


class WlanDeviceManager:
    """Scans PyUSB, dispatches to driver factories, returns WlanInterfaces."""

    def __init__(self) -> None:
        self.interfaces: List[WlanInterface] = []

    async def refresh(self) -> List[WlanInterface]:
        """Discover supported cards by VID:PID. The blocking bus scan runs in a thread (it
        stalls ~1s+ on a non-WinUSB device on Windows), so the poll never freezes the TUI; the
        openability/WinUSB check is deferred to connect time too. Driver construction is
        instant and stays on the loop. [DEVICE-SETUP.md]"""
        for iface in self.interfaces:
            await iface.close()
        self.interfaces = []

        t = time.perf_counter()
        backend = libusb_package.get_libusb1_backend()
        logger.debug("refresh: get_libusb1_backend took %.0f ms", (time.perf_counter() - t) * 1000)
        t = time.perf_counter()
        matches = await asyncio.to_thread(_scan_bus, backend)
        logger.debug("refresh: bus scan (off-thread) returned %d match(es) in %.0f ms",
                     len(matches), (time.perf_counter() - t) * 1000)

        for dev, driver_cls, id_entry in matches:
            try:
                driver = driver_cls.from_usb_device(dev, id_entry)
            except Exception as e:
                # A not-yet-ported placeholder (or a driver that opens the device just to
                # construct) — skip it; it simply isn't listed. Debug-level so a placeholder
                # doesn't spam the fast poll.
                logger.debug("Skipping %04x:%04x (%s): %s",
                             id_entry.vid, id_entry.pid, driver_cls.__name__, e)
                continue
            self.interfaces.append(WlanInterface(
                driver, f"wlan{len(self.interfaces)}", id_entry.description,
                vid=id_entry.vid, pid=id_entry.pid, dev=dev))

        logger.debug("Discovered %d supported card(s).", len(self.interfaces))
        return self.interfaces

    def get_interface(self, name: str) -> Optional[WlanInterface]:
        for iface in self.interfaces:
            if iface.name == name:
                return iface
        return None

    def get_interface_by_vidpid(self, vid: int, pid: int) -> Optional[WlanInterface]:
        """Re-find a card by VID:PID after it re-enumerated (e.g. post-WinUSB-install)."""
        for iface in self.interfaces:
            if iface.vid == vid and iface.pid == pid:
                return iface
        return None

    def is_openable(self, iface: WlanInterface) -> bool:
        """Can libusb open the card backing ``iface``? Opens + disposes the handle, so this
        BLOCKS — call it off the event loop. Used only when ``connect()`` fails, to tell
        "not WinUSB-bound" (→ offer an install) from a genuine init fault. [DEVICE-SETUP.md]"""
        return iface.dev is None or _is_openable(iface.dev)

    async def close_all(self) -> None:
        for iface in self.interfaces:
            await iface.close()
        self.interfaces = []
