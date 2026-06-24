"""USB-bus scan + driver dispatch.

The manager has zero chipset-specific knowledge. Each driver declares
its own VID:PIDs via :attr:`SUPPORTED_IDS` and constructs itself via
:py:meth:`from_usb_device`; the manager just iterates the bus and asks
each driver "is this yours?".
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Dict, List, NoReturn, Optional, Type

import libusb_package
import usb.core
import usb.util

from wifit3.engine.protocols import DeviceID, WlanDriver
from wifit3.errors import WifiteFatalError

from .interface import WlanInterface

logger = logging.getLogger(__name__)


ENV_RTL8814_DRIVER = "WIFIT3_RTL8814"
ENV_RTL8821_DRIVER = "WIFIT3_RTL8821"
ENV_RTL8812_DRIVER = "WIFIT3_RTL8812"
ENV_RTL8188_DRIVER = "WIFIT3_RTL8188"
ENV_RTL8822_DRIVER = "WIFIT3_RTL8822"

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
        from wifit3.chips.rt3070.driver import RT3070Driver
        from wifit3.chips.rt5370.driver import RT5370Driver
        from wifit3.chips.rt5372.driver import RT5372Driver
        from wifit3.chips.rtl8187.driver import RTL8187Driver
        from wifit3.chips.rtl8188eus.driver import RTL8188EUSDriver
        from wifit3.chips.rtl8188eus_dkms.driver import Rtl8188eusDkmsDriver
        from wifit3.chips.rtl8812au.driver import RTL8812AUDriver
        from wifit3.chips.rtl8812au_dkms.driver import Rtl8812auDkmsDriver
        from wifit3.chips.rtl8814au_dkms.driver import Rtl8814auDkmsDriver
        from wifit3.chips.rtl8821au.driver import RTL8821AUDriver
        from wifit3.chips.rtl8821au_dkms.driver import Rtl8821auDkmsDriver
        from wifit3.chips.rtl8821cu_dkms.driver import Rtl8821cuDkmsDriver
        from wifit3.chips.rtl8822bu.driver import RTL8822BUDriver
        from wifit3.chips.rtl8822bu_dkms.driver import Rtl8822buDkmsDriver
        from wifit3.chips.rtw88_8814au.driver import RTL8814AUDriver

        _DRIVER_CLASSES = {
            # Kernel drivers
            "ar9271": AR9271Driver,
            "rt2500usb": RT2500USBDriver,
            "rt2800usb": RT2800USBDriver,
            "rt3070": RT3070Driver,
            "rt5370": RT5370Driver,
            "rt5372": RT5372Driver,
            "rtl8187": RTL8187Driver,
            "mt76x0u": MT76x0UDriver,
            "mt76x2u": MT76x2UDriver,
            "mt7921au": MT7921AUDriver,

            # Kernel + DKMS drivers
            "rtl8188eus": env_or_none(ENV_RTL8188_DRIVER, "mainline", RTL8188EUSDriver) or Rtl8188eusDkmsDriver,
            "rtl8812au":  env_or_none(ENV_RTL8812_DRIVER, "mainline", RTL8812AUDriver) or Rtl8812auDkmsDriver,
            "rtl8821au":  env_or_none(ENV_RTL8821_DRIVER, "mainline", RTL8821AUDriver) or Rtl8821auDkmsDriver,
            "rtl8821cu": Rtl8821cuDkmsDriver,
            "rtl8814au":  env_or_none(ENV_RTL8814_DRIVER, "mainline", RTL8814AUDriver) or Rtl8814auDkmsDriver,
            "rtl8822bu":  env_or_none(ENV_RTL8822_DRIVER, "mainline", RTL8822BUDriver) or Rtl8822buDkmsDriver,
        }
    return _DRIVER_CLASSES


def env_or_none(key, value, driver):
    """Returns given driver when env `$key` == `value` (case-insensitive), None otherwise."""
    if os.environ.get(key, "").strip().lower() == value.lower():
        return driver
    return None


def _match_driver(dev: usb.core.Device) -> Optional[tuple[Type[WlanDriver], DeviceID]]:
    """Find the first registered driver that claims `dev`."""
    for entry, driver_cls in _import_driver_classes().items():
        for entry in driver_cls.SUPPORTED_IDS:
            if entry.vid == dev.idVendor and entry.pid == dev.idProduct:
                return driver_cls, entry
    return None


def _is_openable(dev: usb.core.Device) -> bool:
    # Tier-0 probe: can libusb actually OPEN this device, or is it present-but-unbound?
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


def _raise_usblib_fatal(cause: Exception) -> NoReturn:
    # pyusb's NoBackendError is opaque. libusb ships with wifit3 (libusb_package), so the only way
    # it fails to load is a missing OS dependency — on Linux almost always libudev; on Windows/macOS
    # the bundled lib should always load, so it points at a broken install. Turn it into an
    # actionable fatal error the splash surfaces in a modal.
    if sys.platform.startswith("linux"):
        message = (
            "The bundled libusb could not be loaded — a system dependency is missing.\n\n"
            "Install it for your architecture and replug the card:\n"
            "  Debian / Ubuntu / Kali:   sudo apt install libudev1\n"
            "  Fedora / RHEL:            sudo dnf install systemd-libs\n"
            "  Arch:                     sudo pacman -S systemd-libs")
    else:
        message = (
            "The bundled libusb failed to initialize. Reinstall wifit3 — the install is likely "
            "corrupt or being blocked by security software.")
    raise WifiteFatalError("USB backend unavailable", message) from cause


def _scan_bus(backend) -> List[tuple]:
    # Blocking bus scan: ``(dev, driver_cls, id_entry)`` for every supported VID:PID match.
    out: List[tuple] = []
    try:
        for dev in usb.core.find(find_all=True, backend=backend):
            match = _match_driver(dev)
            if match is not None:
                out.append((dev, match[0], match[1]))
    except usb.core.NoBackendError as exc:
        # No usable libusb backend (its OS glue is missing) — fatal, the app can't run.
        _raise_usblib_fatal(exc)
    return out


class WlanDeviceManager:
    """Scans PyUSB, dispatches to driver factories, returns WlanInterfaces."""

    def __init__(self) -> None:
        self.interfaces: List[WlanInterface] = []
        self._dev_sig: Optional[list] = None

    async def refresh(self) -> List[WlanInterface]:
        # Discover supported cards by VID:PID.
        backend = libusb_package.get_libusb1_backend()
        matches = await asyncio.to_thread(_scan_bus, backend)

        # If the same supported cards are still present, keep the live interfaces rather than
        # tearing them down and rebuilding every poll (that churns USB handles + the channel
        # hopper for nothing — the `RTL8187 driver closed` spam). A replug (new bus address) or
        # a different set forces a rebuild.
        sig = sorted((ent.vid, ent.pid, getattr(dev, "address", 0)) for dev, _, ent in matches)
        if self.interfaces and sig == self._dev_sig:
            return self.interfaces
        self._dev_sig = sig

        for iface in self.interfaces:
            await iface.close()
        self.interfaces = []
        for dev, driver_cls, id_entry in matches:
            try:
                driver = driver_cls.from_usb_device(dev, id_entry)
            except Exception as e:
                # A not-yet-ported placeholder
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
        # Can libusb open the card backing ``iface``?
        return iface.dev is None or _is_openable(iface.dev)

    def usb_node_path(self, iface: WlanInterface) -> Optional[str]:
        # The card's usbfs node ``/dev/bus/usb/BBB/DDD``, or None if there's no device handle.
        if iface.dev is None:
            return None
        return f"/dev/bus/usb/{iface.dev.bus:03d}/{iface.dev.address:03d}"

    def linux_needs_permission(self, iface: WlanInterface) -> bool:
        # Linux: is the card's usbfs node NOT writable by us, so open + detach would fail?
        node = self.usb_node_path(iface)
        if not sys.platform.startswith("linux") or node is None:
            return False
        try:
            return not os.access(node, os.W_OK)
        except OSError:
            return True

    async def linux_wait_for_access(self, iface: WlanInterface, *, want_writable: bool,
                                    timeout: float = 5.0, interval: float = 0.1) -> bool:
        # Block until the card's usbfs node reaches ``want_writable`` (or ``timeout`` elapses).
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            writable = not self.linux_needs_permission(iface)
            if writable == want_writable:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(interval)

    async def close_all(self) -> None:
        for iface in self.interfaces:
            await iface.close()
        self.interfaces = []
