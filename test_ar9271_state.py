import usb.core
import libusb_package
backend = libusb_package.get_libusb1_backend()
dev = usb.core.find(idVendor=0x0cf3, idProduct=0x9271, backend=backend)
if dev:
    try:
        dev.read(0x82, 512, timeout=100)
        print("Warm (data read)")
    except usb.core.USBError as e:
        print(f"USBError: {e}, errno: {e.errno}")
else:
    print("Not found")