"""ZeroCD (USB mass-storage stub) ejection: switch a card out of its fake-CD-ROM front-end so its
real Wi-Fi VID:PID enumerates. Drivers opt in via ``ZEROCD_IDS``; the eject step is per-platform."""
from __future__ import annotations

import ctypes
import logging
import struct
import sys
import time
from collections import deque
from ctypes import wintypes
from typing import Callable, Iterable, List

import usb.core
import usb.util

logger = logging.getLogger(__name__)

VidPid = tuple[int, int]

_EJECT_TAG = 0x77696669                       # "wifi", the CBW tag we echo back in the status reply
_SCSI_START_STOP_UNIT = bytes([0x1B, 0, 0, 0, 0x02, 0])  # LoEj=1, Start=0: eject the medium
_CSW_LEN = 13
_IO_TIMEOUT_MS = 1000

_MAX_ATTEMPTS = 3            # per physical port, within the window
_WINDOW_S = 60.0            # so a stubborn stub can't be ejected in a tight spin


def _start_stop_unit_cbw() -> bytes:
    """The 31-byte USB Bulk-Only Mass Storage command wrapping SCSI START STOP UNIT (eject)."""
    return struct.pack("<4sIIBBB16s", b"USBC", _EJECT_TAG, 0, 0x00, 0,
                       len(_SCSI_START_STOP_UNIT), _SCSI_START_STOP_UNIT)


def eject(dev: usb.core.Device) -> bool:
    """Eject the ZeroCD stub ``dev`` so it re-enumerates as its Wi-Fi device. True if the command
    was sent. Never raises: a permission/backend failure is logged and reported as False."""
    try:
        if sys.platform == "win32":
            return _eject_windows(dev.idVendor, dev.idProduct)
        return _eject_libusb(dev)
    except (usb.core.USBError, NotImplementedError, OSError) as e:
        logger.info("ZeroCD eject of %04x:%04x failed: %s", dev.idVendor, dev.idProduct, e)
        return False


def _eject_libusb(dev: usb.core.Device) -> bool:
    """Linux / macOS: detach usb-storage from interface 0, send the eject over its bulk pipe."""
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass
    intf = dev.get_active_configuration()[(0, 0)]
    out_ep = usb.util.find_descriptor(
        intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
    in_ep = usb.util.find_descriptor(
        intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
    if out_ep is None or in_ep is None:
        return False
    usb.util.claim_interface(dev, 0)
    try:
        out_ep.write(_start_stop_unit_cbw(), timeout=_IO_TIMEOUT_MS)
        try:
            in_ep.read(_CSW_LEN, timeout=_IO_TIMEOUT_MS)  # status reply; the stub may drop before it
        except usb.core.USBError:
            pass
    finally:
        usb.util.dispose_resources(dev)
    return True


def _port_path(dev: usb.core.Device) -> tuple:
    """A key identifying the physical port, stable across the stub's re-enumerations. Falls back to
    the (re-assigned) address only when the backend can't report the port chain."""
    try:
        ports = tuple(dev.port_numbers or ())
    except (NotImplementedError, AttributeError, usb.core.USBError):
        ports = ()
    return (getattr(dev, "bus", None), ports or (getattr(dev, "address", None),))


class ZeroCdEjector:
    """Ejects present ZeroCD stubs, at most ``_MAX_ATTEMPTS`` per physical port per ``_WINDOW_S`` so a
    device that keeps re-appearing as a stub can't be ejected in a spin. Stateful; one per app."""

    def __init__(self, ids_fn: Callable[[], Iterable[VidPid]],
                 devices_fn: Callable[[], List[usb.core.Device]],
                 *, eject_fn: Callable[[usb.core.Device], bool] = eject,
                 clock: Callable[[], float] | None = None) -> None:
        self._ids_fn = ids_fn
        self._devices_fn = devices_fn
        self._eject_fn = eject_fn
        self._clock = clock or time.monotonic
        self._attempts: dict[tuple, deque[float]] = {}
        self._exhausted: set[tuple] = set()   # ports we've already warned about

    def eject_present(self) -> None:
        """Eject every rate-limit-eligible ZeroCD stub currently on the bus. Never raises."""
        ids = frozenset(self._ids_fn())
        if not ids:
            return
        try:
            devs = self._devices_fn()
        except Exception as e:
            logger.debug("ZeroCD scan skipped: %s", e)
            return
        for dev in devs:
            if (dev.idVendor, dev.idProduct) not in ids or not self._allow(dev):
                continue
            if self._eject_fn(dev):
                logger.info("Ejected ZeroCD stub %04x:%04x", dev.idVendor, dev.idProduct)

    def _allow(self, dev: usb.core.Device) -> bool:
        port = _port_path(dev)
        now = self._clock()
        hist = self._attempts.setdefault(port, deque())
        while hist and now - hist[0] > _WINDOW_S:
            hist.popleft()
        if len(hist) >= _MAX_ATTEMPTS:
            if port not in self._exhausted:
                logger.warning("ZeroCD stub on port %s not switching after %d attempts; giving up",
                               port, _MAX_ATTEMPTS)
                self._exhausted.add(port)
            return False
        hist.append(now)
        self._exhausted.discard(port)
        return True


# --- Windows: resolve VID:PID -> its storage device-interface path via the PnP tree (cfgmgr32),
# --- then eject through it. No drive letter or friendly-name matching; standard-user callable.

_CR_SUCCESS = 0
_CM_GETIDLIST_FILTER_ENUMERATOR = 0x1
_CM_GETIDLIST_FILTER_PRESENT = 0x100
_IOCTL_STORAGE_EJECT_MEDIA = 0x2D4808
_GENERIC_READ = 0x80000000
_FILE_SHARE_RW = 0x3
_OPEN_EXISTING = 3
_INVALID_HANDLE = wintypes.HANDLE(-1).value

# {GUID_DEVINTERFACE_CDROM}, {GUID_DEVINTERFACE_DISK}: a ZeroCD stub is a CD-ROM, but some report a disk.
_STORAGE_INTERFACE_GUIDS = (
    "{53F56308-B6BF-11D0-94F2-00A0C91EFB8B}",
    "{53F56307-B6BF-11D0-94F2-00A0C91EFB8B}",
)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_byte * 8)]


