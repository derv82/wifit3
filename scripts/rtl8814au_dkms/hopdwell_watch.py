"""RTL8814AU — beacon watch on a dwell that FOLLOWS a hop series (exercises the dwell tune).

beacon_watch.py sits from cold (no hop), so it can't show the hop->dwell RX-reader-race fix.
This hops 2.4<->5 GHz for a while (scan=True), lands on a controllable --last-hop (5 GHz or a
2.4 GHz channel), then dwells --dwell-channel (scan=False, the tune the fix protects) and prints
the per-second histogram + mean/median/min/max/stdev/zero-seconds for the pinned --ref AP.

    uv run python scripts/rtl8814au_dkms/hopdwell_watch.py --ref <BSSID> --last-hop 149   # 5->2
    uv run python scripts/rtl8814au_dkms/hopdwell_watch.py --ref <BSSID> --last-hop 6     # 2->2
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wifit3.wlan.manager import WlanDeviceManager  # noqa: E402
from wifit3.wlan.packet import WlanFrameParser  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    ref = args.ref.lower()
    hop = [int(c) for c in args.hop_channels.split(",") if c.strip()]
    mgr = WlanDeviceManager()
    ifaces = await mgr.refresh()
    if not ifaces:
        print("[-] no device", file=sys.stderr)
        return 1
    iface = ifaces[0]
    if not await iface.connect(progress_cb=lambda p, m: None):
        print("[-] bring-up failed", file=sys.stderr)
        await mgr.close_all()
        return 1
    beacons: deque = deque(maxlen=200_000)

    def on_rx(raw: bytes, rssi: int, ts: float) -> None:
        try:
            p = WlanFrameParser.parse_80211_frame(raw, rssi)
        except Exception:  # noqa: BLE001
            return
        if p and p.type == "beacon" and p.bssid and p.bssid.lower() == ref:
            beacons.append(time.monotonic())
    iface.register_rx_callback(on_rx)

    band = "5 GHz" if args.last_hop > 14 else "2.4 GHz"
    try:
        for cycle in range(args.cycles):
            hs = args.hop_secs if cycle == 0 else args.rehop_secs
            t_end = time.monotonic() + hs                     # hop 2.4<->5 (scan=True)
            i = 0
            while time.monotonic() < t_end:
                await iface.set_channel(hop[i % len(hop)], scan=True)
                await asyncio.sleep(args.hop_dwell)
                i += 1
            await iface.set_channel(args.last_hop, scan=True)  # deterministic last hop
            await asyncio.sleep(args.hop_dwell)
            beacons.clear()
            d0 = time.monotonic()
            await iface.set_channel(args.dwell_channel, scan=False)  # the dwell tune (fix protects)
            await asyncio.sleep(args.dwell_secs)
            secs = int(args.dwell_secs)
            buckets = [0] * secs
            for ts in list(beacons):
                b = int(ts - d0)
                if 0 <= b < secs:
                    buckets[b] += 1
            tag = f"cycle {cycle + 1}/{args.cycles}: " if args.cycles > 1 else ""
            print(f"\n[*] {tag}ref {args.ref} on ch{args.dwell_channel} after a {band} hop:",
                  file=sys.stderr)
            for s, n in enumerate(buckets, 1):
                print(f"  sec {s:3d}: {n:3d}/s  {'#' * n}", file=sys.stderr)
            print("  " + "-" * 52, file=sys.stderr)
            print(f"  total={sum(buckets)}  mean={statistics.mean(buckets):.1f}  "
                  f"median={statistics.median(buckets):.0f}  min={min(buckets)}  max={max(buckets)}  "
                  f"stdev={statistics.pstdev(buckets):.1f}  zero-seconds={buckets.count(0)}",
                  file=sys.stderr)
    finally:
        await mgr.close_all()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="8814au beacon watch on a dwell after a hop series.")
    p.add_argument("--ref", required=True, help="reference BSSID (runtime only).")
    p.add_argument("--hop-channels", default="1,149")
    p.add_argument("--hop-secs", type=float, default=45.0)
    p.add_argument("--hop-dwell", type=float, default=0.5)
    p.add_argument("--last-hop", type=int, default=149, help="channel of the final hop before the dwell.")
    p.add_argument("--dwell-channel", type=int, default=1)
    p.add_argument("--dwell-secs", type=float, default=15.0)
    p.add_argument("--cycles", type=int, default=1, help="repeat hop->dwell N times (each a histogram).")
    p.add_argument("--rehop-secs", type=float, default=10.0, help="hop duration for cycles after the first.")
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
