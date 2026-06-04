"""RTL8814AU A/B scan — DKMS port vs mainline, via the REAL driver-selection path.

Brings the card up through `WlanDeviceManager` (so it honours the `WIFIT3_RTL8814` env
var exactly as the app does), hops a band, and reports the user-visible breadth headline
(unique APs / beacons / RSSI) from `interface.get_access_points()`. The point of the
re-port is 2.4 GHz monitor RX breadth, so that is the default band.

Run BOTH drivers and compare — but **stagger and replug between every run** (a slowly
growing noise floor would otherwise make whichever card measured last look worst, and a
replug guarantees a cold chip with zero warm-state confounds):

    # round 1
    unplug, wait 3s, replug
    uv run scripts/rtl8814au_dkms/ab_scan.py                       # DKMS (default)
    unplug, wait 3s, replug
    $env:WIFIT3_RTL8814='mainline'; uv run scripts/rtl8814au_dkms/ab_scan.py   # mainline
    Remove-Item Env:WIFIT3_RTL8814
    # round 2: same, and so on — compare the DISTRIBUTIONS across rounds, not one number.

Options: --band 2g|5g|all (default 2g) · --duration S · --interval S (per-hop dwell).
Never prints SSIDs; BSSIDs are this environment's and stay on your terminal only.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from wifit3.wlan.manager import ENV_RTL8814_DRIVER, WlanDeviceManager  # noqa: E402

RTL8814_VID, RTL8814_PID = 0x0BDA, 0x8813


def _is_8814(iface) -> bool:
    return any(e.vid == RTL8814_VID and e.pid == RTL8814_PID
               for e in getattr(iface.driver, "SUPPORTED_IDS", []))


def _band_channels(iface, band: str) -> list:
    """The selected driver's supported channels for the band (so we never tune one it
    can't); reported so any DKMS-vs-mainline channel-set difference is visible."""
    chans = list(getattr(iface.driver, "SUPPORTED_CHANNELS", []))
    if band == "2g":
        return [c for c in chans if c <= 14]
    if band == "5g":
        return [c for c in chans if c > 14]
    return chans


async def run(args) -> int:
    mgr = WlanDeviceManager()
    interfaces = await mgr.refresh()
    cards = [i for i in interfaces if _is_8814(i)]
    if not cards:
        print(f"[FAIL] no RTL8814AU ({RTL8814_VID:04x}:{RTL8814_PID:04x}) on the USB bus")
        return 1
    iface = cards[0]
    driver_name = type(iface.driver).__name__
    env = os.environ.get(ENV_RTL8814_DRIVER, "") or "<unset>"

    print(f"\n=== A/B scan @ {time.strftime('%H:%M:%S')} ===")
    print(f"  {ENV_RTL8814_DRIVER}={env}  ->  driver: {driver_name}")

    def progress(pct, msg):
        print(f"  [{pct * 100:5.1f}%] {msg}")

    # --no-dig: disable the DKMS DIG/AGC watchdog (no-op on drivers without one) BEFORE
    # connect, to test whether it trades weak-AP sensitivity on a busy band.
    if args.no_dig:
        if hasattr(iface.driver, "enable_dig"):
            iface.driver.enable_dig = False
            print("  DIG watchdog DISABLED for this run")
        else:
            print(f"  (--no-dig ignored: {driver_name} has no DIG watchdog)")

    if not await iface.connect(progress):
        print("[FAIL] bring-up failed")
        await mgr.close_all()
        return 1

    if args.channels:
        requested = [int(x) for x in args.channels.split(",") if x.strip()]
        supported = set(getattr(iface.driver, "SUPPORTED_CHANNELS", []))
        channels = [c for c in requested if c in supported]
        dropped = [c for c in requested if c not in supported]
        if dropped:
            print(f"  (driver {driver_name} can't tune {dropped} — dropped for fairness)")
    else:
        channels = _band_channels(iface, args.band)
    if not channels:
        print(f"[FAIL] driver {driver_name} advertises no {args.band} channels")
        await mgr.close_all()
        return 1
    print(f"  band {args.band}: hopping {len(channels)} channels {channels} "
          f"@ {args.interval:g}s/hop for {args.duration:g}s")

    await iface.start_hopping(channels=channels, interval=args.interval)
    start = time.monotonic()
    try:
        while time.monotonic() - start < args.duration:
            await asyncio.sleep(2.0)
            aps = iface.get_access_points()
            beacons = sum(getattr(a, "beacons", 0) for a in aps)
            print(f"\r  {time.monotonic() - start:4.0f}s  nAPs={len(aps):3d}  "
                  f"beacons={beacons}", end="")
    except KeyboardInterrupt:
        pass
    print()
    await iface.stop_hopping()

    aps = sorted(iface.get_access_points(),
                 key=lambda a: getattr(a, "signal", -100), reverse=True)
    beacons = sum(getattr(a, "beacons", 0) for a in aps)
    print(f"\n[RESULT] driver={driver_name} band={args.band}  "
          f"{len(aps)} unique APs, {beacons} beacons over {args.duration:g}s")
    print("  top APs by signal (bssid / signal dBm / beacons / ch):")
    for a in aps[:20]:
        print(f"    {a.bssid}  {getattr(a, 'signal', '?'):>4} dBm  "
              f"{getattr(a, 'beacons', 0):>4}  ch{getattr(a, 'channel', '?')}")
    await mgr.close_all()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--band", choices=["2g", "5g", "all"], default="2g")
    ap.add_argument("--channels", default=None,
                    help="explicit comma list (e.g. 36,40,44,48,149,153,157,161,165) hopped "
                         "by BOTH drivers — for a fair fixed-set A/B; overrides --band")
    ap.add_argument("--duration", type=float, default=30.0, help="scan window (s)")
    ap.add_argument("--interval", type=float, default=0.5, help="per-hop dwell (s)")
    ap.add_argument("--no-dig", action="store_true",
                    help="disable the DKMS DIG/AGC watchdog (no-op on other drivers)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
