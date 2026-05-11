import usb.core
import usb.util
import time
import libusb_package

def test_reset():
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=0x0cf3, idProduct=0x9271, backend=backend)
    
    if dev is None:
        print("[-] AR9271 not found.")
        return

    print(f"[+] Found AR9271 at {dev.bus}:{dev.address}")
    
    # Check current state
    cfg = dev.get_active_configuration()
    intf = cfg[(0,0)]
    ep4 = usb.util.find_descriptor(intf, custom_match=lambda e: e.bEndpointAddress == 0x04)
    
    if ep4:
        etype = usb.util.endpoint_type(ep4.bmAttributes)
        state = "COLD (Interrupt)" if etype == usb.util.ENDPOINT_TYPE_INTR else "WARM (Bulk)"
        print(f"[*] Current state: {state}")

    print("[*] Issuing dev.reset()...")
    try:
        dev.reset()
        print("[+] reset() call returned.")
    except Exception as e:
        print(f"[*] reset() threw (Expected if device drops): {e}")

    print("[*] Waiting for re-enumeration...")
    time.sleep(5)
    
    dev = usb.core.find(idVendor=0x0cf3, idProduct=0x9271, backend=backend)
    if dev:
        print(f"[+] Device came back at {dev.bus}:{dev.address}")
        cfg = dev.get_active_configuration()
        intf = cfg[(0,0)]
        ep4 = usb.util.find_descriptor(intf, custom_match=lambda e: e.bEndpointAddress == 0x04)
        if ep4:
            etype = usb.util.endpoint_type(ep4.bmAttributes)
            state = "COLD (Interrupt)" if etype == usb.util.ENDPOINT_TYPE_INTR else "WARM (Bulk)"
            print(f"[*] Post-reset state: {state}")
    else:
        print("[-] Device did not come back.")

if __name__ == "__main__":
    test_reset()
