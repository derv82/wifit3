"""Diagnose the real-driver cold-ch1 RX shortfall: is bit16 actually cleared after connect(),
and is the phydm watchdog (DIG) cranking IGI and killing sensitivity? Reads RF18 + IGI right
after connect() and again after a dwell, optionally cancelling the watchdog. Passive (RX only).

    uv run python scripts/rtl8821cu_dkms/driver_rx_diag.py [seconds] [killwd]
        killwd: pass 'killwd' to cancel the watchdog right after connect
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8821cu_dkms.driver import Rtl8821cuDkmsDriver
from wifit3.chips.rtl8821cu_dkms.rf import read_rf


async def run(secs: float, kill_wd: bool) -> int:
    dev = usb.core.find(idVendor=0x0BDA, idProduct=0xC820,
                        backend=libusb_package.get_libusb1_backend())
    if dev is None:
        print("no 0bda:c820 device")
        return 1
    drv = Rtl8821cuDkmsDriver(dev)
    n = [0]
    drv.register_rx_callback(lambda p: n.__setitem__(0, n[0] + 1))
    await drv.connect()
    t = drv.transport

    rf18 = read_rf(t, 0x18)
    igi0 = t.read8(0x0C50) & 0x7F
    print(f"after connect : RF18={rf18:#07x} bit16={(rf18>>16)&1} ch={rf18&0xFF:#04x} IGI={igi0:#04x}")

    if kill_wd and drv._watchdog_task is not None:
        drv._watchdog_task.cancel()
        print("watchdog CANCELLED")

    # sample delivered-count + IGI every ~3s to watch the sag
    end = time.monotonic() + secs
    last = 0
    while time.monotonic() < end:
        await asyncio.sleep(3)
        igi = t.read8(0x0C50) & 0x7F
        cur = n[0]
        print(f"  +3s: delivered+={cur-last:4d} (total {cur})  IGI={igi:#04x}")
        last = cur

    print(f"TOTAL delivered={n[0]} over {secs:.0f}s ({n[0]/secs:.1f}/s)  watchdog={'OFF' if kill_wd else 'ON'}")
    await drv.close()
    return 0


if __name__ == "__main__":
    s = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
    kw = len(sys.argv) > 2 and sys.argv[2] == "killwd"
    raise SystemExit(asyncio.run(run(s, kw)))
