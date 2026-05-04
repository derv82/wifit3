import ctypes
from ctypes import wintypes
from loguru import logger

# Windows Wireless LAN API constants and structures
class WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [
        ("InterfaceGuid", ctypes.c_byte * 16),
        ("strInterfaceDescription", ctypes.c_wchar * 256),
        ("isState", ctypes.c_uint),
    ]

class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
    _fields_ = [
        ("dwNumberOfItems", ctypes.c_uint),
        ("dwIndex", ctypes.c_uint),
        ("InterfaceInfo", WLAN_INTERFACE_INFO * 1),
    ]

def get_windows_interfaces():
    """Uses WlanAPI.dll to list wireless interfaces on Windows."""
    try:
        wlanapi = ctypes.windll.wlanapi
        handle = wintypes.HANDLE()
        negotiated_version = wintypes.DWORD()
        
        res = wlanapi.WlanOpenHandle(2, None, ctypes.byref(negotiated_version), ctypes.byref(handle))
        if res != 0:
            return []

        interface_list_ptr = ctypes.pointer(WLAN_INTERFACE_INFO_LIST())
        res = wlanapi.WlanEnumInterfaces(handle, None, ctypes.byref(interface_list_ptr))
        if res != 0:
            wlanapi.WlanCloseHandle(handle, None)
            return []

        interfaces = []
        if interface_list_ptr.contents.dwNumberOfItems > 0:
            import uuid
            class ACTUAL_INTERFACE_LIST(ctypes.Structure):
                _fields_ = [
                    ("dwNumberOfItems", ctypes.c_uint),
                    ("dwIndex", ctypes.c_uint),
                    ("InterfaceInfo", WLAN_INTERFACE_INFO * interface_list_ptr.contents.dwNumberOfItems),
                ]
            actual_list = ctypes.cast(interface_list_ptr, ctypes.POINTER(ACTUAL_INTERFACE_LIST))
            for i in range(actual_list.contents.dwNumberOfItems):
                info = actual_list.contents.InterfaceInfo[i]
                # GUIDs in Windows are stored in mixed endianness
                # Using uuid.UUID(bytes_le=...) correctly handles this
                guid_str = str(uuid.UUID(bytes_le=bytes(info.InterfaceGuid))).upper()
                interfaces.append({
                    "description": info.strInterfaceDescription,
                    "guid": f"{{{guid_str}}}"
                })

        wlanapi.WlanFreeMemory(interface_list_ptr)
        wlanapi.WlanCloseHandle(handle, None)
        return interfaces
    except Exception:
        return []
