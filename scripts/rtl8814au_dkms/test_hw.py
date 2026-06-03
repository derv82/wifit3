"""RTL8814AU (vendor/DKMS port) — hardware test of the implemented bring-up.

Finds the card and runs `driver.connect()`, which currently does the EFUSE read
(chip params: rfe_type, crystal_cap, MAC), M1 (power-on -> LLT -> 3081/IDDMA
firmware download -> FW-ready), M2a (MAC register table), and M2b (hal_init MISC
stage + PHY_BBConfig8814: BB PHY_REG + AGC_TAB tables, crystal-cap, TRX path), and
checks the chip reached CPU_DL_READY. Standalone vendor port on branch
``dkms/8814au``; does NOT touch the registered mainline driver.

Usage (run from a checkout with the card plugged in):
    .venv\\Scripts\\python.exe scripts\\rtl8814au_dkms\\test_hw.py
    .venv\\Scripts\\python.exe scripts\\rtl8814au_dkms\\test_hw.py --debug

On Linux, unbind the in-kernel driver first (e.g. ``rmmod 8814au``); on Windows the
device must be WinUSB-bound (Zadig). If the bulk pipe wedges, unplug/replug and rerun.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8814au_dkms.driver import Rtl8814auDkmsDriver


def progress(pct: float, msg: str) -> None:
    print(f"  [{pct * 100:5.1f}%] {msg}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    entry = Rtl8814auDkmsDriver.SUPPORTED_IDS[0]
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=entry.vid, idProduct=entry.pid, backend=backend)
    if dev is None:
        print(f"[FAIL] no {entry.vid:04x}:{entry.pid:04x} on the USB bus ({entry.description})")
        return 1
    print(f"[*] Matched {entry.description} ({entry.vid:04x}:{entry.pid:04x})")

    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        # Already configured (typical on Windows/WinUSB) — keep going.
        logging.debug("set_configuration: %s", e)

    driver = Rtl8814auDkmsDriver.from_usb_device(dev, entry)
    ready = asyncio.run(driver.connect(progress))

    if ready:
        print("[PASS] bring-up reached FW-ready (CPU_DL_READY) and applied the "
              "MAC table + MISC stage + PHY_BBConfig8814 (BB/AGC).")
        return 0
    print("[FAIL] firmware download did not reach FW-ready.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
