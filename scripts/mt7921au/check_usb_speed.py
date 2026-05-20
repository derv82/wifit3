"""Probe MT7921AU for USB speed + endpoint details. Run after replug."""
import libusb_package
import usb.core
import usb.util

SPEED_NAMES = {1: "LOW (1.5 Mbps)", 2: "FULL (12 Mbps, USB 1.1)",
               3: "HIGH (480 Mbps, USB 2.0)", 4: "SUPER (5 Gbps, USB 3.0)",
               5: "SUPER+ (10 Gbps, USB 3.1)"}

backend = libusb_package.get_libusb1_backend()
dev = usb.core.find(idVendor=0x0e8d, idProduct=0x7961, backend=backend)
if dev is None:
    print("MT7921AU not found")
    raise SystemExit(1)

print(f"Bus {dev.bus}, Address {dev.address}")
print(f"bcdUSB:  {dev.bcdUSB:04x}  ({'3.x' if dev.bcdUSB >= 0x0300 else '2.x' if dev.bcdUSB >= 0x0200 else '1.x'})")
speed = getattr(dev, "speed", None)
print(f"speed:   {speed}  ({SPEED_NAMES.get(speed, 'unknown')})")
print(f"manufacturer: {usb.util.get_string(dev, dev.iManufacturer)}")
print(f"product:      {usb.util.get_string(dev, dev.iProduct)}")
print()
print("Endpoints on interface 3:")
cfg = dev.get_active_configuration()
intf = cfg[(3, 0)]
for ep in intf:
    direction = "IN " if usb.util.endpoint_direction(ep.bEndpointAddress) else "OUT"
    print(f"  EP 0x{ep.bEndpointAddress:02x} {direction}  type=0x{ep.bmAttributes:02x}  wMaxPacketSize={ep.wMaxPacketSize}")
