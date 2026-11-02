"""Windows PnP (cfgmgr32) enumeration of present USB devices. libusb can't see a device with no
WinUSB/libusb driver bound, so discovery uses this to also surface a supported card that is present
but driverless (or on a native driver), letting wifit3 offer the WinUSB install for it."""
from __future__ import annotations

import ctypes
import re
from ctypes import wintypes
from typing import Set, Tuple

_CR_SUCCESS = 0
_FILTER = 0x1 | 0x100   # CM_GETIDLIST_FILTER_ENUMERATOR | CM_GETIDLIST_FILTER_PRESENT
_VIDPID = re.compile(r"USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", re.IGNORECASE)


def present_usb_ids() -> Set[Tuple[int, int]]:
    """Every present USB ``(vid, pid)`` per the Windows device tree, regardless of bound driver."""
    cm = ctypes.WinDLL("cfgmgr32")
    size = wintypes.ULONG()
    if cm.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), "USB", _FILTER) != _CR_SUCCESS:
        return set()
    buf = ctypes.create_unicode_buffer(size.value)
    if cm.CM_Get_Device_ID_ListW("USB", buf, size.value, _FILTER) != _CR_SUCCESS:
        return set()
    ids: Set[Tuple[int, int]] = set()
    for instance_id in buf[:].split("\0"):
        match = _VIDPID.search(instance_id)
        if match:
            ids.add((int(match.group(1), 16), int(match.group(2), 16)))
    return ids
