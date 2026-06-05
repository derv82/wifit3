"""RTL8812AU A/B scan — DKMS port vs mainline, via the REAL driver-selection path.

Brings the card up through `WlanDeviceManager` (so it honours `WIFIT3_RTL8812` exactly as
the app does), dwells on a channel or hops a band, and reports the A/B headline: unique
APs (breadth), total + peak beacons/s, and an optional canary row (RSSI + beacons/s — the
DIG-health indicator). The 8812's whole reason for existing is surviving the 2.4+5 GHz
scan that radio-silence-kills mainline, so the decisive A/B is `--band all` over a LONG
window: mainline's beacon rate collapses when it dies; the DKMS port's should keep
climbing.

Env polarity: mainline is the blank default, `=dkms` opts into the vendor port (the
default flips to DKMS once this A/B proves out). Run BOTH and compare across rounds —
**replug between every run** (a cold chip — no warm-state confound, and a slowly rising
noise floor would otherwise bias whichever card measured last):

    # round 1
    unplug, wait 3s, replug
    uv run scripts/rtl8812au_dkms/ab_scan.py --band all --duration 300              # mainline
    unplug, wait 3s, replug
    $env:WIFIT3_RTL8812='dkms'; uv run scripts/rtl8812au_dkms/ab_scan.py --band all --duration 300
    Remove-Item Env:WIFIT3_RTL8812
    # round 2: same, and so on (2-3 rounds), then paste all of it in one message.

Options: --channel N (fixed, default 1) · --band 2g|5g|all (hop instead) · --duration S
· --interval S (per-hop dwell) · --no-dig (disable the DKMS watchdog) · --canary BSSID.
Never prints SSIDs; pass --canary on your terminal only — no BSSID is committed here.
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

from _hwstop import interruptible_sleep  # noqa: E402
from wifit3.wlan.manager import ENV_RTL8812_DRIVER, WlanDeviceManager  # noqa: E402

RTL8812_VID, RTL8812_PID = 0x0BDA, 0x8812
# A 2.4+5 GHz sweep has legitimately-quiet arcs (DFS / sparse 5 GHz), so only a LONG
# flatline (~24 s, longer than one sweep's quiet arc) signals a real wedge — a 6 s gap
# does not. The per-tick beacon totals below are the raw signal; this is just the alarm.
_WEDGE_TICKS = 12


def _is_8812(iface) -> bool:
    return any(e.vid == RTL8812_VID and e.pid == RTL8812_PID
               for e in getattr(iface.driver, "SUPPORTED_IDS", []))


def _channels(iface, args) -> tuple:
    chans = list(getattr(iface.driver, "SUPPORTED_CHANNELS", []))
    if args.band == "2g":
        return [c for c in chans if c <= 14], "2g hop"
    if args.band == "5g":
        return [c for c in chans if c > 14], "5g hop"
    if args.band == "all":
        return chans, "all-band hop"
    return [args.channel], f"ch{args.channel} fixed"


async def run(args) -> int:
    mgr = WlanDeviceManager()
    cards = [i for i in await mgr.refresh() if _is_8812(i)]
    if not cards:
        print(f"[FAIL] no RTL8812AU ({RTL8812_VID:04x}:{RTL8812_PID:04x}) on the USB bus")
        return 1
    iface = cards[0]
    driver_name = type(iface.driver).__name__
    env = os.environ.get(ENV_RTL8812_DRIVER, "") or "<unset>"

    print(f"\n=== A/B scan @ {time.strftime('%H:%M:%S')} ===")
    print(f"  {ENV_RTL8812_DRIVER}={env}  ->  driver: {driver_name}")

    def progress(pct, msg):
        print(f"  [{pct * 100:5.1f}%] {msg}")

    # --no-dig isolates the DKMS DIG/AGC watchdog's effect (no-op on drivers without one).
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

    channels, what = _channels(iface, args)
    if not channels:
        print(f"[FAIL] driver {driver_name} advertises no channels for {args.band or args.channel}")
        await mgr.close_all()
        return 1
    print(f"  {what}: {len(channels)} channel(s) @ {args.interval:g}s/hop for {args.duration:g}s "
          f"({'DIG ON' if not args.no_dig else 'DIG OFF'})")

    await iface.start_hopping(channels=channels, interval=args.interval)
    start = time.monotonic()
    # Sample the beacon total each tick so a mid-run collapse (the death-loop fingerprint)
    # is visible in the timeline, not just the final tally.
    last_beacons, stalls, max_stall = 0, 0, 0
    try:
        while time.monotonic() - start < args.duration:
            await interruptible_sleep(2.0)
            aps = iface.get_access_points()
            beacons = sum(getattr(a, "beacons", 0) for a in aps)
            stalls = stalls + 1 if beacons == last_beacons else 0
            max_stall = max(max_stall, stalls)
            last_beacons = beacons
            flag = f"  <-- {stalls * 2}s with no new beacons (WEDGE?)" if stalls >= _WEDGE_TICKS else ""
            print(f"\r  {time.monotonic() - start:4.0f}s  nAPs={len(aps):3d}  beacons={beacons}{flag}",
                  end="" if not flag else "\n")
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("\n[stopping — Ctrl+C]")
    print()
    await iface.stop_hopping()

    elapsed = max(time.monotonic() - start, 1e-3)
    aps = sorted(iface.get_access_points(),
                 key=lambda a: getattr(a, "beacons", 0), reverse=True)
    total = sum(getattr(a, "beacons", 0) for a in aps)
    n2 = sum(1 for a in aps if (getattr(a, "channel", 0) or 0) <= 14)
    n5 = len(aps) - n2
    print(f"\n[RESULT] driver={driver_name}  {what}  {len(aps)} unique APs "
          f"({n2} on 2.4 GHz, {n5} on 5 GHz), {total} beacons ({total / elapsed:.1f}/s) "
          f"over {elapsed:.0f}s")
    print(f"  survival: longest no-beacon stall {max_stall * 2}s -- "
          f"{'CLEAN (radio stayed alive)' if max_stall < _WEDGE_TICKS else 'POSSIBLE WEDGE (flatlined)'}")
    if aps:
        peak = aps[0]
        print(f"  peak AP: {getattr(peak, 'beacons', 0)} beacons "
              f"({getattr(peak, 'beacons', 0) / elapsed:.1f}/s), {getattr(peak, 'signal', '?')} dBm")
        print("  top APs by beacon count (bssid / ch / beacons / /s / signal dBm):")
        for a in aps[:10]:
            print(f"    {a.bssid}  ch{str(getattr(a, 'channel', '?')):>3}  "
                  f"{getattr(a, 'beacons', 0):>4}  {getattr(a, 'beacons', 0) / elapsed:4.1f}/s  "
                  f"{getattr(a, 'signal', '?')} dBm")

    if args.canary:
        canary = args.canary.lower()
        c = next((a for a in aps if (a.bssid or "").lower() == canary), None)
        c_b = getattr(c, "beacons", 0) if c else 0
        print(f"\n  CANARY {canary}: {c_b} beacons ({c_b / elapsed:.1f}/s), "
              f"{getattr(c, 'signal', '?') if c else '<not heard>'} dBm  "
              f"[a steady rate = healthy DIG; a flatline = the radio went deaf]")
    else:
        print("\n  (no --canary given — pass one for the per-AP DIG-health row)")
    await mgr.close_all()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=1, help="fixed channel (default 1)")
    ap.add_argument("--band", choices=["2g", "5g", "all"], default=None,
                    help="hop this band instead of the fixed --channel ('all' = the death-loop A/B)")
    ap.add_argument("--duration", type=float, default=30.0, help="scan window (s)")
    ap.add_argument("--interval", type=float, default=0.5, help="per-hop dwell when hopping (s)")
    ap.add_argument("--no-dig", action="store_true", help="disable the DKMS DIG/AGC watchdog")
    ap.add_argument("--canary", default=None, help="A/B canary BSSID (pass on your terminal)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
