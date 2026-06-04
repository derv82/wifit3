"""RTL8821AU (DKMS port) — live hardware smoke test of the implemented bring-up.

Passive: control transfers + the firmware page-writes only. No 802.11 TX/inject.

Phases (cumulative):
  open : USB claim + REG_SYS_CFG sanity read (cold-boot ground truth 0x04412135).
  fw   : open, then firmware.bring_up (power-on -> LLT -> FW download -> FW-ready),
         checking REG_MCUFWDL ends with WINTINI_RDY set.

Usage (card plugged in, WinUSB-bound via Zadig on Windows):
    uv run python scripts/rtl8821au_dkms/test_hw.py            # = fw
    uv run python scripts/rtl8821au_dkms/test_hw.py --phase open --debug
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtl8821au_dkms import constants as C
from wifit3.chips.rtl8821au_dkms import bb, chan, firmware, mac, rf
from wifit3.chips.rtl8821au_dkms.transport import RTL8821AUDkmsTransport


def _fail(msg: str) -> int:
    print(f"[FAIL] {msg}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=("open", "fw", "mac", "phy", "chan"), default="chan")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=C.USB_VID_REALTEK, idProduct=C.USB_PID_AWUS036ACS,
                        backend=backend)
    if dev is None:
        return _fail(f"AWUS036ACS not found ({C.USB_VID_REALTEK:04x}:{C.USB_PID_AWUS036ACS:04x}). "
                     "Plug it in, confirm Zadig bound it to WinUSB.")
    print(f"[*] Found AWUS036ACS at bus {dev.bus}, address {dev.address}")
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass
    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        logging.debug("set_configuration: %s", e)
    try:
        usb.util.claim_interface(dev, 0)
    except usb.core.USBError as e:
        return _fail(f"claim_interface(0): {e}")

    t = RTL8821AUDkmsTransport(dev)
    rc = 0
    try:
        sys_cfg = t.read32(C.REG_SYS_CFG)
        print(f"  REG_SYS_CFG (0xF0) = 0x{sys_cfg:08x}  (cold-boot ground truth 0x04412135)")
        if sys_cfg in (0, 0xFFFFFFFF):
            return _fail("implausible REG_SYS_CFG — unplug 5s, replug, rerun.")

        if args.phase == "open":
            print("[PASS] control-transfer plumbing works.")
            return 0

        fw = firmware.load_firmware_blob()
        print(f"[*] FW blob {len(fw)} bytes; running bring_up()...")
        ready = firmware.bring_up(t, fw)
        mcu = t.read32(C.REG_MCUFWDL)
        bits = [n for n, b in (("MCUFWDL_RDY", C.MCUFWDL_RDY), ("FWDL_ChkSum_rpt", C.FWDL_ChkSum_rpt),
                               ("WINTINI_RDY", C.WINTINI_RDY), ("RAM_DL_SEL", C.RAM_DL_SEL))
                if mcu & b]
        print(f"  REG_MCUFWDL (0x80) = 0x{mcu:08x}  set: {bits or '(none)'}")
        if not ready:
            return _fail("bring_up did not reach FW-ready (WINTINI_RDY).")
        print("[PASS] FW-ready (WINTINI_RDY) — wlan CPU is running the firmware.")

        if args.phase in ("mac", "phy", "chan"):
            print("[*] running MAC init (M2)...")
            mac.phy_mac_config(t)
            mac.mac_init_misc(t)
            cr = t.read8(C.REG_CR)
            print(f"  REG_CR (0x100) = 0x{cr:02x}  "
                  f"MACTXEN={bool(cr & mac.MACTXEN)} MACRXEN={bool(cr & mac.MACRXEN)}")
            if not (cr & mac.MACTXEN and cr & mac.MACRXEN):
                return _fail("REG_CR missing MACTXEN|MACRXEN after MAC init.")
            print("[PASS] MAC enabled (REG_CR MACTXEN|MACRXEN).")

        if args.phase in ("phy", "chan"):
            print("[*] running PHY init (M3: BB PHY_REG/AGC + crystal_cap + RadioA)...")
            bb.phy_bb_config(t, crystal_cap=0x27)   # TODO(efuse): read crystal_cap from EFUSE
            rf.phy_rf_config(t)
            xtal = t.read32(0x002C)
            print(f"  REG 0x2C = 0x{xtal:08x}  (xtal field [23:12] = 0x{(xtal >> 12) & 0xFFF:03x}, "
                  f"expect 0x9e7 for crystal_cap 0x27)")
            print("[PASS] PHY (BB + RF) init complete — no bus errors.")

        if args.phase == "chan":
            print("[*] running channel tune (M4: 2.4 GHz band + ch1 + 20 MHz BW)...")
            chan.set_chnl_bw(t, ch=1)
            rf18 = rf._rf_serial_read(t, rf.RF_PATH_A, rf.RF_CHNLBW)
            print(f"  RF[0x18] = 0x{rf18:05x}  (channel/BW reg — ch1 @ 20 MHz)")
            print("[PASS] channel tune complete — no bus errors.")
    finally:
        try:
            usb.util.release_interface(dev, 0)
            usb.util.dispose_resources(dev)
        except usb.core.USBError as e:
            print(f"  (release warning: {e})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
