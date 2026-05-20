import usb.core
import usb.util
import libusb_package

def dump_ar9271():
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=0x0cf3, idProduct=0x9271, backend=backend)
    if not dev:
        print("Device not found.")
        return

    print(f"Device: {dev}")
    for cfg in dev:
        print(f"  Config {cfg.bConfigurationValue}")
        for intf in cfg:
            print(f"    Interface {intf.bInterfaceNumber}, Alt {intf.bAlternateSetting}")
            for ep in intf:
                print(f"      Endpoint {hex(ep.bEndpointAddress)}: Attributes={hex(ep.bmAttributes)}")

if __name__ == "__main__":
    dump_ar9271()
