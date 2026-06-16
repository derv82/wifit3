"""Cold-boot soak: quantify the intermittent 2.4 GHz-deaf rate (no physical replug needed).

Each driver.connect() runs a full cold cycle (card_dis_flow OFF->ON), so the intermittent
cold-state RF wedge recurs across repeated runs (~40% observed). This loops connect -> tune ch1
-> listen -> close N times and reports the deaf rate + the RF18 read/final per run, turning the
"ughhh intermittent, can't tell if it's fixed" problem into a measurable number. Passive (no TX).

Run: uv run python scripts/rtl8822bu_dkms/soak_2g.py --runs 20 --listen 1.5
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8822bu_dkms.driver import Rtl8822buDkmsDriver


async def one_run(dev, entry, listen: float) -> tuple[int, int]:
    driver = Rtl8822buDkmsDriver.from_usb_device(dev, entry)
    beacons = Counter()

    def cb(parsed: dict) -> None:
        if parsed.get("type") == "beacon":
            beacons["n"] += 1

    driver.register_rx_callback(cb)
    if not await driver.connect(lambda p, m: None):
        await driver.close()
        return -1, 0
    await driver.set_channel(1)
    await asyncio.sleep(listen)
    n = beacons["n"]
    final = driver._dbg_frames  # frames seen during the ch1 dwell window
    await driver.close()
    return n, final


async def run(args) -> int:
    entry = Rtl8822buDkmsDriver.SUPPORTED_IDS[0]
    backend = libusb_package.get_libusb1_backend()
    deaf = 0
    ok = 0
    for i in range(args.runs):
        dev = usb.core.find(idVendor=entry.vid, idProduct=entry.pid, backend=backend)
        if dev is None:
            print(f"  run {i+1:>2}: no device on bus")
            await asyncio.sleep(1.0)
            continue
        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass
        try:
            beacons, frames = await one_run(dev, entry, args.listen)
        except Exception as e:  # noqa: BLE001
            print(f"  run {i+1:>2}: ERROR {e}")
            continue
        status = "DEAF " if beacons == 0 else "ok   "
        if beacons == 0:
            deaf += 1
        else:
            ok += 1
        print(f"  run {i+1:>2}: {status} ch1 beacons={beacons:>4}")
        await asyncio.sleep(0.3)
    total = deaf + ok
    print(f"\n[RESULT] {deaf}/{total} cold boots were 2.4 GHz-DEAF "
          f"({100*deaf/total:.0f}%)" if total else "[RESULT] no runs")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--listen", type=float, default=1.5)
    args = ap.parse_args()
    logging.basicConfig(level=logging.ERROR)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
