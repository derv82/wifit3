import time
import usb.core
import libusb_package

backend = libusb_package.get_libusb1_backend()

def check_device():
    dev = usb.core.find(idVendor=0x0cf3, idProduct=0x9271, backend=backend)
    if not dev:
        print("Device not found on bus!")
        return
        
    try:
        data = dev.read(0x82, 512, timeout=100)
        print(f"Warm (data read: {len(data)} bytes)")
    except usb.core.USBError as e:
        if e.errno in (10060, 110, 116) or "timeout" in str(e).lower():
            print(f"Warm (timeout, no data) - {e}")
        else:
            print(f"USBError (Other): {e}")

print("Starting 5 polls...")
for i in range(5):
    print(f"Poll {i+1}: ", end="")
    check_device()
    time.sleep(1)
