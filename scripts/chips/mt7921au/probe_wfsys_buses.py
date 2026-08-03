"""
Diagnostic: read the WFSYS-reset registers over all three register buses to
learn empirically which bus reaches each on WinUSB.

The kernel's mt792xu_wfsys_reset drives these over the UHW bus, which errors out
(Errno 5) on WinUSB. The prior session re-mapped epctl_rst_opt (0x74011890) onto
the unified bus and HW-verified it. The reset register CBTOP_RGU (0x70002600)
lives in a different region, so confirm the bus before porting the reset.

Registers (mt792x_regs.h):
  MT_CBTOP_RGU_WF_SUBSYS_RST  0x70002600  (rst_reg, CBTOP region)
  MT_UDMA_CONN_INFRA_STATUS   0x74000a20  (done_reg, UMAC region; BIT22 = INIT_DONE)
  MT_UDMA_CONN_INFRA_STATUS_SEL 0x74000a24
Reference points whose correct bus is already known:
  MT_HW_CHIPID                0x70010200  (standard bus, from cold-boot pcap)
  MT_CONN_ON_MISC             0x7c0600f0  (unified bus, FW_N9_RDY in bits 1:0)
  MT_SSUSB_EPCTL_CSR_EP_RST_OPT 0x74011890 (unified bus, prior session HW-verified)
"""
import sys
import struct
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

VID, PID = 0x0e8d, 0x7961

# (bmRequestType_read, bRequest_read) per bus
BUSES = {
    "standard": (0xC0, 0x63),  # recipient 0  — chip-id bus
    "unified":  (0xDF, 0x63),  # recipient 31 — EXT bus (mt792xu_rr)
    "uhw":      (0xDE, 0x01),  # recipient 30 — UHW bus (mt792xu_uhw_rr)
}

REGS = {
    "MT_HW_CHIPID (0x70010200)":              0x70010200,
    "MT_CONN_ON_MISC (0x7c0600f0)":           0x7c0600f0,
    "MT_SSUSB_EPCTL_EP_RST_OPT (0x74011890)": 0x74011890,
    "MT_CBTOP_RGU_WF_SUBSYS_RST (0x70002600)": 0x70002600,
    "MT_UDMA_CONN_INFRA_STATUS (0x74000a20)": 0x74000a20,
    "MT_UDMA_CONN_INFRA_STATUS_SEL (0x74000a24)": 0x74000a24,
}


def read_reg(dev, bm, breq, addr):
    wValue = (addr >> 16) & 0xFFFF
    wIndex = addr & 0xFFFF
    try:
        res = dev.ctrl_transfer(bmRequestType=bm, bRequest=breq,
                                wValue=wValue, wIndex=wIndex,
                                data_or_wLength=4, timeout=1000)
        if len(res) < 4:
            return f"short({len(res)}B)"
        return f"0x{struct.unpack('<I', res)[0]:08x}"
    except usb.core.USBError as e:
        return f"ERR({e.errno})"


def main():
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        print("MT7921AU not found.")
        sys.exit(1)
    print(f"Found at bus {dev.bus} addr {dev.address}")

    # Claim the vendor-specific (class 0xFF) interface, like the loader does.
    for intf in dev.get_active_configuration():
        if intf.bInterfaceClass == 0xFF:
            try:
                usb.util.claim_interface(dev, intf.bInterfaceNumber)
                print(f"Claimed vendor interface {intf.bInterfaceNumber}")
            except Exception as e:
                print(f"claim failed (continuing): {e}")
            break

    print(f"\n{'register':<46}" + "".join(f"{b:>14}" for b in BUSES))
    for name, addr in REGS.items():
        row = f"{name:<46}"
        for bus, (bm, breq) in BUSES.items():
            row += f"{read_reg(dev, bm, breq, addr):>14}"
        print(row)

    misc = read_reg(dev, 0xDF, 0x63, 0x7c0600f0)
    print(f"\nMT_CONN_ON_MISC (unified) = {misc}")
    if misc.startswith("0x"):
        v = int(misc, 16)
        print(f"  FW_N9_RDY (bits 1:0 == 0x3): "
              f"{'WARM (fw running)' if (v & 0x3) == 0x3 else 'cold / not ready'}")


if __name__ == "__main__":
    main()
