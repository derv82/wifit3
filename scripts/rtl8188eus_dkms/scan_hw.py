"""RTL8188EUS (vendor/DKMS port) — live monitor-RX scan / beacon count.

Drives the DKMS driver DIRECTLY, bypassing the device manager (master keeps the
mainline-derived ``rtl8188eus`` as the default for 2357:010c). Brings the card up through
the monitor opmode entry + RX path, registers a beacon-tallying rx callback, hops 2.4 GHz
channels 1-13, and reports the breadth headline (unique BSSIDs + per-BSSID beacon counts +
strongest RSSI) the re-port is judged on. The DKMS A/B target is to tie/beat the mainline
kernel's ~83% reception (DKMS captures read 86-89%, min 7, no collapse).

Usage (card plugged in; on Windows Zadig/WinUSB-bind 2357:010c, on Linux rmmod r8188eu):
    uv run python scripts/rtl8188eus_dkms/scan_hw.py                 # hop 2.4 GHz, 30s
    uv run python scripts/rtl8188eus_dkms/scan_hw.py --channel 1     # fixed ch1
    uv run python scripts/rtl8188eus_dkms/scan_hw.py --duration 60 --debug

Never prints SSIDs; BSSIDs are this environment's and stay on your terminal only.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8188eus_dkms.driver import Rtl8188eusDkmsDriver


class BeaconTally:
    """rx callback: counts beacons per BSSID, tracks strongest RSSI + ESSID variance."""

    def __init__(self) -> None:
        self.by_bssid: Counter = Counter()
        self.rssi: dict = {}
        self.essids: dict = {}
        self.total_frames = 0

    def __call__(self, parsed: dict) -> None:
        self.total_frames += 1
        if parsed.get("type") == "beacon":
            bssid = (parsed.get("bssid") or "").lower()
            if bssid and bssid != "ff:ff:ff:ff:ff:ff":
                self.by_bssid[bssid] += 1
                r = parsed.get("rssi")
                if r and (bssid not in self.rssi or r > self.rssi[bssid]):
                    self.rssi[bssid] = r
                ssid = parsed.get("ssid")
                if ssid is not None:
                    self.essids.setdefault(bssid, Counter())[ssid] += 1


async def run(args) -> int:
    entry = Rtl8188eusDkmsDriver.SUPPORTED_IDS[0]
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=entry.vid, idProduct=entry.pid, backend=backend)
    if dev is None:
        print(f"[FAIL] no {entry.vid:04x}:{entry.pid:04x} on the USB bus "
              "(plug in + Zadig/WinUSB-bind the card)")
        return 1
    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        logging.debug("set_configuration: %s", e)

    driver = Rtl8188eusDkmsDriver.from_usb_device(dev, entry)
    driver.enable_dig = not args.no_dig   # A/B: isolate the M12 watchdog's effect
    tally = BeaconTally()
    driver.register_rx_callback(tally)

    def progress(pct, msg):
        print(f"  [{pct * 100:5.1f}%] {msg}")

    if not await driver.connect(progress):
        print("[FAIL] bring-up did not reach FW-ready")
        return 1

    channels = [args.channel] if args.channel else [c for c in driver.SUPPORTED_CHANNELS]
    what = f"ch {args.channel}" if args.channel else "2.4 GHz channels 1-13"
    print(f"\n[*] scanning {what} for {args.duration:g}s ...")
    start = time.monotonic()
    i = 0
    try:
        while time.monotonic() - start < args.duration:
            await driver.set_channel(channels[i % len(channels)])
            i += 1
            await asyncio.sleep(args.dwell)
            print(f"\r  {time.monotonic() - start:4.0f}s  nAPs={len(tally.by_bssid)}  "
                  f"beacons={sum(tally.by_bssid.values())}  frames={tally.total_frames}",
                  end="")
    except KeyboardInterrupt:
        pass
    print()
    await driver.close()

    n_aps = len(tally.by_bssid)
    print(f"\n[RESULT] {n_aps} unique AP(s), {sum(tally.by_bssid.values())} beacons, "
          f"{tally.total_frames} total frames over {args.duration:g}s")
    if tally.by_bssid:
        print("  top BSSIDs by beacon count (strongest RSSI seen):")
        for bssid, n in tally.by_bssid.most_common(15):
            print(f"    {bssid}  {n:>4}  {tally.rssi.get(bssid, '?')} dBm")
    else:
        print("  (no beacons — check antenna/channel; this is the live RX gate)")

    variant = {b: c for b, c in tally.essids.items() if len(c) > 1}
    print(f"\n  ESSID-variance canary: {len(variant)} of {len(tally.essids)} beaconing "
          f"BSSID(s) showed >1 distinct ESSID"
          + ("" if not variant else "   <-- INVESTIGATE (possible frame corruption)"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=30.0, help="scan window (s)")
    ap.add_argument("--channel", type=int, default=None, help="fix on this channel")
    ap.add_argument("--dwell", type=float, default=2.0, help="per-channel dwell (s)")
    ap.add_argument("--no-dig", action="store_true",
                    help="disable the M12 DIG watchdog (A/B baseline)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
