"""RTL8812AU (DKMS port) — live hardware smoke test of the implemented bring-up.

Passive: control transfers + firmware page-writes only (and monitor RX once M5 lands).
No 802.11 TX/inject. Phases are cumulative; only the milestones implemented so far are
wired.

Phases:
  open : USB claim + REG_SYS_CFG sanity read (no known cold-boot value — no 8812 pcap —
         so it just rejects 0 / 0xFFFFFFFF and prints the chip version).
  fw   : open, then firmware.bring_up (power-on -> LLT -> FW download -> FW-ready),
         checking REG_MCUFWDL ends with WINTINI_RDY set.

Usage (card plugged in, WinUSB-bound via Zadig on Windows):
    uv run python scripts/rtl8812au_dkms/test_hw.py --phase open
    uv run python scripts/rtl8812au_dkms/test_hw.py --phase fw
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

from wifit3.chips.rtl88xxau_base import registers as R
from wifit3.chips.rtl88xxau_base.transport import Rtl88xxauTransport
from wifit3.chips.rtl8812au_dkms import firmware
from wifit3.chips.rtl8812au_dkms.constants import USB_PID_AWUS036ACH, USB_VID_REALTEK


def _fail(msg: str) -> int:
    print(f"[FAIL] {msg}")
    return 1


def _open_device():
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=USB_VID_REALTEK, idProduct=USB_PID_AWUS036ACH, backend=backend)
    if dev is None:
        print(f"[FAIL] AWUS036ACH not found ({USB_VID_REALTEK:04x}:{USB_PID_AWUS036ACH:04x}). "
              "Plug it in, confirm Zadig bound it to WinUSB.")
        return None
    print(f"[*] Found AWUS036ACH at bus {dev.bus}, address {dev.address}")
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass
    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        logging.debug("set_configuration: %s", e)
    return dev


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=("open", "fw"), default="fw")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    dev = _open_device()
    if dev is None:
        return 1
    try:
        usb.util.claim_interface(dev, 0)
    except usb.core.USBError as e:
        return _fail(f"claim_interface(0): {e}  (a running wifit3 may hold the card)")

    t = Rtl88xxauTransport(dev)
    try:
        sys_cfg = t.read32(R.REG_SYS_CFG)
        print(f"  REG_SYS_CFG (0xF0) = 0x{sys_cfg:08x}  (chip version / cut)")
        if sys_cfg in (0, 0xFFFFFFFF):
            return _fail("implausible REG_SYS_CFG — unplug 5s, replug, rerun.")

        if args.phase == "open":
            print("[PASS] control-transfer plumbing works.")
            return 0

        fw = firmware.load_firmware_blob()
        print(f"[*] FW blob {len(fw)} bytes; running bring_up()...")
        ready = firmware.bring_up(t, fw)
        mcu = t.read32(R.REG_MCUFWDL)
        bits = [n for n, b in (("MCUFWDL_RDY", R.MCUFWDL_RDY), ("FWDL_ChkSum_rpt", R.FWDL_ChkSum_rpt),
                               ("WINTINI_RDY", R.WINTINI_RDY), ("RAM_DL_SEL", R.RAM_DL_SEL))
                if mcu & b]
        print(f"  REG_MCUFWDL (0x80) = 0x{mcu:08x}  set: {bits or '(none)'}")
        if not ready:
            return _fail("bring_up did not reach FW-ready (WINTINI_RDY).")
        print("[PASS] FW-ready (WINTINI_RDY) — wlan CPU is running the firmware.")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, 0)
            usb.util.dispose_resources(dev)
        except usb.core.USBError as e:
            print(f"  (release warning: {e})")


if __name__ == "__main__":
    sys.exit(main())
