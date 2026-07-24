"""
Read-only liveness probe for an MT7921AU (PAU0F / AWUS036AXML).

Answers one question: can we SEE the device, enumerate its endpoints, and
issue vendor control reads (register access)? No firmware upload, no bulk OUT,
no 802.11 TX — purely read-side, safe to run any number of times.

Usage: uv run python scripts/mt7921au/probe_pau0f.py
"""
import sys
import struct
import libusb_package
import usb.core
import usb.util

VID, PID = 0x0E8D, 0x7961

SPEED_NAMES = {1: "LOW (1.5 Mbps)", 2: "FULL (12 Mbps, USB 1.1)",
               3: "HIGH (480 Mbps, USB 2.0)", 4: "SUPER (5 Gbps, USB 3.0)",
               5: "SUPER+ (10 Gbps, USB 3.1)"}

# Vendor control read: bmRequestType=0xC0 (IN, vendor, device), bRequest=0x63.
# Full 32-bit reg addr splits across wValue(hi16)/wIndex(lo16).
MT_VEND_READ_REG_REQ = 0x63
MT_VEND_READ_RECIPIENT = 0xDF  # "unified bus" read variant (IN, vendor, other)

# Registers we expect to be readable on a cold device.
MT_HW_CHIPID = 0x70010200   # lower 16 bits = chip id (0x7961)
MT_CONN_ON_MISC = 0x7C0600F0  # MCU power-on status (BIT0) — unified bus


def reg_read(dev, addr, bRequestType=0xC0, bRequest=MT_VEND_READ_REG_REQ, timeout=1000):
    wValue = (addr >> 16) & 0xFFFF
    wIndex = addr & 0xFFFF
    res = dev.ctrl_transfer(bmRequestType=bRequestType, bRequest=bRequest,
                            wValue=wValue, wIndex=wIndex,
                            data_or_wLength=4, timeout=timeout)
    if len(res) < 4:
        return None
    return struct.unpack("<I", bytes(res))[0]


def main():
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        print("[FAIL] MT7921AU (0e8d:7961) not found on the USB bus.")
        return 1

    print("=== Device ===")
    print(f"  bus {dev.bus}  address {dev.address}")
    print(f"  idVendor   0x{dev.idVendor:04x}")
    print(f"  idProduct  0x{dev.idProduct:04x}")
    print(f"  bcdDevice  0x{dev.bcdDevice:04x}   <-- device release number (the 2-byte field)")
    print(f"  bcdUSB     0x{dev.bcdUSB:04x}   (port-dependent, not a model id)")
    speed = getattr(dev, "speed", None)
    print(f"  speed      {speed}  ({SPEED_NAMES.get(speed, 'unknown')})  (port-dependent)")
    print(f"  bDeviceClass    0x{dev.bDeviceClass:02x}  subclass 0x{dev.bDeviceSubClass:02x}  "
          f"proto 0x{dev.bDeviceProtocol:02x}")

    def safe_str(idx):
        if not idx:
            return f"(no string, index {idx})"
        try:
            return repr(usb.util.get_string(dev, idx))
        except Exception as e:
            return f"(unreadable: {type(e).__name__}: {e})"

    print(f"  iManufacturer[{dev.iManufacturer}]  {safe_str(dev.iManufacturer)}")
    print(f"  iProduct[{dev.iProduct}]       {safe_str(dev.iProduct)}")
    print(f"  iSerialNumber[{dev.iSerialNumber}]  {safe_str(dev.iSerialNumber)}")

    print("\n=== Configurations / Interfaces / Endpoints ===")
    try:
        for cfg in dev:
            print(f"  config {cfg.bConfigurationValue}: {cfg.bNumInterfaces} interfaces")
            for intf in cfg:
                cls = intf.bInterfaceClass
                print(f"    if {intf.bInterfaceNumber} alt {intf.bAlternateSetting}: "
                      f"class=0x{cls:02x} sub=0x{intf.bInterfaceSubClass:02x} "
                      f"proto=0x{intf.bInterfaceProtocol:02x}  {intf.bNumEndpoints} eps"
                      f"{'  <-- vendor-specific' if cls == 0xFF else ''}")
                for ep in intf:
                    d = "IN " if usb.util.endpoint_direction(ep.bEndpointAddress) else "OUT"
                    etype = usb.util.endpoint_type(ep.bmAttributes)
                    tname = {0: "ctrl", 1: "iso", 2: "bulk", 3: "intr"}.get(etype, "?")
                    print(f"      EP 0x{ep.bEndpointAddress:02x} {d} {tname:4s} "
                          f"wMaxPacketSize={ep.wMaxPacketSize}")
    except Exception as e:
        print(f"  [WARN] descriptor walk failed: {e}")

    print("\n=== Vendor control reads (the AXML wall was here on WinUSB) ===")
    rc = 0
    # 1. Standard-bus chip id read.
    try:
        v = reg_read(dev, MT_HW_CHIPID, bRequestType=0xC0, bRequest=MT_VEND_READ_REG_REQ)
        chip = (v >> 0) & 0xFFFF if v is not None else None
        if v is None:
            print("  [FAIL] MT_HW_CHIPID read returned <4 bytes")
            rc = 1
        else:
            print(f"  [ OK ] MT_HW_CHIPID (0x{MT_HW_CHIPID:08x}) = 0x{v:08x}  -> chip 0x{chip:04x}")
    except Exception as e:
        print(f"  [FAIL] MT_HW_CHIPID read: {type(e).__name__}: {e}")
        rc = 1

    # 2. Unified-bus MCU power-on status (the read the kernel polls for BIT0).
    try:
        v = reg_read(dev, MT_CONN_ON_MISC, bRequestType=MT_VEND_READ_RECIPIENT,
                     bRequest=MT_VEND_READ_REG_REQ)
        if v is None:
            print("  [WARN] MT_CONN_ON_MISC unified read returned <4 bytes")
        else:
            print(f"  [ OK ] MT_CONN_ON_MISC (0x{MT_CONN_ON_MISC:08x}) = 0x{v:08x}  "
                  f"(BIT0 pwr-on={v & 1})")
    except Exception as e:
        print(f"  [WARN] MT_CONN_ON_MISC unified read: {type(e).__name__}: {e}")

    # 3. Repeat the chip-id read 5x to confirm EP0 stays alive (the AXML symptom
    #    was EP0 going dead after the first transfers).
    print("\n=== EP0 stability (5x chip-id reads) ===")
    fails = 0
    for i in range(5):
        try:
            v = reg_read(dev, MT_HW_CHIPID)
            print(f"  read {i+1}: 0x{v:08x}")
        except Exception as e:
            print(f"  read {i+1}: FAIL {type(e).__name__}: {e}")
            fails += 1
    if fails:
        print(f"  [WARN] {fails}/5 chip-id reads failed — EP0 instability")
        rc = 1

    print("\n=== Verdict ===")
    if rc == 0:
        print("  Device is visible and answering vendor control reads. "
              "Read path works.")
    else:
        print("  Some reads failed — see above.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