def _guid(text: str) -> _GUID:
    g = _GUID()
    ctypes.WinDLL("ole32").CLSIDFromString(text, ctypes.byref(g))
    return g


def _multi_sz(buf: ctypes.Array) -> List[str]:
    return [s for s in buf[:].split("\0") if s]


def _storage_interface_paths(vid: int, pid: int) -> List[str]:
    cm = ctypes.WinDLL("cfgmgr32")
    flags = _CM_GETIDLIST_FILTER_ENUMERATOR | _CM_GETIDLIST_FILTER_PRESENT
    size = wintypes.ULONG()
    if cm.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), "USB", flags) != _CR_SUCCESS:
        return []
    buf = ctypes.create_unicode_buffer(size.value)
    if cm.CM_Get_Device_ID_ListW("USB", buf, size.value, flags) != _CR_SUCCESS:
        return []
    prefix = f"USB\\VID_{vid:04X}&PID_{pid:04X}\\"
    paths: List[str] = []
    for instance_id in _multi_sz(buf):
        if instance_id.upper().startswith(prefix):
            paths += _child_storage_paths(cm, instance_id)
    return paths


def _child_storage_paths(cm: ctypes.WinDLL, instance_id: str) -> List[str]:
    devnode = wintypes.DWORD()
    if cm.CM_Locate_DevNodeW(ctypes.byref(devnode), instance_id, 0) != _CR_SUCCESS:
        return []
    paths: List[str] = []
    for child_inst in _descendant_instance_ids(cm, devnode.value):
        for guid_text in _STORAGE_INTERFACE_GUIDS:
            paths += _interface_paths(cm, guid_text, child_inst)
    return paths


def _descendant_instance_ids(cm: ctypes.WinDLL, devnode: int) -> List[str]:
    out: List[str] = []
    child = wintypes.DWORD()
    if cm.CM_Get_Child(ctypes.byref(child), devnode, 0) != _CR_SUCCESS:
        return out
    while True:
        out.append(_instance_id(cm, child.value))
        out += _descendant_instance_ids(cm, child.value)
        sibling = wintypes.DWORD()
        if cm.CM_Get_Sibling(ctypes.byref(sibling), child.value, 0) != _CR_SUCCESS:
            return out
        child = sibling


def _instance_id(cm: ctypes.WinDLL, devnode: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    cm.CM_Get_Device_IDW(devnode, buf, 512, 0)
    return buf.value


def _interface_paths(cm: ctypes.WinDLL, guid_text: str, instance_id: str) -> List[str]:
    guid = _guid(guid_text)
    size = wintypes.ULONG()
    if cm.CM_Get_Device_Interface_List_SizeW(
            ctypes.byref(size), ctypes.byref(guid), instance_id, 0) != _CR_SUCCESS:
        return []
    buf = ctypes.create_unicode_buffer(size.value)
    if cm.CM_Get_Device_Interface_ListW(
            ctypes.byref(guid), instance_id, buf, size.value, 0) != _CR_SUCCESS:
        return []
    return _multi_sz(buf)


def _ioctl_eject(path: str) -> bool:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    handle = k32.CreateFileW(path, _GENERIC_READ, _FILE_SHARE_RW, None, _OPEN_EXISTING, 0, None)
    if handle == _INVALID_HANDLE:
        logger.debug("ZeroCD: CreateFileW(%s) failed (%d)", path, ctypes.get_last_error())
        return False
    try:
        returned = wintypes.DWORD()
        ok = k32.DeviceIoControl(handle, _IOCTL_STORAGE_EJECT_MEDIA, None, 0, None, 0,
                                 ctypes.byref(returned), None)
        if not ok:
            logger.debug("ZeroCD: eject IOCTL on %s failed (%d)", path, ctypes.get_last_error())
        return bool(ok)
    finally:
        k32.CloseHandle(handle)


def _eject_windows(vid: int, pid: int) -> bool:
    return any(_ioctl_eject(path) for path in _storage_interface_paths(vid, pid))
