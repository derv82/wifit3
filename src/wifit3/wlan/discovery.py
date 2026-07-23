"""USB-bus scan + driver dispatch: which supported cards are present, and building their interfaces.

Discovery only. Bringing a card up (connect, firmware, permissions) is the bring-up engine's job
(``wifit3.wlan.bringup``); privileged setup is ``wifit3.setup``.
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

from wifit3.chips.driver import DeviceID, Driver
from wifit3.errors import WifiteFatalError

from .interface import WlanInterface

logger = logging.getLogger(__name__)


ENV_RTL8814_DRIVER = "WIFIT3_RTL8814"
ENV_RTL8821_DRIVER = "WIFIT3_RTL8821"
ENV_RTL8812_DRIVER = "WIFIT3_RTL8812"
ENV_RTL8188_DRIVER = "WIFIT3_RTL8188"
ENV_RTL8822_DRIVER = "WIFIT3_RTL8822"

_DRIVER_CLASSES: Dict[str, Type[Driver]] | None = None


def _import_driver_classes() -> Dict[str, Type[Driver]]:
    """Import every driver class once (deferred to sidestep the import cycle)."""
    global _DRIVER_CLASSES
    if _DRIVER_CLASSES is None:
        from wifit3.chips.ar9271_v2.driver import AR9271V2Driver
        from wifit3.chips.mt76x0u.driver import MT76x0UDriver
        from wifit3.chips.mt76x2u.driver import MT76x2UDriver
        from wifit3.chips.mt7921au.driver import MT7921AUDriver
        from wifit3.chips.rt2500usb.driver import RT2500USBDriver
        from wifit3.chips.rt2800usb.driver import RT2800USBDriver
        from wifit3.chips.rt3070.driver import RT3070Driver
        from wifit3.chips.rt5370.driver import RT5370Driver
        from wifit3.chips.rt5372.driver import RT5372Driver
        from wifit3.chips.rt5572.driver import RT5572Driver
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
            "ar9271": AR9271V2Driver,
            "rt2500usb": RT2500USBDriver,
            "rt2800usb": RT2800USBDriver,
            "rt3070": RT3070Driver,
            "rt5370": RT5370Driver,
            "rt5372": RT5372Driver,
            "rt5572": RT5572Driver,
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


def _match_driver(dev: usb.core.Device) -> Optional[tuple[Type[Driver], DeviceID]]:
    """Find the first registered driver that claims `dev`."""
    for driver_cls in _import_driver_classes().values():
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
        return False          # busy / access-denied, not ready to drive either way
    finally:
        # get_active_configuration() opens a libusb handle to read the descriptor
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass


def _raise_usblib_fatal(cause: Exception) -> NoReturn:
    # pyusb's NoBackendError is opaque.
    if sys.platform.startswith("linux"):
        message = (
            "The bundled libusb could not be loaded. A system dependency is missing.\n\n"
            "Install it for your architecture and replug the card:\n"
            "  Debian / Ubuntu / Kali:   sudo apt install libudev1\n"
            "  Fedora / RHEL:            sudo dnf install systemd-libs\n"
            "  Arch:                     sudo pacman -S systemd-libs")
    else:
        message = (
            "The bundled libusb failed to initialize. Reinstall wifit3: the install is likely "
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
        _raise_usblib_fatal(exc)
    return out


def find_devices() -> List[DeviceID]:
    """Every supported card present on the USB bus right now, one DeviceID per physical match."""
    backend = libusb_package.get_libusb1_backend()
    return [entry for _dev, _cls, entry in _scan_bus(backend)]


def build_interface(device_id: DeviceID, name: str = "wlan0") -> Optional[WlanInterface]:
    """The (unconnected) WlanInterface for the present card matching ``device_id``'s VID:PID, or None
    if none is on the bus or its driver can't be constructed. ``iface.connect()`` opens it, not this."""
    backend = libusb_package.get_libusb1_backend()
    for dev, driver_cls, entry in _scan_bus(backend):
        if entry.vid != device_id.vid or entry.pid != device_id.pid:
            continue
        try:
            driver = driver_cls.from_usb_device(dev, entry)
        except Exception as e:
            logger.debug("build_interface: %04x:%04x (%s): %s",
                         entry.vid, entry.pid, driver_cls.__name__, e)
            return None
        return WlanInterface(driver, name, entry.description,
                             vid=entry.vid, pid=entry.pid, dev=dev)
    return None


def build_interfaces() -> List[WlanInterface]:
    """Every present supported card as an (unconnected) WlanInterface, named wlan0..N. A one-shot
    convenience for the dev scripts; the app builds one card at a time through the bring-up engine."""
    backend = libusb_package.get_libusb1_backend()
    out: List[WlanInterface] = []
    for dev, driver_cls, entry in _scan_bus(backend):
        try:
            driver = driver_cls.from_usb_device(dev, entry)
        except Exception as e:
            logger.debug("build_interfaces: %04x:%04x (%s): %s",
                         entry.vid, entry.pid, driver_cls.__name__, e)
            continue
        out.append(WlanInterface(driver, f"wlan{len(out)}", entry.description,
                                 vid=entry.vid, pid=entry.pid, dev=dev))
    return out


async def close_interfaces(ifaces) -> None:
    """Close every interface from build_interfaces(), tolerating a per-card close fault."""
    for iface in ifaces:
        try:
            await iface.close()
        except Exception:
            logger.debug("close_interfaces: %s close failed", getattr(iface, "name", "?"))


def usb_node_path(device_id: DeviceID) -> Optional[str]:
    """The usbfs node ``/dev/bus/usb/BBB/DDD`` of the present card matching ``device_id`` (Linux),
    or None if it isn't on the bus. The path the udev rule / chgrp acts on."""
    backend = libusb_package.get_libusb1_backend()
    for dev, _cls, entry in _scan_bus(backend):
        if entry.vid == device_id.vid and entry.pid == device_id.pid:
            return f"/dev/bus/usb/{dev.bus:03d}/{dev.address:03d}"
    return None


async def wait_for_presence(vid: int, pid: int, *, present: bool,
                            timeout: float = 120.0, interval: float = 0.3) -> bool:
    """Block until the card ``vid:pid`` is present (or absent) on the bus, or ``timeout`` elapses.
    Polls the cheap enumeration only (no interface build/teardown), so it never disturbs live cards."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        devs = await asyncio.to_thread(find_devices)
        here = any(d.vid == vid and d.pid == pid for d in devs)
        if here == present:
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(interval)
