"""MT7925AU set_channel() True/False probe (bring-up + RX only, safe on hardware).

Pins down the "set_channel returned False but the card was still on the right channel" report.
Two things get confirmed on the live card:

  1. driver/interface set_channel(ch) truly tunes: beacons appear on populated 2.4 GHz channels,
     so the unconditional ``return True`` (driver.py:155) is truthful, not a false positive.
  2. WlanArray.set_channel(ch) returns False when the card is ALREADY on ``ch`` (array.py: the
     ``m.current_channel == channel`` skip returns tuned_any=False), while the card stays tuned.
     That is the exact "False but still on the desired channel" case.

    uv run python scripts/chips/mt7925au/set_channel_probe.py
    uv run python scripts/chips/mt7925au/set_channel_probe.py --watch 6 --channels 1,6,11
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from wifit3.wlan.array import WlanArray
from wifit3.wlan.discovery import build_interface, find_devices

_CHIPSET = "MT7925AU"


class FrameCounter:
    """RX callback: tally beacons (and all frames) seen since the last reset()."""

    def __init__(self) -> None:
        self.beacons = 0
        self.frames = 0

    def reset(self) -> None:
        self.beacons = self.frames = 0

    def __call__(self, pkt) -> None:
        if not pkt:
            return
        self.frames += 1
        if pkt.type == "beacon":
            self.beacons += 1


async def _watch(counter: FrameCounter, secs: float) -> tuple[int, int]:
    counter.reset()
    await asyncio.sleep(secs)
    return counter.beacons, counter.frames


async def main() -> int:
    p = argparse.ArgumentParser(description="MT7925AU set_channel True/False probe.")
    p.add_argument("--watch", type=float, default=4.0, help="RX window per channel (s). Default 4.")
    p.add_argument("--channels", type=str, default="1,6,11",
                   help="2.4 GHz channels to sweep. Default 1,6,11.")
    p.add_argument("--iters", type=int, default=20,
                   help="no-op set_channel repeats per channel (determinism tally). Default 20.")
    args = p.parse_args()
    channels = [int(c) for c in args.channels.split(",") if c.strip()]

    dev_id = next((d for d in find_devices() if d.chipset == _CHIPSET), None)
    if dev_id is None:
        print(f"[-] no {_CHIPSET} on the bus")
        return 1
    iface = build_interface(dev_id)
    if iface is None:
        print("[-] build_interface returned None")
        return 1

    print(f"[*] bringing up {iface.description} ...")
    if not await iface.connect():
        print("[-] connect() returned False")
        return 1

    counter = FrameCounter()
    iface.register_rx_callback(counter)

    print("\n=== 1. interface.set_channel() sweep (expect True + beacons where APs live) ===")
    for ch in channels:
        rv = await iface.set_channel(ch)
        b, f = await _watch(counter, args.watch)
        print(f"  set_channel({ch:>3}) -> {rv!s:<5}  current_channel={iface.current_channel:>3}  "
              f"beacons={b:>3}  frames={f:>4}")

    print(f"\n=== 2. WlanArray.set_channel() no-op repeat x{args.iters} per channel (the False) ===")
    # attach() reroutes RX to the array's deduped stream, so count on the array from here on.
    array = WlanArray()
    array.attach(iface)
    arr_counter = FrameCounter()
    array.register_rx_callback(arr_counter)

    total_noop = total_false = 0
    for ch in channels:
        other = next((c for c in channels if c != ch), ch)
        await array.set_channel(other)               # move away
        rv_move = await array.set_channel(ch)        # genuine move: expect True
        false_hits = 0
        for _ in range(args.iters):
            if (await array.set_channel(ch)) is False:   # no-op repeat: expect False every time
                false_hits += 1
        b, f = await _watch(arr_counter, args.watch)  # still tuned? beacons should still flow
        total_noop += args.iters
        total_false += false_hits
        print(f"  ch {ch:>3}: move->{rv_move!s:<5}  no-op False {false_hits:>2}/{args.iters}  "
              f"still-tuned beacons={b:>3} frames={f:>4}  current_channel={iface.current_channel}")

    print(f"\n[=] no-op array.set_channel returned False {total_false}/{total_noop} times while the "
          f"card stayed tuned (beacons flowing). Root cause: array.py current_channel skip.")

    await iface.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[!] Interrupted.")
        raise SystemExit(130)
