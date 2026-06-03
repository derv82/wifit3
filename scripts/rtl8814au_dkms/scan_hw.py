"""RTL8814AU (vendor/DKMS port) — live monitor-RX scan / beacon count.

Drives the DKMS driver DIRECTLY (like ``test_hw.py``), bypassing the device
manager, because this port is intentionally not registered in ``wlan/manager.py``
yet (master keeps the mainline ``rtw88_8814au``). Brings the card up through
M3b-3a, registers a beacon-tallying rx callback, hops 2.4 GHz channels (or sits on
one), and reports the breadth headline — unique BSSIDs (nAPs) and per-BSSID beacon
counts — that the re-port is judged on (DKMS ~21-24 APs vs mainline's noisy 1-11).

Usage (card plugged in; on Linux unbind the kernel driver, on Windows Zadig/WinUSB):
    .venv\\Scripts\\python.exe scripts\\rtl8814au_dkms\\scan_hw.py                 # hop 1-13, 30s
    .venv\\Scripts\\python.exe scripts\\rtl8814au_dkms\\scan_hw.py --channel 1      # fixed ch1
    .venv\\Scripts\\python.exe scripts\\rtl8814au_dkms\\scan_hw.py --duration 60 --debug

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

from wifit3.chips.rtl8814au_dkms.driver import Rtl8814auDkmsDriver


class BeaconTally:
    """Driver-level rx callback: receives parsed frame dicts, counts beacons and
    tracks the strongest RSSI seen per BSSID (to sanity-check the M3b-3b decode)."""

    def __init__(self) -> None:
        self.by_bssid: Counter = Counter()
        self.rssi: dict = {}        # bssid -> strongest (max) dBm seen
        self.total_frames = 0

    def __call__(self, parsed: dict) -> None:
        self.total_frames += 1
        if parsed.get("type") == "beacon":
            bssid = (parsed.get("bssid") or "").lower()
            if bssid and bssid != "ff:ff:ff:ff:ff:ff":
                self.by_bssid[bssid] += 1
                # Track the strongest (max) real dBm; skip the 0 "unknown" sentinel
                # (frames with no PHY status) so it can't mask a real negative value.
                r = parsed.get("rssi")
                if r and (bssid not in self.rssi or r > self.rssi[bssid]):
                    self.rssi[bssid] = r


async def run(args) -> int:
    entry = Rtl8814auDkmsDriver.SUPPORTED_IDS[0]
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=entry.vid, idProduct=entry.pid, backend=backend)
    if dev is None:
        print(f"[FAIL] no {entry.vid:04x}:{entry.pid:04x} on the USB bus")
        return 1
    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        logging.debug("set_configuration: %s", e)

    driver = Rtl8814auDkmsDriver.from_usb_device(dev, entry)
    tally = BeaconTally()
    driver.register_rx_callback(tally)

    def progress(pct, msg):
        print(f"  [{pct * 100:5.1f}%] {msg}")

    if not await driver.connect(progress):
        print("[FAIL] bring-up did not reach FW-ready")
        return 1

    channels = [args.channel] if args.channel else list(range(1, 14))
    print(f"\n[*] scanning {'ch ' + str(args.channel) if args.channel else 'channels 1-13'} "
          f"for {args.duration:g}s ...")
    start = time.monotonic()
    i = 0
    try:
        while time.monotonic() - start < args.duration:
            await driver.set_channel(channels[i % len(channels)])
            i += 1
            await asyncio.sleep(args.dwell)
            print(f"\r  {time.monotonic() - start:4.0f}s  "
                  f"nAPs={len(tally.by_bssid)}  beacons={sum(tally.by_bssid.values())}  "
                  f"frames={tally.total_frames}", end="")
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
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=30.0, help="scan window (s)")
    ap.add_argument("--channel", type=int, default=None,
                    help="fix on this 2.4G channel (default: hop 1-13)")
    ap.add_argument("--dwell", type=float, default=2.0, help="per-channel dwell (s)")
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
