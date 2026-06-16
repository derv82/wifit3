"""Passive hop-timing diagnostic — why no 2.4 GHz APs until 5 GHz, and the ~3 s startup.

Measures three things on real hardware, all passive (no TX):
  1. connect() cold-bring-up wall time + USB control-transfer count (the ~3 s startup).
  2. set_channel_bw per-hop wall time + transfer count for representative channels.
  3. Beacons attributed per channel under the REAL scanner hop loop (0.25 s) vs a
     long-dwell control (1.0 s) vs fixed-channel — to localise the "2.4 GHz silent
     while hopping but fine fixed-channel" symptom.

Run: uv run python scripts/rtl8822bu_dkms/hop_timing.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8822bu_dkms.driver import Rtl8822buDkmsDriver
from wifit3.wlan.channels import scan_hop_order


class CountingCtrl:
    """Wraps dev.ctrl_transfer + dev.write to count/time USB transfers per phase."""
    def __init__(self, dev):
        self.dev = dev
        self._orig_ctrl = dev.ctrl_transfer
        self._orig_write = dev.write
        self.ctrl = 0
        self.bulk = 0
        dev.ctrl_transfer = self._ctrl
        dev.write = self._write

    def _ctrl(self, *a, **k):
        self.ctrl += 1
        return self._orig_ctrl(*a, **k)

    def _write(self, *a, **k):
        self.bulk += 1
        return self._orig_write(*a, **k)

    def snap(self):
        return self.ctrl, self.bulk


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

    counter = CountingCtrl(dev)
    driver = Rtl8822buDkmsDriver.from_usb_device(dev, entry)

    # ---- per-channel beacon attribution -------------------------------------
    bcn_by_ch: Counter = Counter()
    frm_by_ch: Counter = Counter()
    bssids_by_ch: dict[int, set] = defaultdict(set)

    def cb(parsed: dict) -> None:
        ch = driver._channel or 0
        frm_by_ch[ch] += 1
        if parsed.get("type") == "beacon":
            bcn_by_ch[ch] += 1
            b = (parsed.get("bssid") or "").lower()
            if b:
                bssids_by_ch[ch].add(b)

    driver.register_rx_callback(cb)

    # ---- 1. cold bring-up timing --------------------------------------------
    t0 = time.perf_counter()
    c0 = counter.snap()
    ok = await driver.connect(lambda p, m: None)
    dt = time.perf_counter() - t0
    c1 = counter.snap()
    if not ok:
        print("[FAIL] connect")
        return 1
    print("=" * 70)
    print(f"[1] COLD BRING-UP: {dt:.2f}s wall  |  {c1[0]-c0[0]} ctrl xfers, "
          f"{c1[1]-c0[1]} bulk-OUT  ({(c1[0]-c0[0])/dt:.0f} ctrl/s)")

    # ---- 2. per-hop tune timing ---------------------------------------------
    print("=" * 70)
    print("[2] PER-HOP set_channel_bw time (the airodump hop primitive):")
    # walk a band-crossing order so we time both within-band and crossing hops
    for ch in [1, 6, 11, 1, 36, 40, 6]:
        cb0 = counter.snap()
        t = time.perf_counter()
        await driver.set_channel(ch, scan=True)
        d = time.perf_counter() - t
        cb1 = counter.snap()
        print(f"    ch{ch:>3}: {d*1000:6.0f} ms  ({cb1[0]-cb0[0]:>4} ctrl xfers)")

    # ---- 3. beacons per channel: real 0.25 s hop vs 1.0 s dwell -------------
    async def hoploop(channels, interval, secs, label):
        bcn_by_ch.clear(); frm_by_ch.clear()
        for c in list(bssids_by_ch):
            bssids_by_ch[c].clear()
        print("=" * 70)
        print(f"[3:{label}] hop {channels} @ interval={interval}s for {secs}s ...")
        import itertools
        cyc = itertools.cycle(channels)
        last = None
        end = time.perf_counter() + secs
        while time.perf_counter() < end:
            ch = next(cyc)
            if ch != last:
                await driver.set_channel(ch, scan=True)
                last = ch
            await asyncio.sleep(interval)
        # report
        chs = sorted(set(channels))
        for c in chs:
            band = "2.4" if c <= 14 else "5  "
            print(f"    ch{c:>3} [{band}GHz]  beacons={bcn_by_ch[c]:>4}  "
                  f"frames={frm_by_ch[c]:>5}  APs={len(bssids_by_ch[c])}")
        tot2 = sum(bcn_by_ch[c] for c in chs if c <= 14)
        tot5 = sum(bcn_by_ch[c] for c in chs if c > 14)
        print(f"    => 2.4 GHz beacons={tot2}   5 GHz beacons={tot5}")

    full = scan_hop_order(driver.SUPPORTED_CHANNELS)
    # Trim 5 GHz to the non-DFS lower set so the loop cycles fast enough to be fair
    full = [c for c in full if c <= 14 or c in (36, 40, 44, 48, 149, 153, 157, 161)]

    await hoploop(full, 0.25, args.secs, "REAL-0.25s")
    await hoploop(full, 1.0, args.secs, "SLOW-1.0s")
    await hoploop([1, 6, 11], 0.25, args.secs, "2G-ONLY-0.25s")

    await driver.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=20.0, help="seconds per hop-loop phase")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
