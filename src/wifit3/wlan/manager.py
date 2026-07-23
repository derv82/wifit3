"""Legacy discovery facade, retained as a thin shim while consumers migrate to ``wlan.discovery``.

``WlanDeviceManager`` is being retired: its discovery half moved to :mod:`wifit3.wlan.discovery`, its
Linux permission methods to :class:`wifit3.setup.linux.SetupLinux`. Do not add to it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import List, Optional

import libusb_package

from .discovery import (
    ENV_RTL8188_DRIVER,
    ENV_RTL8812_DRIVER,
    ENV_RTL8814_DRIVER,
    ENV_RTL8821_DRIVER,
    ENV_RTL8822_DRIVER,
    _import_driver_classes,
    _is_openable,
    _scan_bus,
)
from .interface import WlanInterface

logger = logging.getLogger(__name__)

# Re-exported so existing importers (setup, scripts, tests, CI) keep resolving these off manager
# until they move to wlan.discovery.
__all__ = [
    "WlanDeviceManager",
    "_import_driver_classes",
    "ENV_RTL8188_DRIVER", "ENV_RTL8812_DRIVER", "ENV_RTL8814_DRIVER",
    "ENV_RTL8821_DRIVER", "ENV_RTL8822_DRIVER",
]


class WlanDeviceManager:
    """Scans PyUSB, dispatches to driver factories, returns WlanInterfaces."""

    def __init__(self) -> None:
        self.interfaces: List[WlanInterface] = []
        self._dev_sig: Optional[list] = None

    async def refresh(self) -> List[WlanInterface]:
        # Discover supported cards by VID:PID.
        backend = libusb_package.get_libusb1_backend()
        matches = await asyncio.to_thread(_scan_bus, backend)

        # If the same supported cards are still present, keep the live interfaces
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

    def linux_kernel_driver_bound(self, iface: WlanInterface) -> bool:
        # Linux: is a real kernel Wi-Fi driver bound to this card?
        if not sys.platform.startswith("linux") or iface.dev is None:
            return False
        from wifit3.setup.linux import kernel_driver_bound
        try:
            return kernel_driver_bound([(iface.vid, iface.pid)])
        except OSError:
            return False

    async def linux_wait_for_access(self, iface: WlanInterface, *, want_writable: bool,
                                    timeout: float = 5.0, interval: float = 0.1) -> bool:
        # Block until the card's usbfs node reaches ``want_writable`` (or ``timeout`` elapses).
        vid, pid = iface.vid, iface.pid
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            await self.refresh()
            current = self.get_interface_by_vidpid(vid, pid)
            if current is not None and (not self.linux_needs_permission(current)) == want_writable:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(interval)

    async def linux_wait_for_presence(self, vid: int, pid: int, *, present: bool,
                                      timeout: float = 120.0, interval: float = 0.3) -> bool:
        # Block until the card ``vid:pid`` is present (or absent) on the bus, or ``timeout`` elapses.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            await self.refresh()
            here = self.get_interface_by_vidpid(vid, pid) is not None
            if here == present:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(interval)

    async def close_all(self) -> None:
        for iface in self.interfaces:
            await iface.close()
        self.interfaces = []
