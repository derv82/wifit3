import usb.core
import usb.util
import libusb_package
import struct

def probe():
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=0x148f, idProduct=0x5572, backend=backend)
    
    if not dev:
        print("Device not found!")
        return

    print(f"Found RT5572: {dev.bus}:{dev.address}")
    
    def read_reg(reg):
        try:
            # bRequest 7 (MULTI_READ), wIndex is register
            res = dev.ctrl_transfer(0xc0, 0x07, 0, reg, 4)
            return struct.unpack("<I", res)[0]
        except Exception as e:
            return f"Error: {e}"

    def read_mode():
        try:
            # bRequest 1 (DEVICE_MODE), wValue 0x11 (AUTORUN)
            res = dev.ctrl_transfer(0xc0, 0x01, 0x11, 0, 4)
            return struct.unpack("<I", res)[0]
        except Exception as e:
            return f"Error: {e}"

    print("-" * 30)
    print(f"PBF_SYS_CTRL (0x0400): {hex(read_reg(0x0400)) if isinstance(read_reg(0x0400), int) else read_reg(0x0400)}")
    print(f"ASIC_VER_ID  (0x1010): {hex(read_reg(0x1010)) if isinstance(read_reg(0x1010), int) else read_reg(0x1010)}")
    print(f"FW_MODE (Req1 V0x11): {hex(read_mode()) if isinstance(read_mode(), int) else read_mode()}")
    print("-" * 30)

if __name__ == "__main__":
    probe()
