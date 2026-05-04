import ctypes
from ctypes import wintypes
from loguru import logger

# Windows Wireless LAN API constants and structures
# https://docs.microsoft.com/en-us/windows/win32/api/wlanapi/

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
        
        # WlanOpenHandle
        res = wlanapi.WlanOpenHandle(2, None, ctypes.byref(negotiated_version), ctypes.byref(handle))
        if res != 0:
            logger.error(f"WlanOpenHandle failed with error {res}")
            return []

        # WlanEnumInterfaces
        interface_list_ptr = ctypes.pointer(WLAN_INTERFACE_INFO_LIST())
        res = wlanapi.WlanEnumInterfaces(handle, None, ctypes.byref(interface_list_ptr))
        if res != 0:
            wlanapi.WlanCloseHandle(handle, None)
            logger.error(f"WlanEnumInterfaces failed with error {res}")
            return []

        interfaces = []
        if interface_list_ptr.contents.dwNumberOfItems > 0:
            # Re-cast to get the actual number of items
            class ACTUAL_INTERFACE_LIST(ctypes.Structure):
                _fields_ = [
                    ("dwNumberOfItems", ctypes.c_uint),
                    ("dwIndex", ctypes.c_uint),
                    ("InterfaceInfo", WLAN_INTERFACE_INFO * interface_list_ptr.contents.dwNumberOfItems),
                ]
            actual_list = ctypes.cast(interface_list_ptr, ctypes.POINTER(ACTUAL_INTERFACE_LIST))
            for i in range(actual_list.contents.dwNumberOfItems):
                info = actual_list.contents.InterfaceInfo[i]
                interfaces.append({
                    "description": info.strInterfaceDescription,
                    "guid": bytes(info.InterfaceGuid).hex()
                })

        wlanapi.WlanFreeMemory(interface_list_ptr)
        wlanapi.WlanCloseHandle(handle, None)
        return interfaces
    except Exception as e:
        logger.error(f"Error calling WlanAPI: {e}")
        return []

if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        print("Discovering Windows Interfaces...")
        ifaces = get_windows_interfaces()
        for iface in ifaces:
            print(f"- {iface['description']} (GUID: {iface['guid']})")
    else:
        print("Not on Windows.")
