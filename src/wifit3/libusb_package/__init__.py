import os
import usb.backend.libusb1

def find_library():
    # Стандартный путь к библиотеке в Termux
    termux_lib = "/data/data/com.termux/files/usr/lib/libusb-1.0.so"
    if os.path.exists(termux_lib):
        return termux_lib
    return "libusb-1.0.so"

def get_library_path():
    return find_library()

# Именно эту функцию ищет wifit3 внутри device/manager.py
def get_libusb1_backend():
    return usb.backend.libusb1.get_backend(find_library=find_library)