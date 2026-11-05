"""The USB backend interface: one libusb context, bundled or system-provided."""
from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from typing import List, Optional

import usb.core

logger = logging.getLogger(__name__)

try:
    import libusb_package
except ImportError:
    libusb_package = None

_desktop_lock = threading.Lock()
_desktop_backend = None


class UsbBackend(ABC):
    """Enumerate and locate USB devices for one platform."""

    @abstractmethod
    def enumerate(self) -> List[usb.core.Device]:
        """Every device the platform currently exposes."""

    @abstractmethod
    def find(self, vid: int, pid: int, bus: Optional[int] = None,
             address: Optional[int] = None) -> Optional[usb.core.Device]:
        """One matching device, or None. A (bus, address) pair selects an exact instance."""

    @abstractmethod
    def find_all(self, vid: int, pid: int) -> List[usb.core.Device]:
        """Every device with this VID:PID."""

    def node_path(self, dev: usb.core.Device) -> Optional[str]:
        """The usbfs node for ``dev`` where the platform has one (desktop Linux)."""
        return None


class LibusbBackend(UsbBackend):
    """Desktop: one libusb context, either bundled or system-provided."""

    def __init__(self, backend) -> None:
        self._backend = backend

    def enumerate(self) -> List[usb.core.Device]:
        return list(usb.core.find(find_all=True, backend=self._backend) or [])

    def find(self, vid: int, pid: int, bus: Optional[int] = None,
             address: Optional[int] = None) -> Optional[usb.core.Device]:
        extra = {"bus": bus, "address": address} if bus is not None and address is not None else {}
        return usb.core.find(idVendor=vid, idProduct=pid, backend=self._backend, **extra)

    def find_all(self, vid: int, pid: int) -> List[usb.core.Device]:
        return list(usb.core.find(find_all=True, idVendor=vid, idProduct=pid,
                                  backend=self._backend) or [])

    def node_path(self, dev: usb.core.Device) -> Optional[str]:
        bus, address = getattr(dev, "bus", None), getattr(dev, "address", None)
        if bus is None or address is None:
            return None
        return f"/dev/bus/usb/{bus:03d}/{address:03d}"


def usb_backend() -> Optional[UsbBackend]:
    """The libusb backend, or None when libusb cannot be loaded."""
    low_level = _desktop_libusb()
    if low_level is None:
        return None
    return LibusbBackend(low_level)


def _desktop_libusb():
    global _desktop_backend
    with _desktop_lock:
        if libusb_package is not None:
            return libusb_package.get_libusb1_backend()
        if _desktop_backend is None:
            from usb.backend import libusb1
            _desktop_backend = libusb1.get_backend()
            if _desktop_backend is None:
                logger.error("system libusb not found")
        return _desktop_backend
