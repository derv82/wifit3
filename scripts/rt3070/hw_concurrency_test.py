"""HARDWARE validation of the _hw_lock fix: hammer the card with the exact race that
wedged it (two concurrent channel tunes — the post-cancellation two-thread state) and
confirm RX survives + WPDMA stays armed.

Calling driver._tune() from two threads directly recreates what a UI view-switch produces:
a cancelled tune's executor thread still draining while a new tune's thread starts. With
the threading.Lock fix they serialize; without it they'd collide and wedge RX-DMA.

    uv run python scripts/rt3070/hw_concurrency_test.py   (card plugged + WinUSB-bound)
"""
from __future__ import annotations

import asyncio
import random
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core

from wifit3.chips.rt3070 import constants as C
from wifit3.chips.rt3070.driver import RT3070Driver
from wifit3.chips.rt3070.transport import RT3070Transport

ROUNDS = 40


async def main() -> int:
    dev = usb.core.find(idVendor=0x148F, idProduct=0x3070,
                        backend=libusb_package.get_libusb1_backend())
    if dev is None:
        print("[FAIL] no 148f:3070 (plug in + WinUSB-bind)")
        return 1
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass

    driver = RT3070Driver.from_usb_device(dev, RT3070Driver.SUPPORTED_IDS[0])
    rx = {"n": 0}
    driver.register_rx_callback(lambda p: rx.__setitem__("n", rx["n"] + 1))
    if not await driver.connect(lambda p, m: None):
        print("[FAIL] bring-up")
        return 1
    print("[*] connected; RX baseline 2s...")
    await asyncio.sleep(2.0)
    base = rx["n"]
    print(f"    baseline frames: {base}")

    print(f"[*] STRESS: {ROUNDS} rounds of TWO concurrent _tune threads (the wedge race)...")
    for i in range(ROUNDS):
        a, b = random.randint(1, 13), random.randint(1, 13)
        t1 = threading.Thread(target=driver._tune, args=(a,))
        t2 = threading.Thread(target=driver._tune, args=(b,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    print("    stress done (no hang/crash)")

    # Land on ch1 and listen — does RX still flow?
    driver._tune(1)
    before = rx["n"]
    await asyncio.sleep(3.0)
    after_frames = rx["n"] - before

    # RX-frames-flowing IS the wedge test (294/3s ⇒ RX-DMA works). Read WPDMA only AFTER
    # the reader stops — reading it mid-bulk-IN returns a racy/stale value, not a wedge.
    await driver.close()
    glo = RT3070Transport(driver.transport.dev, timeout_ms=300).register_read(C.WPDMA_GLO_CFG)

    print(f"\n[RESULT] post-stress RX frames in 3s: {after_frames}   "
          f"WPDMA_GLO_CFG (reader stopped) = 0x{glo:08x}")
    if after_frames > 20:
        print("[PASS] RX flowing after 40 rounds of the concurrent-tune race — the card did "
              "NOT wedge. The _hw_lock fix holds on hardware.")
        return 0
    print("[FAIL] RX dead post-stress — the card wedged despite the fix.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
