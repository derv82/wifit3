"""Passive driver-path RX probe — isolates the live deauth "ap_beacons=0" failure.

live deauth drives the full driver (connect -> RxReaderThread -> enable_monitor -> watchdog) and saw
0 RX frames, while cck_diag (a synchronous bulk_in loop) hears beacons fine. This runs the SAME
driver RX path live deauth uses — connect + the reader thread + the 2 s watchdog — but injects NOTHING
(passive, agent-safe), and tallies frames for `--listen` seconds. It tells us whether the failure is
(a) the driver/reader RX path itself, or (b) the concurrent TX (inject) in live deauth starving RX.

Run: uv run python scripts/chips/rtl8822bu_dkms/driver_rx_probe.py --channel 1 --listen 15
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8822bu_dkms.driver import Rtl8822buDkmsDriver

if TYPE_CHECKING:
    from wifit3.dot11.packet import Packet


async def run(args) -> int:
    entry = Rtl8822buDkmsDriver.SUPPORTED_IDS[0]
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=entry.vid, idProduct=entry.pid, backend=backend)
    if dev is None:
        print(f"[FAIL] no {entry.vid:04x}:{entry.pid:04x} on the bus")
        return 1
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass

    driver = Rtl8822buDkmsDriver.from_usb_device(dev, entry)
    tally = Counter()
    bssids: Counter = Counter()

    def cb(parsed: Packet) -> None:
        tally["frames"] += 1
        t = parsed.type
        if t == "beacon":
            tally["beacons"] += 1
            b = (parsed.bssid or "").lower()
            if b:
                bssids[b] += 1
        if parsed.to_ds and not parsed.from_ds:
            tally["tods"] += 1

    driver.register_rx_callback(cb)
    if not await driver.connect(lambda p, m: print(f"  [{p*100:5.1f}%] {m}")):
        print("[FAIL] connect")
        return 1
    await driver.set_channel(args.channel)
    print(f"[*] PASSIVE listen on ch {args.channel} for {args.listen:g}s (no TX)...")
    for s in range(int(args.listen)):
        await asyncio.sleep(1.0)
        print(f"\r  {s+1:>3}s  frames={tally['frames']}  beacons={tally['beacons']}  "
              f"tods={tally['tods']}  APs={len(bssids)}", end="")
    print()
    await driver.close()
    print(f"[RESULT] {tally['frames']} frames, {tally['beacons']} beacons, {tally['tods']} ToDS, "
          f"{len(bssids)} APs")
    for b, n in bssids.most_common(8):
        print(f"    {b}  {n}")
    if tally["frames"] == 0:
        print("  => driver RX path delivers 0 frames passively -> the bug is the driver/reader path "
              "(not TX contention).")
    else:
        print("  => driver RX path works passively -> live deauth's 0-frames is TX/inject contention.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--listen", type=float, default=15.0)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
