"""USB link-speed diagnostic. Prints the negotiated bus speed for every device."""
import sys

import libusb_package
import usb.core

SPEED = {
    0: "UNKNOWN",
    1: "LOW    (1.5M  / USB1.0)",
    2: "FULL   (12M   / USB1.1)",
    3: "HIGH   (480M  / USB2.0)",
    4: "SUPER  (5G    / USB3.0)",
    5: "SUPER+ (10G   / USB3.1)",
}


def main() -> int:
    backend = libusb_package.get_libusb1_backend()
    devs = list(usb.core.find(find_all=True, backend=backend))
    if not devs:
        print("No USB devices visible to libusb.")
        return 1
    for dev in sorted(devs, key=lambda d: (d.bus, d.address)):
        try:
            spd = dev.speed
        except Exception:
            spd = 0
        try:
            bcd = dev.bcdUSB
        except Exception:
            bcd = 0
        print(f"  {dev.idVendor:04x}:{dev.idProduct:04x}  bus={dev.bus} addr={dev.address:>3}"
              f"  speed={SPEED.get(spd, f'? ({spd})'):<22}  bcdUSB={bcd:#06x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
